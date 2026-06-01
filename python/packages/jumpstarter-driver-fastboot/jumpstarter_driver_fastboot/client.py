import sys
import tempfile
import zipfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import click
from jumpstarter_driver_opendal.adapter import OpendalAdapter
from opendal import Operator

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


@dataclass(kw_only=True)
class FastbootClient(DriverClient):
    """Client for the fastboot driver.

    Wraps host-side ``fastboot`` invocations. The DUT must already be
    in fastboot mode for any of these to work.
    """

    def devices(self) -> list[dict[str, str]]:
        """List devices currently in fastboot mode visible to the exporter."""
        return self.call("devices")

    def getvar(self, var: str) -> str:
        """``fastboot getvar VAR`` — return the value string (empty if unknown)."""
        return self.call("getvar", var)

    def flash(
        self,
        partition: str,
        file: str | None = None,
    ) -> Generator[str, None, None]:
        """Stream-flash ``partition``.

        :param partition: name of the fastboot partition (``boot``,
            ``super``, ``vbmeta``, …).
        :param file: optional local path to the image. If given, the
            file is uploaded to the exporter and flashed. If omitted,
            the exporter's configured ``partitions[partition]`` is used.
        """
        if file is None:
            yield from self.streamingcall("flash", partition)
            return

        absolute = Path(file).expanduser().resolve()
        with OpendalAdapter(
            client=self,
            operator=Operator("fs", root="/"),
            path=str(absolute),
        ) as handle:
            yield from self.streamingcall("flash", partition, handle, absolute.name)

    def erase(self, partition: str) -> Generator[str, None, None]:
        """``fastboot erase PARTITION``."""
        yield from self.streamingcall("erase", partition)

    def reboot(self, target: str | None = None) -> Generator[str, None, None]:
        """``fastboot reboot [TARGET]`` (``bootloader``/``recovery``/``fastboot``)."""
        yield from self.streamingcall("reboot", target)

    def set_active(self, slot: str) -> Generator[str, None, None]:
        """``fastboot set_active SLOT`` — for A/B partitioned systems."""
        yield from self.streamingcall("set_active", slot)

    def flashall(
        self,
        bundle: str,
        wipe: bool = False,
    ) -> Generator[str, None, None]:
        """Flash multiple partitions from a manifest bundle.

        :param bundle: Path to either:
            - A zip file containing manifest.yaml (or .json) and images
            - A directory containing manifest.yaml (or .json) and images
              (will be auto-compressed to a temporary zip file)
        :param wipe: If True, wipes userdata (``-w`` flag).
        """
        bundle_path = Path(bundle).expanduser().resolve()

        # Check if it's a directory - if so, create a temporary zip
        if bundle_path.is_dir():
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_zip_path = tmp.name

            try:
                # Create zip file from directory contents
                with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for file_path in bundle_path.rglob("*"):
                        if file_path.is_file():
                            arcname = file_path.relative_to(bundle_path)
                            zf.write(file_path, arcname)

                # Upload the temporary zip
                with OpendalAdapter(
                    client=self,
                    operator=Operator("fs", root="/"),
                    path=tmp_zip_path,
                ) as handle:
                    yield from self.streamingcall("flashall", handle, wipe)
            finally:
                # Clean up temporary file
                Path(tmp_zip_path).unlink(missing_ok=True)
        else:
            # It's already a zip file, use it directly
            with OpendalAdapter(
                client=self,
                operator=Operator("fs", root="/"),
                path=str(bundle_path),
            ) as handle:
                yield from self.streamingcall("flashall", handle, wipe)

    @staticmethod
    def _stream_to_stdout(gen):
        """Helper to stream generator output to stdout."""
        for chunk in gen:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        sys.stdout.write("\n")

    def _register_devices_command(self, base):
        """Register the 'devices' command."""

        @base.command()
        def devices():
            """List devices in fastboot mode"""
            ds = self.devices()
            if not ds:
                click.echo("No fastboot devices found")
                return
            for d in ds:
                click.echo(f"{d.get('serial', '?')}\t{d.get('type', '?')}")

    def _register_getvar_command(self, base):
        """Register the 'getvar' command."""

        @base.command()
        @click.argument("var")
        def getvar(var):
            """Query a fastboot variable"""
            click.echo(self.getvar(var))

    def _register_flash_command(self, base):
        """Register the 'flash' command."""

        @base.command()
        @click.argument("partition")
        @click.argument(
            "file",
            required=False,
            type=click.Path(exists=True, dir_okay=False),
        )
        def flash(partition, file):
            """Flash PARTITION with FILE.

            Without FILE, the exporter's configured local image for
            PARTITION is used.
            """
            self._stream_to_stdout(self.flash(partition, file))

    def _register_erase_command(self, base):
        """Register the 'erase' command."""

        @base.command()
        @click.argument("partition")
        def erase(partition):
            """Erase PARTITION"""
            self._stream_to_stdout(self.erase(partition))

    def _register_reboot_command(self, base):
        """Register the 'reboot' command."""

        @base.command()
        @click.argument("target", required=False)
        def reboot(target):
            """Reboot the device (TARGET: bootloader/recovery/fastboot/none)"""
            self._stream_to_stdout(self.reboot(target))

    def _register_set_active_command(self, base):
        """Register the 'set-active' command."""

        @base.command(name="set-active")
        @click.argument("slot")
        def set_active_cmd(slot):
            """Set the active boot slot (A/B systems)"""
            self._stream_to_stdout(self.set_active(slot))

    def _register_flashall_command(self, base):
        """Register the 'flashall' command."""

        @base.command()
        @click.argument("bundle", type=click.Path(exists=True))
        @click.option("--wipe", is_flag=True, help="Wipe userdata (-w flag)")
        def flashall(bundle, wipe):
            """Flash all partitions from a manifest bundle.

            BUNDLE can be either:
              - A zip file containing manifest.yaml/json and images
              - A directory containing manifest.yaml/json and images
                (will be auto-compressed before upload)

            The manifest is a simple partition -> filename mapping:

            \b
              boot_a: boot.img
              boot_b: boot.img
              super: super.img
              userdata: userdata.img
            """
            self._stream_to_stdout(self.flashall(bundle, wipe))

    def cli(self):
        @driver_click_group(self)
        def base():
            """fastboot client (wraps fastboot CLI on the exporter)"""

        self._register_devices_command(base)
        self._register_getvar_command(base)
        self._register_flash_command(base)
        self._register_erase_command(base)
        self._register_reboot_command(base)
        self._register_set_active_command(base)
        self._register_flashall_command(base)
        return base
