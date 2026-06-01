import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from jumpstarter_driver_opendal.adapter import OpendalAdapter
from opendal import Operator

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


@dataclass(kw_only=True)
class DfuClient(DriverClient):
    """Client interface for the DFU driver.

    Sends files to the exporter and runs ``dfu-util`` against a USB
    DUT that is already in DFU mode. Output (including the dfu-util
    progress bar) is streamed back to the caller as it arrives.
    """

    def list_devices(self) -> list[dict[str, Any]]:
        """Return DFU devices currently visible to the exporter."""
        return self.call("list_devices")

    def enter_dfu(self) -> str:
        """Run the configured sequence to put the DUT into DFU mode.

        Requires ``enter_dfu_sequence`` to be set in the exporter config.
        """
        return self.call("enter_dfu")

    def wait_for_device(self, timeout: float = 15.0) -> dict[str, Any]:
        """Wait until a matching device appears in DFU mode."""
        return self.call("wait_for_device", float(timeout))

    def download(
        self,
        operator: Operator,
        path: str,
        *,
        alt: str | int = "0",
        dfuse_address: str | None = None,
        vid_pid: str | None = None,
        serial: str | None = None,
        intf: str | int | None = None,
        transfer_size: int | None = None,
    ) -> Generator[str, None, None]:
        """Flash ``path`` (read via ``operator``) to the DUT.

        Yields chunks of dfu-util's output as they arrive. Iterate the
        result to consume the flash.
        """
        with OpendalAdapter(client=self, operator=operator, path=path) as handle:
            yield from self.streamingcall(
                "download",
                handle,
                str(alt),
                dfuse_address,
                vid_pid,
                serial,
                None if intf is None else str(intf),
                transfer_size,
            )

    def download_file(
        self,
        file_path: str,
        *,
        alt: str | int = "0",
        dfuse_address: str | None = None,
        vid_pid: str | None = None,
        serial: str | None = None,
        intf: str | int | None = None,
        transfer_size: int | None = None,
    ) -> Generator[str, None, None]:
        """Flash a local file to the DUT, streaming dfu-util output."""
        # expanduser() first so '~' resolves to the user's home directory.
        absolute = Path(file_path).expanduser().resolve()
        yield from self.download(
            operator=Operator("fs", root="/"),
            path=str(absolute),
            alt=alt,
            dfuse_address=dfuse_address,
            vid_pid=vid_pid,
            serial=serial,
            intf=intf,
            transfer_size=transfer_size,
        )

    def detach(
        self,
        *,
        vid_pid: str | None = None,
        serial: str | None = None,
        intf: str | int | None = None,
    ) -> str:
        """Detach the DUT from DFU mode (``dfu-util -e``)."""
        return self.call(
            "detach",
            vid_pid,
            serial,
            None if intf is None else str(intf),
        )

    @staticmethod
    def _add_match_options(f):
        """Apply match options (vid-pid, serial-num, intf) to a Click command."""
        f = click.option("--intf", help="Match device by interface number")(f)
        f = click.option("--serial-num", "serial_num", help="Match device by serial number")(f)
        f = click.option("--vid-pid", help="Match device by VID:PID (e.g. 0483:df11)")(f)
        return f

    def _register_list_command(self, base):
        """Register the 'list' command."""

        @base.command(name="list")
        def list_cmd():
            """List devices in DFU mode visible to the exporter"""
            devices = self.list_devices()
            if not devices:
                click.echo("No DFU devices found")
                return
            for d in devices:
                vp = f"{d.get('vid', '?')}:{d.get('pid', '?')}"
                bits = [f"alt={d.get('alt', '?')}"]
                if d.get("name"):
                    bits.append(f"name={d['name']!r}")
                if d.get("serial"):
                    bits.append(f"serial={d['serial']}")
                if d.get("path"):
                    bits.append(f"path={d['path']}")
                click.echo(f"[{vp}] " + ", ".join(bits))

    def _register_download_command(self, base):
        """Register the 'download' command."""

        @base.command()
        @click.argument("file", type=click.Path(exists=True, dir_okay=False))
        @click.option("--alt", "-a", default="0", show_default=True, help="DFU alt setting to write to")
        @click.option(
            "--address", "-s", "dfuse_address", help="DfuSe -s argument (e.g. 0x80000000 or 0x80000000:leave)"
        )
        @click.option("--transfer-size", "-t", type=int, help="Transfer size override (-t)")
        @self._add_match_options
        def download(file, alt, dfuse_address, transfer_size, vid_pid, serial_num, intf):
            """Flash FILE to the DUT via DFU"""
            for chunk in self.download_file(
                file,
                alt=alt,
                dfuse_address=dfuse_address,
                vid_pid=vid_pid,
                serial=serial_num,
                intf=intf,
                transfer_size=transfer_size,
            ):
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")

    def _register_detach_command(self, base):
        """Register the 'detach' command."""

        @base.command()
        @self._add_match_options
        def detach_cmd(vid_pid, serial_num, intf):
            """Tell the DUT to leave DFU mode (dfu-util -e)"""
            output = self.detach(vid_pid=vid_pid, serial=serial_num, intf=intf)
            if output:
                click.echo(output)

    def _register_enter_command(self, base):
        """Register the 'enter' command."""

        @base.command(name="enter")
        def enter_cmd():
            """Run the configured sequence to put the DUT into DFU mode"""
            click.echo(self.enter_dfu())

    def _register_wait_command(self, base):
        """Register the 'wait' command."""

        @base.command(name="wait")
        @click.option("--timeout", "-t", type=float, default=15.0, show_default=True, help="Timeout in seconds")
        def wait_cmd(timeout):
            """Wait for a matching device to appear in DFU mode"""
            dev = self.wait_for_device(timeout)
            click.echo(f"Found: {dev}")

    def cli(self):
        @driver_click_group(self)
        def base():
            """DFU client (wraps dfu-util on the exporter)"""

        self._register_list_command(base)
        self._register_download_command(base)
        self._register_detach_command(base)
        self._register_enter_command(base)
        self._register_wait_command(base)
        return base
