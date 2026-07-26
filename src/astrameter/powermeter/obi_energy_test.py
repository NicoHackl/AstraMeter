import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from .obi_energy import ObiApiError, ObiAuthError, ObiEnergy


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeResponse:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession.

    ``post`` serves the login endpoint, ``request`` every authenticated REST
    call.  Each queue keeps serving its last entry once exhausted, so a test
    only needs to enqueue the responses it cares about.
    """

    def __init__(self, logins=None, responses=None) -> None:
        self.logins = list(logins or [_FakeResponse(200, {"token": "tok-1"})])
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    @staticmethod
    def _next(queue):
        if not queue:
            raise AssertionError("FakeSession: no response queued")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next(self.logins)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._next(self.responses)

    async def close(self):
        self.closed = True


def _create_powermeter(**overrides: Any) -> ObiEnergy:
    defaults: dict[str, Any] = dict(
        email="user@example.com",
        password="hunter2",
        bridge_id="bridge-1",
        sensor_id="sensor-1",
    )
    defaults.update(overrides)
    return ObiEnergy(**defaults)


def _live_frame(**payload) -> str:
    return json.dumps({"event": "mqttMessage", "data": payload})


# --- Category A: Live message parsing ---------------------------------------


def test_live_message_stores_power():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=432, rssi=-61, battery=97))
    assert pm.values == [432.0]
    assert pm.rssi == -61
    assert pm.battery == 97


def test_negative_power_preserved():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=-1500.5))
    assert pm.values == [-1500.5]


def test_concatenated_frames_all_applied():
    """OBI packs several JSON documents into one WebSocket frame."""
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100) + " " + _live_frame(power=250))
    assert pm.values == [250.0]


def test_trailing_garbage_keeps_earlier_reading():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100) + "{not json")
    assert pm.values == [100.0]


def test_other_event_types_ignored():
    pm = _create_powermeter()
    pm._handle_raw_message(json.dumps({"event": "ping", "data": {"power": 100}}))
    assert pm.values is None


def test_non_numeric_power_ignored():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power="500"))
    assert pm.values is None


def test_boolean_power_ignored():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=True))
    assert pm.values is None


def test_non_finite_power_ignored():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=float("nan")))
    assert pm.values is None


def test_missing_power_keeps_previous_value():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100))
    pm._handle_raw_message(_live_frame(rssi=-60))
    assert pm.values == [100.0]


def test_malformed_json_does_not_crash():
    pm = _create_powermeter()
    pm._handle_raw_message("not valid json")
    assert pm.values is None


def test_non_dict_message_does_not_crash():
    pm = _create_powermeter()
    pm._handle_raw_message(json.dumps([1, 2, 3]))
    pm._handle_raw_message(json.dumps("just a string"))
    assert pm.values is None


def test_non_dict_data_ignored():
    pm = _create_powermeter()
    pm._handle_raw_message(json.dumps({"event": "mqttMessage", "data": "nope"}))
    assert pm.values is None


def test_message_sets_event():
    pm = _create_powermeter()
    assert not pm._message_event.is_set()
    pm._handle_raw_message(_live_frame(power=100))
    assert pm._message_event.is_set()


# --- Category B: get_powermeter_watts / staleness ---------------------------


async def test_get_watts_no_data_raises():
    pm = _create_powermeter()
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()


async def test_get_watts_returns_copy():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100))
    result = await pm.get_powermeter_watts()
    result.append(999)
    assert await pm.get_powermeter_watts() == [100.0]


async def test_get_watts_raises_when_reading_is_stale():
    clock = _FakeClock()
    pm = _create_powermeter(max_measurement_age_seconds=30.0, clock=clock)
    pm._handle_raw_message(_live_frame(power=100))
    clock.advance(29.0)
    assert await pm.get_powermeter_watts() == [100.0]
    clock.advance(2.0)
    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_staleness_disabled_when_max_age_is_zero():
    clock = _FakeClock()
    pm = _create_powermeter(max_measurement_age_seconds=0.0, clock=clock)
    pm._handle_raw_message(_live_frame(power=100))
    clock.advance(100000.0)
    assert await pm.get_powermeter_watts() == [100.0]


# --- Category C: stream_online health hook ----------------------------------


def test_stream_online_false_before_any_reading():
    assert _create_powermeter().stream_online() is False


def test_stream_online_true_when_connected_and_streaming():
    clock = _FakeClock()
    pm = _create_powermeter(max_measurement_age_seconds=30.0, clock=clock)
    pm._connected = True
    # One sample alone isn't a stream; the second establishes continuous flow.
    pm._handle_raw_message(_live_frame(power=100))
    assert pm.stream_online() is False
    clock.advance(2.0)
    pm._handle_raw_message(_live_frame(power=110))
    assert pm.stream_online() is True


def test_stream_online_false_when_connected_but_stale():
    clock = _FakeClock()
    pm = _create_powermeter(max_measurement_age_seconds=30.0, clock=clock)
    pm._connected = True
    pm._handle_raw_message(_live_frame(power=100))
    clock.advance(2.0)
    pm._handle_raw_message(_live_frame(power=110))
    clock.advance(31.0)
    assert pm.stream_online() is False


def test_stream_online_false_when_disconnected_even_if_fresh():
    clock = _FakeClock()
    pm = _create_powermeter(clock=clock)
    pm._handle_raw_message(_live_frame(power=100))
    clock.advance(2.0)
    pm._handle_raw_message(_live_frame(power=110))
    assert pm.stream_online() is False


# --- Category D: wait_for_message -------------------------------------------


async def test_wait_for_message_returns_when_data_available():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100))
    await pm.wait_for_message(timeout=1)


async def test_wait_for_message_timeout():
    pm = _create_powermeter()
    with pytest.raises(TimeoutError):
        await pm.wait_for_message(timeout=0)


async def test_wait_for_next_message_blocks_until_new():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100))

    async def _push_later():
        await asyncio.sleep(0.05)
        pm._handle_raw_message(_live_frame(power=200))

    task = asyncio.create_task(_push_later())
    await pm.wait_for_next_message(timeout=2)
    await task
    assert await pm.get_powermeter_watts() == [200.0]


async def test_wait_for_next_message_timeout():
    pm = _create_powermeter()
    pm._handle_raw_message(_live_frame(power=100))
    with pytest.raises(TimeoutError):
        await pm.wait_for_next_message(timeout=0)


# --- Category E: Login / token handling -------------------------------------


def test_missing_credentials_rejected():
    with pytest.raises(ValueError):
        ObiEnergy("", "secret")
    with pytest.raises(ValueError):
        ObiEnergy("user@example.com", "")


async def test_login_sends_credentials_and_stores_token():
    pm = _create_powermeter()
    pm._session = _FakeSession()
    token = await pm._login()
    assert token == "tok-1"
    method, url, kwargs = pm._session.calls[0]
    assert method == "POST"
    assert url.endswith("/public/login")
    assert json.loads(kwargs["data"]) == {
        "password": "hunter2",
        "country": "de",
        "email": "user@example.com",
    }


async def test_login_country_is_configurable():
    pm = _create_powermeter(country="at")
    pm._session = _FakeSession()
    await pm._login()
    assert json.loads(pm._session.calls[0][2]["data"])["country"] == "at"


async def test_login_rejects_bad_credentials():
    pm = _create_powermeter()
    pm._session = _FakeSession(logins=[_FakeResponse(401, {})])
    with pytest.raises(ObiAuthError):
        await pm._login()


async def test_login_without_token_raises():
    pm = _create_powermeter()
    pm._session = _FakeSession(logins=[_FakeResponse(200, {"foo": "bar"})])
    with pytest.raises(ObiAuthError):
        await pm._login()


async def test_token_is_reused_until_refresh_interval():
    clock = _FakeClock()
    pm = _create_powermeter(login_refresh_interval=600.0, clock=clock)
    pm._session = _FakeSession(
        logins=[
            _FakeResponse(200, {"token": "tok-1"}),
            _FakeResponse(200, {"token": "tok-2"}),
        ]
    )
    assert await pm._ensure_token() == "tok-1"
    clock.advance(599.0)
    assert await pm._ensure_token() == "tok-1"
    clock.advance(2.0)
    assert await pm._ensure_token() == "tok-2"


async def test_request_refreshes_token_once_on_401():
    pm = _create_powermeter()
    pm._session = _FakeSession(
        logins=[
            _FakeResponse(200, {"token": "tok-1"}),
            _FakeResponse(200, {"token": "tok-2"}),
        ],
        responses=[_FakeResponse(401, {}), _FakeResponse(200, {"uploadInterval": 2})],
    )
    assert await pm._set_upload_interval(2) == 2
    auth_headers = [
        c[2]["headers"]["Authorization"] for c in pm._session.calls if c[0] == "PATCH"
    ]
    assert auth_headers == ["Bearer tok-1", "Bearer tok-2"]


async def test_persistent_401_raises_auth_error():
    pm = _create_powermeter()
    pm._session = _FakeSession(responses=[_FakeResponse(401, {})])
    with pytest.raises(ObiAuthError):
        await pm._set_upload_interval(2)


async def test_server_error_raises_api_error():
    pm = _create_powermeter()
    pm._session = _FakeSession(responses=[_FakeResponse(500, {})])
    with pytest.raises(ObiApiError):
        await pm._set_upload_interval(2)


# --- Category F: Bridge/sensor discovery ------------------------------------


async def test_configured_ids_skip_discovery():
    pm = _create_powermeter()
    pm._session = _FakeSession()
    assert await pm._resolve_ids() == ("bridge-1", "sensor-1")
    assert not [c for c in pm._session.calls if c[0] == "GET"]


async def test_ids_are_discovered_when_unset():
    pm = _create_powermeter(bridge_id="", sensor_id="")
    pm._session = _FakeSession(
        responses=[
            _FakeResponse(
                200,
                [
                    {"id": "hh-9", "sensors": [{"id": "mid-7", "batteryLevel": 90}]},
                ],
            )
        ]
    )
    assert await pm._resolve_ids() == ("hh-9", "mid-7")
    # Cached: a second call must not hit the API again.
    calls = len(pm._session.calls)
    assert await pm._resolve_ids() == ("hh-9", "mid-7")
    assert len(pm._session.calls) == calls


async def test_discovery_honours_a_pinned_bridge_id():
    pm = _create_powermeter(bridge_id="hh-2", sensor_id="")
    pm._session = _FakeSession(
        responses=[
            _FakeResponse(
                200,
                [
                    {"id": "hh-1", "sensors": [{"id": "mid-1"}]},
                    {"id": "hh-2", "sensors": [{"id": "mid-2"}]},
                ],
            )
        ]
    )
    assert await pm._resolve_ids() == ("hh-2", "mid-2")


async def test_discovery_without_a_match_raises():
    pm = _create_powermeter(bridge_id="", sensor_id="")
    pm._session = _FakeSession(responses=[_FakeResponse(200, [])])
    with pytest.raises(ObiApiError):
        await pm._resolve_ids()


async def test_unexpected_bridge_payload_raises():
    pm = _create_powermeter(bridge_id="", sensor_id="")
    pm._session = _FakeSession(responses=[_FakeResponse(200, {"not": "a list"})])
    with pytest.raises(ObiApiError):
        await pm._resolve_ids()


# --- Category G: Live mode on/off -------------------------------------------


async def test_enable_live_mode_sets_short_upload_interval():
    pm = _create_powermeter(live_upload_interval=2)
    pm._session = _FakeSession(responses=[_FakeResponse(200, {"uploadInterval": 2})])
    await pm._enable_live_mode()
    method, url, kwargs = pm._session.calls[-1]
    assert method == "PATCH"
    assert url.endswith("/sensors/sensor-1")
    assert json.loads(kwargs["data"]) == {"id": "sensor-1", "uploadInterval": 2}
    assert pm._live_mode_active is True


async def test_restore_puts_sensor_back_on_the_slow_cadence():
    pm = _create_powermeter(idle_upload_interval=300)
    pm._session = _FakeSession(responses=[_FakeResponse(200, {"uploadInterval": 300})])
    pm._live_mode_active = True
    await pm._restore_idle_upload_interval()
    assert json.loads(pm._session.calls[-1][2]["data"])["uploadInterval"] == 300
    assert pm._live_mode_active is False


async def test_restore_is_skipped_when_live_mode_never_started():
    pm = _create_powermeter()
    pm._session = _FakeSession()
    await pm._restore_idle_upload_interval()
    assert pm._session.calls == []


async def test_restore_is_skipped_when_idle_interval_is_zero():
    """``IDLE_UPLOAD_INTERVAL = 0`` means "leave the sensor in live mode"."""
    pm = _create_powermeter(idle_upload_interval=0)
    pm._session = _FakeSession()
    pm._live_mode_active = True
    await pm._restore_idle_upload_interval()
    assert pm._session.calls == []


async def test_restore_swallows_api_errors():
    pm = _create_powermeter()
    pm._session = _FakeSession(responses=[_FakeResponse(500, {})])
    pm._live_mode_active = True
    await pm._restore_idle_upload_interval()
    assert pm._live_mode_active is False


# --- Category H: Watchdog ---------------------------------------------------


async def test_watchdog_reasserts_live_mode_then_reconnects():
    """A silent stream first gets live mode re-requested; a second silent
    window means the socket itself is dead, so force a reconnect."""
    pm = _create_powermeter(watchdog_timeout_seconds=1.0)
    ws = AsyncMock()

    async def _always_time_out(awaitable, timeout=None):
        awaitable.close()
        raise asyncio.TimeoutError

    with (
        patch.object(pm, "_enable_live_mode", new_callable=AsyncMock) as enable,
        patch("asyncio.wait_for", new=_always_time_out),
    ):
        await pm._live_watchdog(ws)
    enable.assert_awaited_once()
    ws.close.assert_awaited_once()


# --- Category I: Lifecycle --------------------------------------------------


async def test_start_creates_session_and_task():
    pm = _create_powermeter()
    with patch.object(pm, "_ws_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = None
        await pm.start()
        assert pm._session is not None
        assert pm._ws_task is not None
        await pm.stop()
    assert pm._session is None


async def test_stop_restores_the_upload_interval():
    pm = _create_powermeter()
    with patch.object(pm, "_ws_loop", new_callable=AsyncMock):
        await pm.start()
        session = _FakeSession(responses=[_FakeResponse(200, {"uploadInterval": 300})])
        pm._session = session
        pm._live_mode_active = True
        await pm.stop()
    assert json.loads(session.calls[-1][2]["data"])["uploadInterval"] == 300
    assert session.closed is True


async def test_reconnect_backoff_grows_while_the_cloud_is_unreachable():
    """Each retry costs a login against OBI's servers, so a persistent
    failure must back off instead of retrying every few seconds."""
    pm = _create_powermeter()
    sleeps: list[float] = []

    def _fail_connect(*args, **kwargs):
        raise OSError("cloud unreachable")

    async def _record_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    with (
        patch.object(pm, "_enable_live_mode", new_callable=AsyncMock),
        patch.object(pm, "_ensure_token", new_callable=AsyncMock),
        patch("asyncio.sleep", new=_record_sleep),
    ):
        pm._session = _FakeSession()
        pm._session.ws_connect = _fail_connect
        with pytest.raises(asyncio.CancelledError):
            await pm._ws_loop()
    assert sleeps == [5.0, 10.0, 20.0]


async def test_ws_loop_drops_the_token_on_a_rejected_handshake():
    pm = _create_powermeter()
    pm._token = "stale"
    pm._token_obtained_at = 0.0

    def _fail_connect(*args, **kwargs):
        # aiohttp's ws_connect returns a context manager, not a coroutine, so
        # a rejected handshake surfaces as soon as it is called.
        raise aiohttp.WSServerHandshakeError(None, (), status=401, message="nope")

    with (
        patch.object(pm, "_enable_live_mode", new_callable=AsyncMock),
        patch.object(pm, "_ensure_token", new_callable=AsyncMock) as ensure,
    ):
        ensure.return_value = "stale"
        pm._session = _FakeSession()
        pm._session.ws_connect = _fail_connect
        task = asyncio.create_task(pm._ws_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pm._token is None
    assert pm._connected is False
