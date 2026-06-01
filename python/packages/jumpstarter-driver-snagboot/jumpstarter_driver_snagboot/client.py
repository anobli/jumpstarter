import sys
from collections.abc import Generator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import click
from jumpstarter_driver_opendal.adapter import OpendalAdapter
from opendal import Operator

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


@dataclass(kw_only=True)
class SnagbootClient(DriverClient):
    """Client for the snagboot driver.

    Currently exposes ``snagrecover``. Other snagboot commands will be
    added later. The SoC name is taken from the exporter's driver config.
    """

    def snagrecover(
        self,
        firmware: dict[str, str] | None = None,
    ) -> Generator[str, None, None]:
        """Stream-run ``snagrecover`` against the DUT.

        :param firmware: optional mapping of role → local file path. If
            given, each file is uploaded to the exporter and registered
            in the snagrecover YAML config under the corresponding role.
            If omitted (or empty), the exporter's configured ``firmware``
            field is used instead — useful when the firmware lives on
            the exporter as part of its setup.

        Yields chunks of snagrecover's combined output (stdout + stderr)
        as they arrive. Iterate the result to consume the recovery.
        """
        # Local mode: no upload, exporter side picks up its configured firmware.
        if not firmware:
            yield from self.streamingcall("snagrecover")
            return

        # Upload mode: stream each local file to the exporter under a
        # resource handle, and pass the handles by role.
        payload: list[list[str]] = []
        with ExitStack() as stack:
            for role, path in firmware.items():
                # expanduser() first so '~' and '~user' resolve to the
                # actual home directory before we anchor the path against
                # cwd (Path.resolve() leaves '~' literal otherwise).
                absolute = Path(path).expanduser().resolve()
                handle = stack.enter_context(
                    OpendalAdapter(
                        client=self,
                        operator=Operator("fs", root="/"),
                        path=str(absolute),
                    )
                )
                payload.append([role, handle, absolute.name])
            yield from self.streamingcall("snagrecover", payload)

    def cli(self):
        @driver_click_group(self)
        def base():
            """snagboot client (wraps snagrecover/snagflash on the exporter)"""
            pass

        @base.command()
        @click.option(
            "--firmware",
            "-f",
            "firmware",
            multiple=True,
            help="Firmware spec 'role=path'. Repeat for each file "
            "(e.g. -f tiboot3=tiboot3.bin -f tispl=tispl.bin). "
            "If omitted, uses the firmware paths configured on the "
            "exporter (`config.firmware` of the Snagboot driver).",
        )
        def recover(firmware):
            """Run snagrecover. With -f, uploads files; without, uses
            the firmware configured on the exporter."""
            fw: dict[str, str] | None = None
            if firmware:
                fw = {}
                for spec in firmware:
                    if "=" not in spec:
                        raise click.UsageError(f"invalid firmware spec '{spec}': expected 'role=path'")
                    role, path = spec.split("=", 1)
                    role = role.strip()
                    path = path.strip()
                    if not role or not path:
                        raise click.UsageError(f"invalid firmware spec '{spec}': role and path required")
                    if role in fw:
                        raise click.UsageError(f"duplicate role '{role}'")
                    fw[role] = path

            for chunk in self.snagrecover(fw):
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")

        return base
