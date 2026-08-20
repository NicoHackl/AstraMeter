import argparse
import logging
from ipaddress import IPv4Network

import astrameter.main as main_module
from astrameter.config.config_loader import ClientFilter, new_config_parser
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.settings import DEFAULT_CT_UDP_PORT, MarstekSettings
from astrameter.main import _resolve_device_config, _virtual_ct_mac, read_ct_powermeter
from astrameter.powermeter import Powermeter


async def _noop_async(self, *args, **kwargs):
    return None


class _StubPowermeter(Powermeter):
    """Minimal powermeter stub for testing ``read_ct_powermeter``."""

    def __init__(
        self,
        values: list[float],
        wait_raises: BaseException | None = None,
        wait_calls: list[float] | None = None,
    ):
        self._values = values
        self._wait_raises = wait_raises
        self._wait_calls = wait_calls if wait_calls is not None else []

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def get_powermeter_watts_raw(self) -> list[float]:
        return list(self._values)

    async def wait_for_next_message(self, timeout=5):
        self._wait_calls.append(timeout)
        if self._wait_raises is not None:
            raise self._wait_raises


_LOCAL = ClientFilter([IPv4Network("127.0.0.1/32")])


async def test_read_ct_powermeter_returns_none_when_no_match():
    pm = _StubPowermeter([10.0])
    powermeters = [(pm, _LOCAL, True)]
    assert await read_ct_powermeter(("10.0.0.1", 0), powermeters) is None


async def test_read_ct_powermeter_pads_to_three_phases():
    pm = _StubPowermeter([42.0])
    powermeters = [(pm, _LOCAL, False)]
    assert await read_ct_powermeter(("127.0.0.1", 0), powermeters) == [42.0, 0, 0]


