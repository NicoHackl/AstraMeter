import dataclasses
import inspect
import json
from ipaddress import IPv4Network

import pytest

from astrameter.config import ClientFilter
from astrameter.ct002 import CT002
from astrameter.ct002.protocol import parse_request
from astrameter.powermeter import Powermeter
from astrameter.shelly.shelly import BATTERY_INACTIVE_TIMEOUT_SECONDS, Shelly


class DummyPowermeter(Powermeter):
    async def get_powermeter_watts(self):
        return [1.0]


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append((data, addr))


REQUEST = json.dumps(
    {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
).encode()

# Captured from a Venus E Mini running firmware V295. Despite probing the
# Shelly Pro 3EM port (2220), HHM-2 sends the native Marstek CT wire format.
HHM2_REQUEST = b"\x01\x0245|HHM-2|ccc837b413f5||000000000000|0|0|\x0377"


def _shelly() -> Shelly:
    cf = ClientFilter([IPv4Network("127.0.0.0/8")])
    return Shelly(
        [(DummyPowermeter(), cf, False)],
        udp_port=1010,
        device_id="test",
        device_type="shellypro3em_old",
    )


def _shelly_with_ct_fallback() -> Shelly:
    ct = CT002(
        udp_port=2220,
        ct_mac="ec4609c439c1",
        ct_type="HME-4",
        device_id="test",
        active_control=False,
    )

    async def reading(_addr, _fields=None, _consumer_id=None):
        return [-963.0, 0.0, 0.0]

    ct.before_send = reading
    return Shelly(
        [],
        udp_port=2220,
        device_id="test",
        device_type="shellypro3em_new",
        ct_fallback=ct,
    )


async def test_hhm2_ct_frame_on_shelly_port_uses_ct002_compatibility_handler():
    shelly = _shelly_with_ct_fallback()
    transport = _FakeTransport()

    await shelly._handle_request(
        transport,
        HHM2_REQUEST,
        ("192.168.10.32", 22222),
    )

    assert len(transport.sent) == 1
    response, addr = transport.sent[0]
    assert addr == ("192.168.10.32", 22222)
    fields, error = parse_request(response)
    assert error is None
    assert fields is not None
    assert fields[:8] == [
        "HME-4",
        "ec4609c439c1",
        "HHM-2",
        "ccc837b413f5",
        "-963",
        "0",
        "0",
        "-963",
    ]

    # Once CT traffic is observed, the dashboard exposes the real CT consumer
    # state rather than an empty Shelly card.
    snapshot = shelly.status_snapshot()
    assert snapshot.device_id == "test"
    assert snapshot.udp_port == 2220
    assert snapshot.consumers[0].consumer_id == "ccc837b413f5"
    assert snapshot.consumers[0].device_type == "HHM-2"

    # The dashboard sees a CT-shaped status document, so its controls must be
    # delegated to the same CT state instead of failing on the Shelly wrapper.
    shelly.set_consumer_manual_target("ccc837b413f5", 125)
    shelly.set_consumer_auto_target("ccc837b413f5", False)
    controlled = shelly.status_snapshot().consumers[0]
    assert controlled.manual_target == 125
    assert controlled.manual_enabled is True


async def test_json_rpc_still_works_when_ct_compatibility_is_enabled():
    shelly = _shelly_with_ct_fallback()
    # The Shelly side still needs a powermeter for ordinary JSON-RPC clients.
    cf = ClientFilter([IPv4Network("127.0.0.0/8")])
    shelly._powermeters = [(DummyPowermeter(), cf, False)]
    transport = _FakeTransport()

    await shelly._handle_request(transport, REQUEST, ("127.0.0.1", 54321))

    assert len(transport.sent) == 1
    response, _ = transport.sent[0]
    assert json.loads(response)["result"]["total_act_power"] == 1.001


def test_status_snapshot_is_not_a_coroutine_function():
    """The builder shares the loop with the UDP handlers; an ``await`` in it
    would tear the snapshot across two polls."""
    assert not inspect.iscoroutinefunction(Shelly.status_snapshot)


async def test_status_snapshot_reports_registered_battery():
    shelly = _shelly()
    transport = _FakeTransport()
    await shelly._handle_request(transport, REQUEST, ("127.0.0.1", 54321))

    snap = shelly.status_snapshot()
    assert snap.device_id == "test"
    assert snap.device_type == "shellypro3em_old"
    assert snap.udp_port == 1010
    assert snap.running is False  # never started
    assert snap.inactive_timeout == BATTERY_INACTIVE_TIMEOUT_SECONDS == 120

    (battery,) = snap.batteries
    assert battery.ip == "127.0.0.1"
    assert battery.last_seen_at > 0
    assert 0 <= battery.last_seen_age < 5
    assert battery.poll_interval is None  # only one poll seen so far
    assert battery.active is True
    assert battery.in_flight is False

    # A second poll establishes the cadence and the parked-handler flag shows.
    await shelly._handle_request(transport, REQUEST, ("127.0.0.1", 54322))
    shelly._inflight_batteries.add("127.0.0.1")
    (battery,) = shelly.status_snapshot().batteries
    assert battery.poll_interval is not None
    assert battery.in_flight is True


async def test_status_snapshot_batteries_sorted_and_marked_inactive():
    shelly = _shelly()
    transport = _FakeTransport()
    for ip in ("127.0.0.2", "127.0.0.1"):
        await shelly._handle_request(transport, REQUEST, (ip, 54321))

    shelly._battery_last_seen["127.0.0.2"] -= BATTERY_INACTIVE_TIMEOUT_SECONDS + 1
    shelly._log_inactive_batteries()

    snap = shelly.status_snapshot()
    assert [b.ip for b in snap.batteries] == ["127.0.0.1", "127.0.0.2"]
    assert [b.active for b in snap.batteries] == [True, False]
    assert snap.batteries[1].last_seen_age > BATTERY_INACTIVE_TIMEOUT_SECONDS


async def test_status_snapshot_is_detached_from_the_device():
    shelly = _shelly()
    transport = _FakeTransport()
    await shelly._handle_request(transport, REQUEST, ("127.0.0.1", 54321))
    snap = shelly.status_snapshot()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.running = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.batteries[0].active = False  # type: ignore[misc]

    # The containers are copies: later device state cannot leak into a
    # snapshot already handed to a request handler, and vice versa.
    await shelly._handle_request(transport, REQUEST, ("127.0.0.2", 54321))
    shelly._inactive_batteries.add("127.0.0.1")
    assert [b.ip for b in snap.batteries] == ["127.0.0.1"]
    assert snap.batteries[0].active is True


async def test_running_tracks_start_and_stop():
    shelly = Shelly([], udp_port=0, device_id="test")
    assert shelly.status_snapshot().started_at is None

    await shelly.start()
    try:
        snap = shelly.status_snapshot()
        assert snap.running is True
        assert snap.started_at is not None
        assert snap.udp_port == shelly.udp_port != 0
    finally:
        await shelly.stop()
    assert shelly.status_snapshot().running is False
