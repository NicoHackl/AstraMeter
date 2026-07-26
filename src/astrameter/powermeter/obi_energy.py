"""OBI / heyOBI Energy Tracking (cloud) powermeter using OBI's live mode.

The OBI Energy Tracking sensor has no local API — readings only reach the
heyOBI cloud.  Its normal upload cadence (once every few minutes) is far too
slow for battery control, but the app can put a sensor into **live mode**: the
sensor's ``uploadInterval`` is dropped to a couple of seconds and readings are
pushed over a WebSocket.  This class drives exactly that flow:

1. log in with the heyOBI account credentials to get a JWT (kept in memory
   only, never logged or persisted),
2. discover the energy-tracking bridge and sensor via ``GET /bridges`` unless
   they are pinned in the config,
3. switch the sensor into live mode (``PATCH /sensors/{id}``), and
4. subscribe to the live-mode WebSocket, which pushes ``power`` readings.

On :meth:`stop` the sensor's upload interval is restored so it does not stay
on the battery-hungry live cadence after AstraMeter exits.

The protocol is undocumented; the request shapes here mirror the community
Home Assistant integration at https://github.com/tomquist/obi_energy.
"""

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from .base import Powermeter, stream_fresh

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
API_BASE_URL = "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"
BRIDGES_URL = f"{API_BASE_URL}/bridges"
SENSOR_URL_TEMPLATE = API_BASE_URL + "/sensors/{sensor_id}"
LIVE_DATA_URL = "wss://energy-tracking-livemode.prod-eks.dbs.obi.solutions/retrieving"

# Public client identifiers the heyOBI app sends; the backend rejects requests
# without them.
API_KEY = "Rh57q3vtOPYTf6FtArVN1boy2AyEiIqaGEmnMks7"
USER_AGENT = "heyOBI APP / iPhone17,2 / 4.9.1 / 560"
LIVE_USER_AGENT = "app_client"
LIB_VERSION = "26.6.9"
ACCEPT_LANGUAGE = "de-DE,de;q=0.9"
LOGIN_COOKIE = "obi_storeid=527"
LOGIN_HOST = "www.obi.de"
LOGIN_ORIGIN = "https://www.obi.de"
LOGIN_REFERER = "https://www.obi.de/"
ACCEPT_BRIDGES = "application/vnd.obi.companion.energy-tracking.bridge.v1+json"
ACCEPT_SENSOR = "application/vnd.obi.companion.energy-tracking.sensor.v1+json"

# WebSocket frames carry this event type for a sensor reading.
LIVE_EVENT = "mqttMessage"

DEFAULT_COUNTRY = "de"
# Upload interval (seconds) requested while AstraMeter is running, and the one
# restored on shutdown.  Both match what the heyOBI app itself uses when its
# live view is opened and closed.
DEFAULT_LIVE_UPLOAD_INTERVAL = 2
DEFAULT_IDLE_UPLOAD_INTERVAL = 300

# The JWT is valid for roughly an hour; refresh well before it expires.
DEFAULT_LOGIN_REFRESH_SECONDS = 55 * 60.0

# Bounded timeout for every REST call (login, bridges, sensor update).
REQUEST_TIMEOUT_SECONDS = 30.0

# WebSocket heartbeat (seconds).  aiohttp sends ping frames at this interval
# and closes the connection when no pong arrives within 2x — catches half-open
# sockets that would otherwise freeze ``async for msg in ws`` forever.
WS_HEARTBEAT_SECONDS = 30.0

# Maximum age of the last live reading before ``get_powermeter_watts``
# considers it stale and raises.  Live mode pushes every ~2 s, so 30 s of
# silence is already a very generous margin.
DEFAULT_MAX_MEASUREMENT_AGE_SECONDS = 30.0

# Application-level watchdog: after this long without a reading, first
# re-request live mode (the backend can quietly drop the sensor back to its
# slow cadence), then — if that doesn't help — force a reconnect.
LIVE_WATCHDOG_TIMEOUT_SECONDS = 20.0

