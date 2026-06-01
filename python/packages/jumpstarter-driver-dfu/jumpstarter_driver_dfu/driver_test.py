import asyncio

import pytest

from .driver import Dfu
from jumpstarter.common.exceptions import ConfigurationError
from jumpstarter.common.utils import serve


def test_drivers_dfu_state():
    """Driver instantiates and exposes a working state interface."""
    instance = Dfu()
    with serve(instance) as client:
        assert client.state().ok
        _ = client.snapshot()


def test_build_match_args_empty():
    d = Dfu()
    assert d._build_match_args(None, None, None) == []


def test_build_match_args_config_defaults():
    d = Dfu(vid_pid="0483:df11", serial="ABC", intf="0")
    assert d._build_match_args(None, None, None) == [
        "-d",
        "0483:df11",
        "-S",
        "ABC",
        "-i",
        "0",
    ]


def test_build_match_args_per_call_override():
    d = Dfu(vid_pid="0483:df11", serial="ABC", intf="0")
    assert d._build_match_args("1234:5678", "XYZ", "1") == [
        "-d",
        "1234:5678",
        "-S",
        "XYZ",
        "-i",
        "1",
    ]


def test_parse_list_empty():
    assert Dfu._parse_list("") == []
    assert Dfu._parse_list("dfu-util 0.11\n\nNo DFU capable USB device available\n") == []


def test_parse_list_single_device():
    sample = (
        "Found DFU: [0483:df11] ver=0200, devnum=18, cfg=1, intf=0, "
        'path="3-1", alt=1, name="@Internal Flash  /0x08000000/04*016Kg", '
        'serial="3271334D3038"\n'
    )
    devices = Dfu._parse_list(sample)
    assert len(devices) == 1
    d = devices[0]
    assert d["mode"] == "DFU"
    assert d["vid"] == "0483"
    assert d["pid"] == "df11"
    assert d["alt"] == "1"
    assert d["intf"] == "0"
    assert d["path"] == "3-1"
    assert d["serial"] == "3271334D3038"
    assert d["name"] == "@Internal Flash  /0x08000000/04*016Kg"


def test_parse_list_multiple_devices():
    sample = (
        "Found DFU: [0483:df11] ver=0200, devnum=18, cfg=1, intf=0, "
        'path="3-1", alt=0, name="@Flash", serial="ABC"\n'
        "Found DFU: [0483:df11] ver=0200, devnum=18, cfg=1, intf=0, "
        'path="3-1", alt=1, name="@OTP", serial="ABC"\n'
    )
    devices = Dfu._parse_list(sample)
    assert len(devices) == 2
    assert [d["alt"] for d in devices] == ["0", "1"]
    assert all(d["serial"] == "ABC" for d in devices)


def test_parse_list_runtime_mode():
    """dfu-util can also report devices in Runtime mode (not yet in DFU)."""
    sample = 'Found Runtime: [1d6b:0002] ver=0500, devnum=2, cfg=1, intf=0, path="1-2"\n'
    devices = Dfu._parse_list(sample)
    assert len(devices) == 1
    assert devices[0]["mode"] == "Runtime"
    assert devices[0]["vid"] == "1d6b"


def test_device_match_no_filter():
    d = Dfu()
    assert d._device_match({"vid": "0483", "pid": "df11"}) is True


def test_device_match_vid_pid():
    d = Dfu(vid_pid="0483:df11")
    assert d._device_match({"vid": "0483", "pid": "df11"}) is True
    assert d._device_match({"vid": "1234", "pid": "5678"}) is False


def test_device_match_serial():
    d = Dfu(serial="ABC123")
    assert d._device_match({"vid": "0483", "pid": "df11", "serial": "ABC123"}) is True
    assert d._device_match({"vid": "0483", "pid": "df11", "serial": "XYZ"}) is False


class _FakeChild:
    """Minimal stand-in for a child driver used by step dispatch tests."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def on(self):
        self.calls.append(("on", ()))

    def off(self):
        self.calls.append(("off", ()))

    async def cycle(self, wait=2):
        self.calls.append(("cycle", (wait,)))


def test_run_step_call_sync_method():
    d = Dfu()
    child = _FakeChild()
    d.children = {"power": child}
    asyncio.run(d._run_step({"call": "power.on"}))
    asyncio.run(d._run_step({"call": "power.off"}))
    assert child.calls == [("on", ()), ("off", ())]


def test_run_step_call_async_method():
    d = Dfu()
    child = _FakeChild()
    d.children = {"power": child}
    asyncio.run(d._run_step({"call": "power.cycle", "args": [3]}))
    assert child.calls == [("cycle", (3,))]


def test_run_step_sleep_and_log():
    d = Dfu()
    asyncio.run(d._run_step({"sleep": 0.0}))
    asyncio.run(d._run_step({"log": "hello"}))


def test_run_step_unknown_child():
    d = Dfu()
    d.children = {}
    with pytest.raises(ValueError, match="unknown child"):
        asyncio.run(d._run_step({"call": "nope.on"}))


def test_run_step_unknown_method():
    d = Dfu()
    child = _FakeChild()
    d.children = {"power": child}
    with pytest.raises(ValueError, match="no callable method"):
        asyncio.run(d._run_step({"call": "power.no_such_method"}))


def test_run_step_invalid_target():
    d = Dfu()
    with pytest.raises(ValueError, match="<child>.<method>"):
        asyncio.run(d._run_step({"call": "no_dot"}))


def test_run_step_unrecognised():
    d = Dfu()
    with pytest.raises(ValueError, match="Unrecognised step"):
        asyncio.run(d._run_step({"unknown_action": True}))


def test_enter_dfu_no_sequence():
    d = Dfu()
    with pytest.raises(RuntimeError, match="enter_dfu_sequence is empty"):
        asyncio.run(d.enter_dfu())


def test_post_init_rejects_sequence_with_missing_child():
    """Configuring a sequence that references an unwired child fails fast."""
    with pytest.raises(ConfigurationError, match="boot_button"):
        Dfu(enter_dfu_sequence=[{"call": "boot_button.on"}])


def test_post_init_accepts_sequence_with_wired_children():
    """When all referenced children are present, construction succeeds."""
    Dfu(
        children={"power": _FakeChild()},
        enter_dfu_sequence=[{"call": "power.off"}, {"sleep": 0.0}],
    )


def test_post_init_empty_sequence_requires_no_children():
    """Empty sequence: Dfu can be a plain dfu-util wrapper with no children."""
    Dfu()


def test_enter_dfu_runs_steps_without_wait():
    """Sequence executes children calls in order when wait is disabled."""
    child = _FakeChild()
    d = Dfu(
        enter_dfu_sequence=[
            {"call": "power.off"},
            {"sleep": 0.0},
            {"call": "power.on"},
        ],
        enter_dfu_wait=False,
    )
    d.children = {"power": child}
    summary = asyncio.run(d.enter_dfu())
    assert "wait disabled" in summary
    assert child.calls == [("off", ()), ("on", ())]
