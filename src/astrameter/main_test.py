import argparse
from ipaddress import IPv4Network

import astrameter.main as main_module
from astrameter.config.config_loader import ClientFilter, new_config_parser
from astrameter.config.ini_config import IniAppConfig
from astrameter.main import _resolve_device_config, _virtual_ct_mac, read_ct_powermeter
from astrameter.powermeter import Powermeter


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


async def test_run_device_wires_ct_compatibility_into_port_2220(monkeypatch):
    captured = {}

    class FakeShelly:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.ct_fallback = kwargs["ct_fallback"]

        async def start(self):
            pass

        async def wait(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(main_module, "Shelly", FakeShelly)
    config = IniAppConfig(new_config_parser())
    pm = _StubPowermeter([-963.0])

    await main_module.run_device(
        "shellypro3em_new",
        config,
        config.general(),
        [(pm, _LOCAL, False)],
        device_id="shellypro3em-ec4609c439c1",
    )

    fallback = captured["ct_fallback"]
    assert captured["udp_port"] == 2220
    assert fallback.udp_port == 2220
    assert fallback.ct_type == "HME-4"
    assert fallback.ct_mac == "ec4609c439c1"
    assert await fallback.before_send(("127.0.0.1", 22222)) == [-963.0, 0, 0]


def test_resolve_device_config_shellypro3em_old_gets_shelly_id():
    device_types, device_ids = _resolve("shellypro3em_old")
    assert device_types == ["shellypro3em_old"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_expands_to_old_and_new():
    device_types, device_ids = _resolve("shellypro3em")
    assert device_types == ["shellypro3em_old", "shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1", "shellypro3em-ec4609c439c1"]
