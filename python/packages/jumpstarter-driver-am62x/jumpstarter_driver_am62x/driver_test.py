import pytest

from .driver import AM62X_DFU_SEQUENCE, AM62X_DFU_VID_PID, AM62x, AM62xDfu
from jumpstarter.common.exceptions import ConfigurationError
from jumpstarter.common.utils import serve


class _FakeChild:
    """Minimal stand-in for power/boot_button children."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def on(self):
        self.calls.append(("on", ()))

    def off(self):
        self.calls.append(("off", ()))


# -- AM62xDfu (the DFU preset) ------------------------------------------------


def _wired_dfu(**kwargs):
    return AM62xDfu(
        children={"power": _FakeChild(), "boot_button": _FakeChild()},
        **kwargs,
    )


def test_am62xdfu_defaults_applied():
    """AM62xDfu bakes in the AM62x ROM VID:PID and the default sequence."""
    inst = _wired_dfu()
    assert inst.vid_pid == AM62X_DFU_VID_PID
    assert inst.enter_dfu_sequence == AM62X_DFU_SEQUENCE
    # Subclass should not share the same list instance with the class default.
    assert inst.enter_dfu_sequence is not AM62X_DFU_SEQUENCE


def test_am62xdfu_state():
    with serve(_wired_dfu()) as client:
        assert client.state().ok


def test_am62xdfu_missing_required_child_fails_fast():
    """Default sequence references power+boot_button — missing one is a hard error."""
    with pytest.raises(ConfigurationError, match="boot_button"):
        AM62xDfu(children={"power": _FakeChild()})
    with pytest.raises(ConfigurationError, match="power"):
        AM62xDfu(children={"boot_button": _FakeChild()})


def test_am62xdfu_custom_sequence_validates_its_own_children():
    """Custom sequences still get their child references validated."""
    # Children referenced by sequence are present → ok
    inst = AM62xDfu(
        children={"vbus": _FakeChild()},
        enter_dfu_sequence=[
            {"call": "vbus.off"},
            {"sleep": 0.1},
            {"call": "vbus.on"},
        ],
    )
    assert "vbus" in inst.children
    # Reference to a missing child still trips
    with pytest.raises(ConfigurationError, match="missing_child"):
        AM62xDfu(
            children={"power": _FakeChild(), "boot_button": _FakeChild()},
            enter_dfu_sequence=[{"call": "missing_child.on"}],
        )


# -- AM62x (the SoC composite) ------------------------------------------------


def _wired_soc(**kwargs):
    return AM62x(
        children={"power": _FakeChild(), "boot_button": _FakeChild()},
        **kwargs,
    )


def test_am62x_auto_creates_dfu_child():
    """The SoC composite auto-wires an AM62xDfu instance as the `dfu` child."""
    inst = _wired_soc()
    assert "dfu" in inst.children
    assert isinstance(inst.children["dfu"], AM62xDfu)
    # Forwarded children
    dfu = inst.children["dfu"]
    assert "power" in dfu.children
    assert "boot_button" in dfu.children
    # AM62xDfu defaults are still in effect on the auto-created child
    assert dfu.vid_pid == AM62X_DFU_VID_PID
    assert dfu.enter_dfu_sequence == AM62X_DFU_SEQUENCE


def test_am62x_explicit_dfu_child_skips_autocreate():
    """Supplying an explicit `dfu` child in YAML disables auto-creation."""
    custom_dfu = AM62xDfu(
        children={"power": _FakeChild(), "boot_button": _FakeChild()},
        enter_dfu_timeout=42.0,
    )
    inst = AM62x(
        children={
            "power": _FakeChild(),
            "boot_button": _FakeChild(),
            "dfu": custom_dfu,
        }
    )
    assert inst.children["dfu"] is custom_dfu
    assert inst.children["dfu"].enter_dfu_timeout == 42.0


def test_am62x_missing_required_child_propagates():
    """If the AM62xDfu auto-create gets unwired children, validation still fires."""
    with pytest.raises(ConfigurationError, match="boot_button"):
        AM62x(children={"power": _FakeChild()})


def test_am62x_state():
    """The composite serves and exposes the dfu child to the client."""
    with serve(_wired_soc()) as client:
        assert client.state().ok
        # CompositeClient.__getattr__ exposes children by name
        assert client.dfu is not None
        assert client.dfu.state().ok
