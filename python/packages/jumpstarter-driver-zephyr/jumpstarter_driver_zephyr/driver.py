# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

import asyncio
import asyncio.subprocess
import os
import tarfile
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import AsyncGenerator

from anyio.streams.file import FileWriteStream

from jumpstarter.driver import Driver, export

# Fixed-name firmware artifacts the client uploads, keyed by the token exposed
# to the flash command. Order matters only for logging.
ARTIFACT_FILES = {
    "hex": "zephyr.hex",
    "bin": "zephyr.bin",
    "elf": "zephyr.elf",
}


@dataclass(kw_only=True)
class Zephyr(Driver):
    """Flash Zephyr firmware to a board attached to the exporter.

    This is the exporter-side half of the "server-side twister" workflow: the
    test runner (twister) runs on the client/CI machine and only ships the built
    firmware image(s) here; the exporter owns *how* to flash. The flash command
    and the board's probe/target configuration live entirely in this driver's
    config, so the client stays board-agnostic.

    The client uploads a small tar of fixed-name artifacts (``zephyr.hex`` and,
    if present, ``zephyr.bin`` / ``zephyr.elf``). The driver extracts them into a
    temporary directory and runs ``flash_command`` from that directory.

    The command may reference the uploaded files either by bare name (it runs
    with cwd set to the upload directory) or via the tokens ``{hex}``, ``{bin}``,
    ``{elf}`` and ``{dir}``, which are substituted with absolute paths.

    Example exporter config::

        export:
          flasher:
            type: jumpstarter_driver_zephyr.driver.Zephyr
            config:
              flash_command: >-
                openocd -f interface/cmsis-dap.cfg -f target/cc13x2_cc26x2.cfg
                -c "program {hex} verify reset exit"
    """

    flash_command: str
    """Shell command used to flash the board (required).

    Run via ``bash -c`` with cwd set to the directory holding the uploaded
    firmware. Supports the ``{hex}``/``{bin}``/``{elf}``/``{dir}`` tokens
    described above. The board's probe/target configuration belongs here, not on
    the client.
    """

    flash_timeout: int = 300
    """Seconds before the flash command is killed (via ``timeout``). 0 disables."""

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_zephyr.client.ZephyrClient"

    def _resolve_files(self, build_dir: str) -> dict[str, str]:
        """Map artifact tokens to absolute paths for the files that were sent.

        Always includes ``dir`` (the upload directory). Each of hex/bin/elf is
        included only if that file was actually uploaded.
        """
        files: dict[str, str] = {"dir": build_dir}
        for token, name in ARTIFACT_FILES.items():
            path = os.path.join(build_dir, name)
            if os.path.isfile(path):
                files[token] = path
        return files

    def _resolve_command(self, files: dict[str, str]) -> str:
        """Substitute the {hex}/{bin}/{elf}/{dir} tokens in the flash command.

        Uses plain replacement (not str.format) so literal braces in the command
        — e.g. an openocd ``-c`` argument — are left untouched.
        """
        cmd = self.flash_command
        for token in ("hex", "bin", "elf", "dir"):
            cmd = cmd.replace("{" + token + "}", files.get(token, ""))
        return cmd

    async def _stream_cmd(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Run a subprocess and yield merged stdout/stderr line by line.

        Yielding each line as it arrives keeps the gRPC stream active during long
        flashes (so HTTP/2 keepalive doesn't trip) and gives the client a live
        view. Raises RuntimeError on non-zero exit so the failure propagates to
        the client (and therefore to twister's flash step).
        """
        if env is None:
            env = os.environ.copy()

        self.logger.debug("Running command: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            cmd[0],
            *cmd[1:],
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None

        tail: deque[str] = deque(maxlen=50)
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(line)
            yield line

        rc = await process.wait()
        if rc != 0:
            tail_text = "\n".join(tail)
            raise RuntimeError(f"flash command failed (rc={rc}):\n{tail_text}")

    @export
    async def flash(self, src: str) -> AsyncGenerator[str, None]:
        """Flash firmware uploaded as a tar of fixed-name artifacts.

        Args:
            src: Streaming resource handle for the tar archive produced by the
                 client. The archive contains ``zephyr.hex`` and, when present,
                 ``zephyr.bin`` / ``zephyr.elf`` at its top level.

        Yields:
            Output lines from the flash command, in real time.
        """
        with tempfile.TemporaryDirectory(prefix="zephyr-flash-") as build_dir:
            archive_path = os.path.join(build_dir, ".upload.tar")

            async with await FileWriteStream.from_path(archive_path) as stream:
                async with self.resource(src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

            with tarfile.open(archive_path) as tar:
                tar.extractall(build_dir, filter="data")
            os.unlink(archive_path)

            files = self._resolve_files(build_dir)
            present = [name for token, name in ARTIFACT_FILES.items() if token in files]
            if not present:
                raise ValueError(
                    f"No firmware artifacts found in upload (expected one of {list(ARTIFACT_FILES.values())})."
                )
            yield f"Received firmware: {', '.join(present)}"

            resolved = self._resolve_command(files)
            yield f"Flashing with: {resolved}"

            # Wrap the whole `bash -c` in `timeout` as an outer argv (not by
            # prepending to the command string) so the timeout guards the entire
            # command, including compound/multi-statement flash commands.
            cmd = ["bash", "-c", resolved]
            if self.flash_timeout and self.flash_timeout > 0:
                cmd = ["timeout", str(self.flash_timeout), *cmd]

            async for line in self._stream_cmd(cmd, cwd=build_dir):
                yield line
            yield "Flash complete"
