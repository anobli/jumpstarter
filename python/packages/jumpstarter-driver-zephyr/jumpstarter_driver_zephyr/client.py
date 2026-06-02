# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import click
from anyio.abc import ObjectStream
from anyio.streams.file import FileReadStream
from jumpstarter_driver_pyserial.client import PySerialClient

from .driver import ARTIFACT_FILES
from jumpstarter.client import DriverClient
from jumpstarter.client.adapters import blocking
from jumpstarter.client.decorators import driver_click_group


@dataclass(frozen=True, kw_only=True, slots=True)
class _FileUploadStream(ObjectStream[bytes]):
    """Minimal ObjectStream that streams a local file to the exporter.

    ``resource_async`` forwards the resource stream in both directions, so the
    stream must expose ``send`` as well as ``receive``. For a one-way upload the
    exporter never sends data back, so ``send``/``send_eof`` are no-ops; only
    ``receive`` (reading the file) and ``aclose`` do real work. This is the
    opendal-free equivalent of ``jumpstarter_driver_opendal``'s
    ``AsyncFileStream`` — keeping it local means this driver doesn't depend on
    opendal (and can't be broken by an opendal version skew in the client env).
    """

    file: FileReadStream

    async def receive(self) -> bytes:
        # FileReadStream.receive() raises EndOfStream at EOF, which the
        # forwarding machinery treats as a clean end of the upload.
        return await self.file.receive()

    async def send(self, item: bytes) -> None:
        raise NotImplementedError("upload stream is read-only")

    async def send_eof(self) -> None:
        pass

    async def aclose(self) -> None:
        await self.file.aclose()


@blocking
@asynccontextmanager
async def _file_resource(*, client: DriverClient, path: str):
    """Expose a local file as a streaming resource handle for ``streamingcall``.

    Mirrors ``OpendalAdapter`` for the plain-filesystem case without pulling in
    opendal: open the file, hand it to ``resource_async``, and yield the handle.
    """
    async with await FileReadStream.from_path(path) as file:
        async with client.resource_async(_FileUploadStream(file=file)) as handle:
            yield handle