async def test_read_ct_powermeter_skips_wait_when_disabled():
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [(pm, _LOCAL, False)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [1.0, 2.0, 3.0]
    assert pm._wait_calls == []


async def test_read_ct_powermeter_calls_wait_with_2s_when_enabled():
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [(pm, _LOCAL, True)]
    await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert pm._wait_calls == [2]


async def test_stub_powermeter_raw_matches_watts():
    pm = _StubPowermeter([3.0, 4.0, 5.0])
    assert (
        await pm.get_powermeter_watts_raw()
        == await pm.get_powermeter_watts()
        == [
            3.0,
            4.0,
            5.0,
        ]
    )


async def test_read_ct_powermeter_swallows_timeout_and_serves_cached():
    """Issue #327: a slow push meter must not break CT002 responses."""
    pm = _StubPowermeter(
        [11.0, 22.0, 33.0],
        wait_raises=TimeoutError("simulated slow meter"),
    )
    powermeters = [(pm, _LOCAL, True)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [11.0, 22.0, 33.0]


def _resolve(device_type: str) -> tuple[list[str], list[str]]:
    cfg = new_config_parser()
    cfg.add_section("GENERAL")
    cfg.set("GENERAL", "DEVICE_TYPE", device_type)
    config = IniAppConfig(cfg)
    args = argparse.Namespace(
        device_types=None, skip_powermeter_test=None, device_ids=None
    )
    device_types, device_ids, _ = _resolve_device_config(config, config.general(), args)
    return device_types, device_ids


def test_resolve_device_config_shellypro3em_new_gets_shelly_id():
    """Issue #389: explicit shellypro3em_new must use a shellypro3em-* id."""
    device_types, device_ids = _resolve("shellypro3em_new")
    assert device_types == ["shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_shelly_id_suffix_is_reused_as_virtual_ct_mac():
    assert _virtual_ct_mac("shellypro3em-ec4609c439c1") == "ec4609c439c1"


def test_custom_shelly_id_gets_stable_locally_administered_ct_mac():
    first = _virtual_ct_mac("my-venus-meter")
    assert first == _virtual_ct_mac("my-venus-meter")
    assert len(first) == 12
    assert int(first[:2], 16) & 0x03 == 0x02


class _FakeShelly:
    """Stand-in for the Shelly listener, capturing what run_device built."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.ct_fallback = kwargs.get("ct_fallback")
        self.udp_port = kwargs.get("udp_port", 0)

    async def start(self):
        pass

    async def wait(self):
        pass

    async def stop(self):
        pass


def _fake_shelly_factory(captured):
    class FakeShelly(_FakeShelly):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)
            captured["device"] = self

    return FakeShelly


_DISCOVERY_REQUEST = ["HHM-2", "ccc837b413f5", "", "000000000000", "0", "0"]


async def test_run_device_wires_ct_compatibility_into_port_2220(monkeypatch):
    captured: dict = {}
    ct_started: list[int] = []

    monkeypatch.setattr(main_module, "Shelly", _fake_shelly_factory(captured))
    config = IniAppConfig(new_config_parser())
    pm = _StubPowermeter([-963.0])

    async def fake_ct_start(self):
        ct_started.append(self.udp_port)

    monkeypatch.setattr(main_module.CT002, "start", fake_ct_start)
    monkeypatch.setattr(main_module.CT002, "stop", _noop_async)

    await main_module.run_device(
        "shellypro3em_new",
        config,
        config.general(),
        [(pm, _LOCAL, False)],
        device_id="shellypro3em-ec4609c439c1",
    )

    fallback = captured["ct_fallback"]
    assert captured["udp_port"] == 2220
    assert fallback.ct_type == "HME-4"
    # The Shelly port carries discovery; the CT port carries a paired battery,
    # so the delegate binds it too.
    assert fallback.udp_port == DEFAULT_CT_UDP_PORT
    assert ct_started == [DEFAULT_CT_UDP_PORT]
    # No CT_MAC: stay permissive so a battery paired with any CT is answered.
    assert fallback.ct_mac == ""
    assert fallback.ct_mac_advertise == "ec4609c439c1"
    selected_mac = "02b250a1b2c3"
    selected_request = ["HHM-2", "ccc837b413f5", "HME-4", selected_mac, "D", "0"]
    assert fallback._validate_ct_mac(selected_request)
    assert (
        fallback._build_response_fields(selected_request, [1, 2, 3])[1] == selected_mac
    )
    # A discovery probe never gets the all-zero wildcard back.
    assert (
        fallback._build_response_fields(_DISCOVERY_REQUEST, [1, 2, 3])[1]
        == "ec4609c439c1"
    )
    assert await fallback.before_send(("127.0.0.1", 22222)) == [-963.0, 0, 0]


async def test_run_device_advertises_registered_ct_mac_for_venus_fallback(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(main_module, "Shelly", _fake_shelly_factory(captured))
    monkeypatch.setattr(main_module.CT002, "start", _noop_async)
    monkeypatch.setattr(main_module.CT002, "stop", _noop_async)
    config = IniAppConfig(new_config_parser())

    await main_module.run_device(
        "shellypro3em_new",
        config,
        config.general(),
        [(_StubPowermeter([0.0]), _LOCAL, False)],
        device_id="shellypro3em-ec4609c439c1",
        marstek_mac="02b250a1b2c3",
    )

    fallback = captured["ct_fallback"]
    assert fallback.ct_mac_advertise == "02b250a1b2c3"
    assert (
        fallback._build_response_fields(_DISCOVERY_REQUEST, [1, 2, 3])[1]
        == "02b250a1b2c3"
    )
    # Dropping the one-time Marstek credentials must not orphan the battery
    # that was paired while they were set, so the gate stays open.
    assert fallback.ct_mac == ""
    assert fallback._validate_ct_mac(
        ["HHM-2", "ccc837b413f5", "HME-4", "02b250ffffff", "D", "0"]
    )


async def test_run_device_survives_a_taken_ct_port(monkeypatch, caplog):
    """A ct002 device type already owning the CT port must not kill Shelly."""
    captured: dict = {}

    monkeypatch.setattr(main_module, "Shelly", _fake_shelly_factory(captured))

    async def refuse(self):
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(main_module.CT002, "start", refuse)
    monkeypatch.setattr(main_module.CT002, "stop", _noop_async)
    config = IniAppConfig(new_config_parser())

    with caplog.at_level(logging.WARNING):
        await main_module.run_device(
            "shellypro3em_new",
            config,
            config.general(),
            [(_StubPowermeter([0.0]), _LOCAL, False)],
            device_id="shellypro3em-ec4609c439c1",
        )

    assert captured["ct_fallback"] is not None
    assert "could not also serve the CT port" in caplog.text


async def test_run_device_advertises_registered_ct_mac_for_ct002(monkeypatch):
    """The registered identity is what a CT002 discovery probe gets told."""
    built: list = []

    real_build = main_module._build_ct002

    def spy(*args, **kwargs):
        device = real_build(*args, **kwargs)
        built.append(device)
        return device

    monkeypatch.setattr(main_module, "_build_ct002", spy)
    monkeypatch.setattr(main_module.CT002, "start", _noop_async)
    monkeypatch.setattr(main_module.CT002, "stop", _noop_async)
    monkeypatch.setattr(main_module.CT002, "wait", _noop_async)
    config = IniAppConfig(new_config_parser())

    await main_module.run_device(
        "ct002",
        config,
        config.general(),
        [(_StubPowermeter([0.0]), _LOCAL, False)],
        device_id="ct002-1",
        marstek_mac="02b250a1b2c3",
    )

    device = built[0]
    # CT_MAC unset, so the gate stays open and only the advertised identity
    # names the CT.
    assert device.ct_mac == ""
    assert device.ct_mac_advertise == "02b250a1b2c3"
    assert (
        device._build_response_fields(_DISCOVERY_REQUEST, [1, 2, 3])[1]
        == "02b250a1b2c3"
    )


def test_managed_marstek_registers_hme4_for_venus_fallback(monkeypatch):
    calls = []

    def fake_ensure(_config, device_type):
        calls.append(device_type)
        return {"mac": "02:B2:50:A1:B2:C3", "version": "121"}

    monkeypatch.setattr(main_module, "ensure_managed_fake_device", fake_ensure)
    settings = MarstekSettings(enable=True, mailbox="user@example.com", password="pw")

    managed = main_module._build_managed_marstek(settings, ["shellypro3em_new"])

    assert calls == ["ct002"]
    assert managed == {"shellypro3em_new": ("02b250a1b2c3", 121)}


def test_managed_marstek_reuses_one_hme4_for_ct002_and_venus_fallback(monkeypatch):
    calls = []

    def fake_ensure(_config, device_type):
        calls.append(device_type)
        return {"mac": "02b250a1b2c3", "version": 121}

    monkeypatch.setattr(main_module, "ensure_managed_fake_device", fake_ensure)
    settings = MarstekSettings(enable=True, mailbox="user@example.com", password="pw")

    managed = main_module._build_managed_marstek(
        settings, ["ct002", "shellypro3em_new"]
    )

    assert calls == ["ct002"]
    assert managed == {
        "ct002": ("02b250a1b2c3", 121),
        "shellypro3em_new": ("02b250a1b2c3", 121),
    }


def test_resolve_device_config_shellypro3em_old_gets_shelly_id():
    device_types, device_ids = _resolve("shellypro3em_old")
    assert device_types == ["shellypro3em_old"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_expands_to_old_and_new():
    device_types, device_ids = _resolve("shellypro3em")
    assert device_types == ["shellypro3em_old", "shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1", "shellypro3em-ec4609c439c1"]
