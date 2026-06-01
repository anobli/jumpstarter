import sys
from dataclasses import dataclass

import click
from jumpstarter_driver_composite.client import CompositeClient
from jumpstarter_driver_dfu.client import DfuClient


@dataclass(kw_only=True)
class AM62xDfuClient(DfuClient):
    """Client for the AM62xDfu driver preset.

    Inherits the full :class:`DfuClient` surface — ``enter_dfu``,
    ``download_file``, ``detach``, ``list_devices``, ``wait_for_device``,
    plus the DFU CLI. Used directly when the exporter exposes an
    ``AM62xDfu`` driver, and as the ``dfu`` child client of the
    higher-level :class:`AM62xClient`.
    """


@dataclass(kw_only=True)
class AM62xClient(CompositeClient):
    """Client for the AM62x SoC driver.

    AM62x is a composite — DFU operations are accessed through the
    auto-created ``dfu`` child::

        am62x.dfu.enter_dfu()
        am62x.dfu.download_file("u-boot.img", alt=2,
                                dfuse_address="0x80000000")
        am62x.dfu.detach()

    CLI::

        j am62x dfu enter
        j am62x dfu list
        j am62x dfu download FILE --alt 2 --address 0x80000000
        j am62x dfu detach
        j am62x boot-to-fastboot

    SoC-specific commands (added as methods here, paired with @export
    methods on the AM62x driver) live at the top level —
    ``am62x.<method>`` and ``j am62x <subcommand>``.
    """

    def boot_to_fastboot(
        self,
        *,
        fastboot_cmd: str = "fastboot usb 0",
        console_debug: bool = False,
    ) -> None:
        """Bring the DUT all the way to a fastboot session.

        Steps:

        1. Run ``dfu.enter_dfu()`` — strap the SoC into USB ROM boot.
        2. Run ``snagboot.snagrecover()`` with no args, picking up the
           firmware paths configured on the exporter.
        3. ``uboot.attach_to_console()`` to interrupt U-Boot's
           autoboot, then ``send_line(fastboot_cmd)``.

        After the call returns, the DUT is sitting in fastboot mode and
        host-side ``fastboot`` commands can be addressed at it. The
        method does not detect successful USB enumeration of the
        fastboot endpoint — it just sends the U-Boot command and lets
        U-Boot block.

        :param fastboot_cmd: U-Boot command that puts the board into
            fastboot mode (default ``fastboot usb 0``).
        :param console_debug: if True, mirror the U-Boot serial console
            to stdout while interrupting autoboot.
        """
        for required in ("dfu", "snagboot", "uboot"):
            if required not in self.children:
                raise RuntimeError(
                    f"AM62x.boot_to_fastboot requires a '{required}' child; "
                    f"add it to the AM62x's children: in the exporter YAML"
                )

        self.logger.info("Step 1/3: entering DFU mode")
        self.dfu.enter_dfu()

        self.logger.info("Step 2/3: running snagrecover (exporter-local firmware)")
        for chunk in self.snagboot.snagrecover():
            sys.stdout.write(chunk)
            sys.stdout.flush()

        self.logger.info("Step 3/3: starting fastboot via U-Boot")
        with self.uboot.attach_to_console(debug=console_debug):
            # Reset the U-Boot environment to defaults and persist
            # before handing control to fastboot, so the next boot
            # picks up whatever fastboot writes.
            self.uboot.run_command("env default -a -f")
            self.uboot.run_command("saveenv")
            # `fastboot` blocks U-Boot — fire and forget, no prompt
            # comes back.
            self.uboot.send_line(fastboot_cmd)

        self.logger.info("DUT should now be in fastboot mode")

    def cli(self):
        # CompositeClient.cli() builds a click group with each child's
        # CLI added as a sub-command (so `j am62x dfu ...`,
        # `j am62x snagboot ...` work automatically). We extend it with
        # AM62x-specific commands here.
        base = super().cli()

        @base.command(name="boot-to-fastboot")
        @click.option(
            "--fastboot-cmd",
            default="fastboot usb 0",
            show_default=True,
            help="U-Boot command to start fastboot",
        )
        @click.option(
            "--console-debug",
            is_flag=True,
            help="Mirror the serial console to stdout while interrupting U-Boot",
        )
        def boot_to_fastboot_cmd(fastboot_cmd, console_debug):
            """DFU strap → snagrecover → U-Boot fastboot."""
            self.boot_to_fastboot(
                fastboot_cmd=fastboot_cmd,
                console_debug=console_debug,
            )

        return base