@dataclass(kw_only=True)
class ZephyrClient(DriverClient):
    """Client for flashing Zephyr firmware on a remote exporter.

    Collects the fixed-name firmware artifacts from a Zephyr build directory and
    streams them to the exporter, which runs its configured flash command. The
    client knows nothing about the probe or flash command — that lives in the
    exporter's driver config.

    Designed to back twister's ``--flash-command``: :meth:`twister` points
    twister at ``jmp_flash_wrapper.sh``, which calls ``j zephyr flash`` (i.e.
    :meth:`flash_build_dir`) against the lease the surrounding ``jmp shell``
    established.
    """

    def flash(self, path: str) -> Iterator[str]:
        """Stream a firmware tar archive to the exporter and flash it.

        Args:
            path: Path to the tar archive of firmware artifacts.

        Yields:
            Command output lines, in real time.
        """
        # The adapter must stay open for the whole streaming call so the exporter
        # can pull the archive; holding the context across ``yield from`` keeps it
        # alive until the generator is exhausted.
        with _file_resource(client=self, path=path) as handle:
            yield from self.streamingcall("flash", handle)

    def flash_build_dir(
        self,
        build_dir: str,
        board_id: str | None = None,
    ) -> Iterator[str]:
        """Flash firmware from a local Zephyr build directory.

        Collects the fixed-name artifacts (``zephyr.hex`` and, when present,
        ``zephyr.bin`` / ``zephyr.elf``) from ``<build_dir>/zephyr/``, tars them,
        and streams them to the exporter.

        Args:
            build_dir: Local Zephyr build directory (the ``--build-dir`` twister
                passes; artifacts live under its ``zephyr/`` subdir).
            board_id: Optional board/probe id (forwarded by twister). Unused for
                selector-based leases where one lease maps to one board; accepted
                so the call signature matches twister's flash-command contract.

        Yields:
            Command output lines, in real time.
        """
        zephyr_dir = Path(build_dir) / "zephyr"
        artifacts = [(name, zephyr_dir / name) for name in ARTIFACT_FILES.values() if (zephyr_dir / name).is_file()]
        if not artifacts:
            raise FileNotFoundError(
                f"No firmware artifacts found under {zephyr_dir} "
                f"(expected one of {list(ARTIFACT_FILES.values())}). "
                "Is build_dir a Zephyr build directory?"
            )

        if board_id:
            yield f"board-id: {board_id} (informational; lease selects the board)"
        yield f"Uploading: {', '.join(name for name, _ in artifacts)}"

        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(tmp.name, "w") as tar:
                for name, path in artifacts:
                    tar.add(path, arcname=name)
            absolute = Path(tmp.name).resolve()
            yield from self.flash(str(absolute))

    def get_serial_clients(self):
        """Return the PySerialClient children of this driver client.

        Returns:
            The PySerialClient children, which can be used to stream twister
            output from the exporter.
        """
        return [child for child in self.children.values() if isinstance(child, PySerialClient)]

    def _flash_command(self) -> str:
        """Build twister's --flash-command value."""
        script_dir = Path(__file__).parent.resolve()
        bash_script = script_dir / "jmp_flash_wrapper.sh"

        return str(bash_script)

    def _twister_argv(
        self,
        platform: str,
        tty: str,
        test_roots: list[str],
        twister_args: list[str] | None = None,
    ) -> list[str]:
        """Build the twister command line.

        Args:
            platform: Twister platform name (passed via ``-p``).
            tty: Serial device twister talks to (``--device-serial``).
            test_roots: Test root paths (each passed via ``-T``).
            twister_args: Extra arguments appended verbatim to the twister
                command line (e.g. ``["-x", "my-fixture"]``).
        """
        argv = [
            "west",
            "twister",
            "--test-only",
            "--device-testing",
            "--device-serial",
            tty,
            "--flash-command",
            self._flash_command(),
        ]
        argv += ["-p", platform]
        # Test paths are relative to current directory
        for test_path in test_roots:
            argv += ["-T", test_path]
        argv += twister_args or []
        return argv

    def _run_twister(self, argv: list[str], twister_out) -> int:
        """Run twister."""
        twister_running_dir = Path(twister_out).parent
        quoted = " ".join(shlex.quote(a) for a in argv)
        print("Running:", quoted, flush=True)
        return subprocess.call(argv, cwd=twister_running_dir)

    def twister(
        self,
        platform: str,
        twister_out: str,
        test_roots: list[str],
        twister_args: list[str] | None = None,
    ) -> None:
        """Run twister to test Zephyr firmware on the exporter.

        Twister runs as a subprocess with stdout/stderr inherited, so its output
        streams directly to the terminal.

        Args:
            platform: Twister platform name (e.g. ``qemu_x86``).
            twister_out: Path to the twister output directory; its parent is used
                as twister's working directory.
            test_roots: One or more test root paths, passed to twister via ``-T``.
            twister_args: Extra arguments appended verbatim to the twister command
                line.
        """

        serials = self.get_serial_clients()
        if not serials:
            raise RuntimeError("No PySerialClient children found for twister output streaming")
        if len(serials) > 1:
            # Not supported yet
            raise RuntimeError("Multiple PySerialClient children support is not implemented yet")

        tty = os.path.join(tempfile.gettempdir(), f"jmp-tty.{os.getpid()}")
        argv = self._twister_argv(platform, tty, test_roots, twister_args)

        with serials[0].pty(symlink_path=tty):
            rc = self._run_twister(argv, twister_out)
            print(f"twister exited with {rc}", flush=True)

    def cli(self):
        @driver_click_group(self)
        def base():
            """Flash Zephyr firmware on the exporter."""
            pass

        @base.command()
        @click.option(
            "--build-dir",
            required=True,
            type=click.Path(exists=True, file_okay=False, dir_okay=True),
            help="Zephyr build directory (artifacts read from its zephyr/ subdir).",
        )
        @click.option(
            "--board-id",
            default=None,
            help="Board/probe id (forwarded by twister; informational here).",
        )
        def flash(build_dir, board_id):
            """Flash firmware from a Zephyr build directory."""
            self.logger.info("Flashing from build directory %s...", build_dir)
            _stream_to_stdout(self.flash_build_dir(build_dir, board_id=board_id))

        @base.command()
        @click.option(
            "--twister-out",
            required=True,
            help="Path to twister prebuilt tests",
        )
        @click.option(
            "--platform",
            required=True,
            help="Twister platform name (e.g. qemu_x86)",
        )
        @click.option(
            "-T",
            "--test-root",
            "test_roots",
            multiple=True,
            required=True,
            help="Test root path (can be specified multiple times)",
        )
        @click.option(
            "--twister-args",
            multiple=True,
            callback=_split_comma,
            help="Extra arguments appended to the twister command line, "
            'comma-separated and repeatable (e.g. --twister-args "-x,my-fixture" '
            '--twister-args "--pytest-args,--foo=bar").',
        )
        def twister(platform, twister_out, test_roots, twister_args):
            """Run twister to test Zephyr firmware on the exporter."""
            self.logger.info("Running twister...")
            self.twister(platform, twister_out, test_roots, twister_args)

        return base


def _split_comma(ctx, param, value: tuple[str, ...]) -> list[str]:
    """Click callback: flatten repeatable, comma-separated option values.

    With ``multiple=True`` click passes ``value`` as a tuple holding one string
    per option occurrence. Each is split on commas and the pieces are
    concatenated in order, so
    ``--twister-args "-x,fixture" --twister-args "--pytest-args,--foo=bar"``
    yields ``["-x", "fixture", "--pytest-args", "--foo=bar"]``. Empty fields are
    dropped so a trailing comma or empty value contributes no arguments.
    """
    result: list[str] = []
    for occurrence in value:
        result.extend(item for item in occurrence.split(",") if item)
    return result


def _stream_to_stdout(lines: Iterator[str]) -> None:
    """Print each line as it arrives, flushing so the user sees live progress."""
    for line in lines:
        click.echo(line)
        sys.stdout.flush()