RECONNECT_DELAY_SECONDS = 5.0
MAX_RECONNECT_DELAY_SECONDS = 300.0


class ObiApiError(Exception):
    """The OBI backend returned an error or an unusable response."""


class ObiAuthError(ObiApiError):
    """Credentials were rejected, or the session token is no longer valid."""


class ObiEnergy(Powermeter):
    """Reads an OBI Energy Tracking sensor through OBI's cloud live mode.

    Returns a single signed value in watts (positive = grid import, negative =
    feed-in).  Flip it with ``POWER_MULTIPLIER = -1`` if your meter is wired
    the other way round.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        bridge_id: str = "",
        sensor_id: str = "",
        country: str = DEFAULT_COUNTRY,
        live_upload_interval: int = DEFAULT_LIVE_UPLOAD_INTERVAL,
        idle_upload_interval: int = DEFAULT_IDLE_UPLOAD_INTERVAL,
        login_refresh_interval: float = DEFAULT_LOGIN_REFRESH_SECONDS,
        max_measurement_age_seconds: float = DEFAULT_MAX_MEASUREMENT_AGE_SECONDS,
        watchdog_timeout_seconds: float = LIVE_WATCHDOG_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not email or not password:
            raise ValueError("OBI Energy requires EMAIL and PASSWORD")
        self.email = email
        self.password = password
        self.country = country or DEFAULT_COUNTRY
        # Configured ids (may be blank); the resolved ones live in _bridge_id/
        # _sensor_id once discovery has run.
        self.bridge_id = bridge_id.strip()
        self.sensor_id = sensor_id.strip()
        self.live_upload_interval = max(1, live_upload_interval)
        self.idle_upload_interval = idle_upload_interval
        self._login_refresh_interval = max(60.0, login_refresh_interval)
        self._max_measurement_age_seconds = max(0.0, max_measurement_age_seconds)
        self._watchdog_timeout_seconds = max(1.0, watchdog_timeout_seconds)
        self._clock = clock or time.monotonic

        self.values: list[float] | None = None
        # Diagnostics reported alongside the reading; kept for log output only.
        self.rssi: int | None = None
        self.battery: int | None = None

        self._token: str | None = None
        self._token_obtained_at: float | None = None
        self._bridge_id: str = self.bridge_id
        self._sensor_id: str = self.sensor_id
        # True once the sensor has been switched to the live cadence, so stop()
        # knows whether it has anything to restore.
        self._live_mode_active = False

        self._last_measurement_time: float | None = None
        # Read-only health flag for stream_online(): set once the live
        # WebSocket is up, cleared whenever it drops.
        self._connected = False
        # True only while readings arrive as a *continuous* stream (each one
        # before the previous went stale).  A single replayed value after a gap
        # is not a live stream and must not flip "Online" back on.
        self._stream_healthy = False
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._message_event = asyncio.Event()
        # Set on every reading; the watchdog clears it to re-arm its timer.
        self._fresh_measurement_event = asyncio.Event()

    # --- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._session:
            return
        self.values = None
        self._last_measurement_time = None
        self._connected = False
        self._stream_healthy = False
        self._message_event = asyncio.Event()
        self._fresh_measurement_event = asyncio.Event()
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        # Clear before cancelling: the ws loop re-raises CancelledError before
        # its own reset runs, so stream_online() would otherwise stay True.
        self._connected = False
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._session:
            await self._restore_idle_upload_interval()
            await self._session.close()
            self._session = None

    # --- Authentication ----------------------------------------------------

    async def _login(self) -> str:
        """Log in and return a fresh JWT, kept in memory only."""
        assert self._session is not None
        # Serialize the body ourselves and send it via ``data=``: the CloudFront
        # in front of the login endpoint is picky about the exact header set,
        # which aiohttp's ``json=`` shortcut would re-derive.
        payload = json.dumps(
            {"password": self.password, "country": self.country, "email": self.email},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "*/*",
            "user-agent": USER_AGENT,
            "accept-language": ACCEPT_LANGUAGE,
            "accept-encoding": "identity",
            "cookie": LOGIN_COOKIE,
            "content-length": str(len(payload)),
            "host": LOGIN_HOST,
            "origin": LOGIN_ORIGIN,
            "referer": LOGIN_REFERER,
        }
        try:
            async with self._session.post(
                LOGIN_URL,
                data=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status in (401, 403):
                    raise ObiAuthError(
                        f"OBI login failed with HTTP {resp.status}: check EMAIL/PASSWORD"
                    )
                if resp.status >= 400:
                    raise ObiApiError(f"OBI login failed with HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (ObiApiError, asyncio.CancelledError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ObiApiError(f"OBI login request failed: {err}") from err

        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ObiAuthError("OBI login response did not contain a token")
        self._token = token
        self._token_obtained_at = self._clock()
        logger.debug("OBI Energy: login succeeded")
        return token

    async def _ensure_token(self) -> str:
        if (
            self._token is None
            or self._token_obtained_at is None
            or (self._clock() - self._token_obtained_at) >= self._login_refresh_interval
        ):
            return await self._login()
        return self._token

    def _api_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": API_KEY,
            "x-app-type": "b2c",
            "Accept": ACCEPT_BRIDGES,
            "accept-language": ACCEPT_LANGUAGE,
            "user-agent": USER_AGENT,
            # The API is fronted by CloudFront; without this it can hand back a
            # stale cached bridge list.
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }

    def _app_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": ACCEPT_SENSOR,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Content-Type": ACCEPT_SENSOR,
            "User-Agent": LIVE_USER_AGENT,
            "X-Platform": "iOS",
            "X-Lib-Version": LIB_VERSION,
        }

    def _live_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "User-Agent": LIVE_USER_AGENT,
            "X-Platform": "iOS",
            "X-Lib-Version": LIB_VERSION,
        }

    # --- REST calls --------------------------------------------------------

    async def _send_json(
        self,
        method: str,
        url: str,
        *,
        headers_factory: Callable[[str], dict[str, str]],
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an authenticated request, refreshing the token once on 401."""
        assert self._session is not None
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        for attempt in (0, 1):
            token = await self._ensure_token()
            headers = headers_factory(token)
            if body is not None:
                headers["Content-Length"] = str(len(body))
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=body,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        logger.debug(
                            "OBI Energy: %s %s returned 401, refreshing token",
                            method,
                            url,
                        )
                        self._token = None
                        continue
                    if resp.status in (401, 403):
                        raise ObiAuthError(
                            f"OBI rejected {method} {url} with HTTP {resp.status}"
                        )
                    if resp.status >= 400:
                        raise ObiApiError(
                            f"OBI {method} {url} failed with HTTP {resp.status}"
                        )
                    return await resp.json(content_type=None)
            except (ObiApiError, asyncio.CancelledError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise ObiApiError(f"OBI {method} {url} failed: {err}") from err
        raise ObiAuthError(f"OBI {method} {url} still unauthorized after a fresh login")

    async def _resolve_ids(self) -> tuple[str, str]:
        """Return the bridge/sensor ids, discovering them once if unset."""
        if self._bridge_id and self._sensor_id:
            return self._bridge_id, self._sensor_id

        bridges = await self._send_json(
            "GET", BRIDGES_URL, headers_factory=self._api_headers
        )
        if not isinstance(bridges, list):
            raise ObiApiError("Unexpected response format for the OBI bridge list")

        for bridge in bridges:
            if not isinstance(bridge, dict):
                continue
            found_bridge = bridge.get("id")
            if not found_bridge or (self.bridge_id and found_bridge != self.bridge_id):
                continue
            for sensor in bridge.get("sensors") or []:
                if not isinstance(sensor, dict):
                    continue
                found_sensor = sensor.get("id")
                if not found_sensor or (
                    self.sensor_id and found_sensor != self.sensor_id
                ):
                    continue
                self._bridge_id = str(found_bridge)
                self._sensor_id = str(found_sensor)
                logger.info(
                    "OBI Energy: using bridge %s / sensor %s "
                    "(pin them with BRIDGE_ID/SENSOR_ID to skip discovery)",
                    self._bridge_id,
                    self._sensor_id,
                )
                return self._bridge_id, self._sensor_id

        raise ObiApiError(
            "OBI Energy: no matching bridge/sensor found for this account "
            "(check BRIDGE_ID/SENSOR_ID, or that the tracker is set up in the heyOBI app)"
        )

    async def _set_upload_interval(self, interval: int) -> int | None:
        """Set the sensor's upload interval and return what the backend stored."""
        sensor_id = self._sensor_id
        data = await self._send_json(
            "PATCH",
            SENSOR_URL_TEMPLATE.format(sensor_id=sensor_id),
            headers_factory=self._app_headers,
            payload={"id": sensor_id, "uploadInterval": interval},
        )
        if not isinstance(data, dict):
            raise ObiApiError("Unexpected response format for the OBI sensor update")
        stored = data.get("uploadInterval")
        return stored if isinstance(stored, int) else None

    async def _enable_live_mode(self) -> None:
        stored = await self._set_upload_interval(self.live_upload_interval)
        self._live_mode_active = True
        logger.debug("OBI Energy: live mode requested (uploadInterval=%s)", stored)

    async def _restore_idle_upload_interval(self) -> None:
        """Put the sensor back on its slow cadence — best effort, never raises."""
        if not self._live_mode_active or self.idle_upload_interval <= 0:
            return
        try:
            stored = await self._set_upload_interval(self.idle_upload_interval)
            logger.info("OBI Energy: live mode disabled (uploadInterval=%s)", stored)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning(
                "OBI Energy: could not restore the sensor's upload interval "
                "(it may stay on the live cadence): %s",
                err,
            )
        finally:
            self._live_mode_active = False

    # --- Live WebSocket ----------------------------------------------------

    async def _ws_loop(self) -> None:
        # Backoff, reset on every successful connect: unlike a LAN meter, a
        # retry here costs a login against OBI's servers, so a wrong password
        # or an outage must not turn into a request every few seconds.
        delay = RECONNECT_DELAY_SECONDS
        while True:
            try:
                bridge_id, sensor_id = await self._resolve_ids()
                await self._enable_live_mode()
                token = await self._ensure_token()
                assert self._session is not None
                async with self._session.ws_connect(
                    LIVE_DATA_URL,
                    params={"bridgeId": bridge_id, "sensorId": sensor_id},
                    headers=self._live_headers(token),
                    heartbeat=WS_HEARTBEAT_SECONDS,
                    compress=15,
                ) as ws:
                    logger.info(
                        "OBI Energy: live WebSocket connected (bridge %s / sensor %s)",
                        bridge_id,
                        sensor_id,
                    )
                    self._connected = True
                    delay = RECONNECT_DELAY_SECONDS
                    watchdog = asyncio.create_task(self._live_watchdog(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_raw_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                self._handle_raw_message(
                                    msg.data.decode("utf-8", "replace")
                                )
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                break
                    finally:
                        watchdog.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await watchdog
                    logger.info("OBI Energy: live WebSocket closed")
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # A rejected handshake usually means the token went stale
                # mid-flight; drop it so the next attempt logs in again.
                if err.status in (401, 403):
                    self._token = None
                logger.error(
                    "OBI Energy: live WebSocket handshake failed with HTTP %s",
                    err.status,
                )
            except ObiAuthError as err:
                self._token = None
                logger.error("OBI Energy: authentication failed: %s", err)
            except ObiApiError as err:
                logger.error("OBI Energy: %s", err)
            except Exception as err:
                logger.error("OBI Energy WebSocket error: %s", err, exc_info=True)
            self._connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_SECONDS)

    async def _live_watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Re-request live mode, then reconnect, when readings dry up.

        The socket stays happily open when the backend drops the sensor back to
        its slow upload cadence — no reading arrives, but nothing errors out
        either.  Re-asserting live mode fixes that case without a reconnect;
        a second silent window means the connection itself is dead.
        """
        misses = 0
        while True:
            self._fresh_measurement_event.clear()
            try:
                await asyncio.wait_for(
                    self._fresh_measurement_event.wait(),
                    timeout=self._watchdog_timeout_seconds,
                )
            except asyncio.TimeoutError:
                misses += 1
                if misses == 1:
                    logger.warning(
                        "OBI Energy: no live reading for %.0fs, re-requesting live mode",
                        self._watchdog_timeout_seconds,
                    )
                    try:
                        await self._enable_live_mode()
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        logger.warning(
                            "OBI Energy: re-requesting live mode failed: %s", err
                        )
                    continue
                logger.warning(
                    "OBI Energy: still no live reading, force-closing the "
                    "WebSocket to trigger a reconnect"
                )
                await ws.close()
                return
            else:
                misses = 0

    def _handle_raw_message(self, raw: str) -> None:
        """Decode a live frame, which may hold several JSON documents back to back."""
        decoder = json.JSONDecoder()
        position = 0
        handled = False
        while position < len(raw):
            while position < len(raw) and raw[position].isspace():
                position += 1
            if position >= len(raw):
                break
            try:
                message, position = decoder.raw_decode(raw, position)
            except ValueError:
                logger.debug("OBI Energy: ignoring undecodable live message")
                return
            if self._handle_message(message):
                handled = True
        if not handled:
            logger.debug("OBI Energy: live message contained no power reading")

    def _handle_message(self, message: Any) -> bool:
        """Apply one decoded live message; return whether it held a reading."""
        if not isinstance(message, dict) or message.get("event") != LIVE_EVENT:
            return False
        payload = message.get("data")
        if not isinstance(payload, dict):
            return False
        power = payload.get("power")
        if isinstance(power, bool) or not isinstance(power, int | float):
            return False
        value = float(power)
        if not math.isfinite(value):
            return False

        now = self._clock()
        max_age = self._max_measurement_age_seconds
        previous = self._last_measurement_time
        if max_age <= 0:
            # Staleness check disabled: treat every sample as a live stream.
            self._stream_healthy = True
        else:
            self._stream_healthy = previous is not None and (now - previous) <= max_age

        self.values = [value]
        self._last_measurement_time = now
        rssi = payload.get("rssi")
        if isinstance(rssi, int) and not isinstance(rssi, bool):
            self.rssi = rssi
        battery = payload.get("battery")
        if isinstance(battery, int) and not isinstance(battery, bool):
            self.battery = battery
        self._message_event.set()
        self._fresh_measurement_event.set()
        return True

    # --- Powermeter API ----------------------------------------------------

    def stream_online(self) -> bool | None:
        return (
            self._connected
            and self._stream_healthy
            and stream_fresh(
                self._last_measurement_time,
                self._max_measurement_age_seconds,
                self._clock,
            )
        )

    async def get_powermeter_watts(self) -> list[float]:
        if self.values is None:
            raise ValueError("No value received from OBI Energy")
        if (
            self._max_measurement_age_seconds > 0
            and self._last_measurement_time is not None
        ):
            age = self._clock() - self._last_measurement_time
            if age > self._max_measurement_age_seconds:
                raise ValueError(
                    f"OBI Energy reading is stale "
                    f"({age:.1f}s old, max {self._max_measurement_age_seconds:.1f}s)"
                )
        return list(self.values)

    async def wait_for_message(self, timeout: float = 5) -> None:
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for OBI Energy reading") from None

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._message_event.clear()
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for OBI Energy reading") from None
