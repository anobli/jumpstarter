import json
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click
from jumpstarter_driver_opendal.adapter import OpendalAdapter
from opendal import Operator

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group


@dataclass(kw_only=True)
class OpenOCDClient(DriverClient):
    """Client for the OpenOCD driver.

    Flash workflow: caller hands over a directory holding everything
    the openocd command will reference (image + any board ``.cfg``/
    ``.tcl``) plus the openocd args, using ``{bundle}`` as a placeholder
    for the future on-exporter location.

    Example:

        result = client.flash_dir(
            "build/zephyr/openocd-bundle",
            args=[
                "-f", "{bundle}/openocd.cfg",
                "-c", "init; targets; reset halt;"
                      " flash write_image erase {bundle}/zephyr.hex;"
                      " reset run; exit",
            ],
        )
    """

    def flash_tar(self, tar_path: str, args: list[str]) -> dict:
        """Flash using a pre-built tar bundle on the local filesystem."""
        absolute = Path(tar_path).resolve()
        with OpendalAdapter(
            client=self,
            operator=Operator("fs", root="/"),
            path=str(absolute),
        ) as handle:
            return self.call("flash", handle, args)

    def flash_dir(self, dir_path: str, args: list[str]) -> dict:
        """Tar a directory tree and flash it as a bundle.

        Members are stored relative to ``dir_path`` so that
        ``{bundle}/<member>`` resolves to the corresponding file on the
        exporter side.
        """
        src = Path(dir_path).resolve()
        if not src.is_dir():
            raise click.UsageError(f"Bundle path is not a directory: {src}")

        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tar_path = tmp.name
        try:
            with tarfile.open(tar_path, "w") as tf:
                for child in sorted(src.iterdir()):
                    tf.add(child, arcname=child.name)
            return self.flash_tar(tar_path, args)
        finally:
            Path(tar_path).unlink(missing_ok=True)

    def cli(self):
        @driver_click_group(self)
        def base():
            """OpenOCD flasher client"""
            pass

        @base.command()
        @click.argument("bundle", type=click.Path(exists=True))
        @click.argument("openocd_args", nargs=-1)
        @click.option(
            "--args-json",
            type=click.Path(exists=True, dir_okay=False),
            default=None,
            help="JSON file containing a list[str] of openocd args. "
            "Use this when args contain shell metacharacters that "
            "would be awkward to escape on the command line.",
        )
        def flash(bundle, openocd_args, args_json):
            """Flash a bundle through openocd.

            BUNDLE is either a directory (tarred on the fly) or a tar
            file. OPENOCD_ARGS are passed verbatim to openocd after
            ``{bundle}`` substitution. Examples:

              j openocd flash ./bundle -- -f '{bundle}/openocd.cfg' \\
                  -c 'init; reset halt; flash write_image erase {bundle}/zephyr.hex; reset run; exit'
            """
            if args_json:
                if openocd_args:
                    raise click.UsageError("Cannot combine --args-json with positional openocd args.")
                args = json.loads(Path(args_json).read_text())
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise click.UsageError("--args-json must hold a JSON list[str].")
            else:
                args = list(openocd_args)

            path = Path(bundle)
            if path.is_dir():
                result = self.flash_dir(str(path), args)
            else:
                result = self.flash_tar(str(path), args)

            sys.stdout.write(result.get("stdout", ""))
            sys.stderr.write(result.get("stderr", ""))
            rc = int(result.get("returncode", -1))
            if rc != 0:
                raise SystemExit(rc)

        return base
