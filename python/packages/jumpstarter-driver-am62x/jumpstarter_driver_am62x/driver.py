from dataclasses import dataclass, field
from typing import Any

from jumpstarter_driver_dfu.driver import Dfu
from jumpstarter_driver_uboot.driver import UbootConsole

from jumpstarter.driver import Driver

# When SYSBOOTn pins select USB boot, the AM62x ROM enumerates as a DFU
# device. The TI ROM uses VID:PID 0451:6165 in this state.
AM62X_DFU_VID_PID = "0451:6165"

# Default sequence to put the SoC into USB ROM boot mode:
#   power off → assert SYSBOOT strap → power on → release strap.
# Boards with different button polarity / wiring can override this in
# the exporter YAML by setting `enter_dfu_sequence`.
AM62X_DFU_SEQUENCE: list[dict[str, Any]] = [
    {"log": "AM62x: entering USB ROM boot mode"},
    {"call": "power.off"},
    {"call": "boot_button.on"},  # assert SYSBOOT for USB boot
    {"sleep": 0.2},
    {"call": "power.on"},  # ROM enumerates USB DFU here
    {"sleep": 1.5},
    {"call": "boot_button.off"},  # release strap
]


@dataclass(kw_only=True)
class AM62xDfu(Dfu):
    """DFU driver preset for TI AM62x SoCs.

    Inherits everything from :class:`Dfu` and only overrides the
    AM62x-specific defaults: the ROM USB ``vid_pid`` and the
    ``enter_dfu_sequence`` that puts the SoC into USB ROM boot mode.
    With the default sequence, the exporter must wire ``power`` and
    ``boot_button`` children; this is validated by the base class.

    Reusable directly as a DFU driver, but most users go through the
    higher-level :class:`AM62x` composite which auto-creates an
    ``AM62xDfu`` child.
    """

    vid_pid: str | None = AM62X_DFU_VID_PID

    enter_dfu_sequence: list[dict[str, Any]] = field(default_factory=lambda: list(AM62X_DFU_SEQUENCE))

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_am62x.client.AM62xDfuClient"


@dataclass(kw_only=True)
class AM62x(Driver):
    """SoC-level driver for TI AM62x.

    A thin composite that auto-creates a ``dfu`` child of type
    :class:`AM62xDfu` and forwards the user-wired children to it.
    The exporter only has to wire ``power`` and ``boot_button`` as
    children of the AM62x driver; the auto-created ``dfu`` child reuses
    them.

    On the client this exposes the DFU surface as a sub-namespace —
    DFU calls go through ``am62x.dfu.<method>`` and the CLI is
    ``j am62x dfu <subcommand>``. SoC-specific commands added as
    ``@export`` methods on this class live at the top level
    (``am62x.<method>`` / ``j am62x <subcommand>``).

    To override AM62xDfu defaults beyond what AM62x exposes, supply an
    explicit ``dfu:`` child in the exporter YAML — auto-creation is
    skipped when a ``dfu`` child is already present.
    """

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        # Auto-create a `uboot` child whenever a `serial` child is
        # wired — `boot_to_fastboot` needs it to drive U-Boot. Skipped
        # silently when there's no serial (other AM62x flows don't
        # require it).
        # AM62x's stock U-Boot uses any-key autoboot interrupt; pick
        # space here because it's harmless if unrecognised but does the
        # job on AM62x. Override via an explicit `uboot:` child if a
        # specific board needs ESC or a different char.
        if "uboot" not in self.children and "serial" in self.children:
            self.children["uboot"] = UbootConsole(
                interrupt_char=" ",
                children={
                    "power": self.children["power"],
                    "serial": self.children["serial"],
                },
            )
        if "dfu" not in self.children:
            # Forward whatever children the exporter wired (typically
            # power + boot_button) to the auto-created AM62xDfu child.
            # AM62xDfu's __post_init__ validates that the sequence's
            # references all resolve.
            self.children["dfu"] = AM62xDfu(
                children=dict(self.children),
            )

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_am62x.client.AM62xClient"

    # SoC-specific @export methods go here.
