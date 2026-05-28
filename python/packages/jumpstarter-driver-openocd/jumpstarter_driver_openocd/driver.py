import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from anyio.streams.file import FileWriteStream
from anyio.to_thread import run_sync

from jumpstarter.driver import Driver, export


@dataclass(kw_only=True)
class OpenOCD(Driver):
    """OpenOCD flasher driver for Jumpstarter.

    The exporter runs ``openocd`` against an artifact bundle uploaded
    by the client. The client packages everything the openocd command
    needs (firmware image + any board ``.cfg``/``.tcl`` files) into a
    tar archive and passes command-line args with a ``{bundle}``
    placeholder that gets substituted to the extracted bundle
    directory at flash time.

    Trust note: the openocd args and TCL files come from the client.
    openocd TCL can ``exec`` external commands, so the leaseholder
    effectively has command execution on the exporter at openocd's
    privilege level. This matches the trust model of the existing
    probe-rs and shell drivers.
    """

    command_openocd: str = "openocd"
    search_dirs: list[str] = field(default_factory=list)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_openocd.client.OpenOCDClient"

    @export
    async def flash(self, bundle_src: str, args: list[str]) -> dict:
        """Run openocd against a client-supplied bundle.

        Args:
            bundle_src: opendal resource handle to a tar archive.
            args: openocd command-line args. Any occurrence of the
                literal string ``{bundle}`` is replaced with the
                bundle's extraction directory.

        Returns:
            dict with keys ``returncode``, ``stdout``, ``stderr``.
        """
        with tempfile.TemporaryDirectory(prefix="jmp-openocd-") as workdir:
            workdir_path = Path(workdir)
            tar_path = workdir_path / "bundle.tar"
            bundle_dir = workdir_path / "bundle"
            bundle_dir.mkdir()

            async with await FileWriteStream.from_path(str(tar_path)) as stream:
                async with self.resource(bundle_src) as res:
                    async for chunk in res:
                        await stream.send(chunk)

            with tarfile.open(tar_path) as tf:
                # filter="data" refuses absolute paths, symlinks escaping
                # the dest, device files etc. (PEP 706 / tarfile data
                # filter, available since 3.12; 3.11 has the same kwarg
                # via the backport in 3.11.4+.)
                tf.extractall(bundle_dir, filter="data")

            substituted = [a.replace("{bundle}", str(bundle_dir)) for a in args]

            search_args: list[str] = []
            for s in self.search_dirs:
                search_args += ["-s", s]
            search_args += ["-s", str(bundle_dir)]

            cmd = [self.command_openocd, *search_args, *substituted]
            self.logger.info("openocd: %s", shlex.join(cmd))

            # subprocess.run is blocking; offload so we don't stall the
            # async event loop. openocd flashes can take several seconds.
            result = await run_sync(
                lambda: subprocess.run(
                    cmd,
                    cwd=str(bundle_dir),
                    capture_output=True,
                    text=True,
                )
            )
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
