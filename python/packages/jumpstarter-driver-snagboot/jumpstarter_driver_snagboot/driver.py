import asyncio
import shlex
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from anyio.streams.file import FileWriteStream

from jumpstarter.driver import Driver, export


@dataclass(kw_only=True)
class Snagboot(Driver):
    """Driver wrapping the `snagboot` tool suite (`snagrecover`,
    `snagflash`, …) on the exporter host.

    Snagboot is a Bootlin tool for SoC USB recovery and flashing
    (STM32MP, TI K3 / AM62x, NXP iMX, RK35xx, …). The DUT must be in
    USB ROM boot mode for `snagrecover` — drive the strap pins through
    a separate driver (e.g. `jumpstarter-driver-dfu` with an
    `enter_dfu_sequence`) before invoking these methods.

    Only `snagrecover` is implemented for now; other commands
    (`snagflash`, …) will follow.
    """

    # SoC name as understood by snagrecover (e.g. ``am62x``,
    # ``stm32mp25``). Per-exporter, not per-call — set it in the
    # exporter YAML rather than passing it on every invocation.
    soc: str | None = None

    snagrecover_path: str = "snagrecover"

    # Optional exporter-local firmware: ``role -> absolute path`` on the
    # exporter filesystem. When set, ``snagrecover_local()`` runs
    # snagrecover without requiring the client to upload anything.
    firmware: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_snagboot.client.SnagbootClient"

    async def _stream(self, cmd: list[str]) -> AsyncGenerator[str, None]:
        """Run a snagboot command and yield combined stdout/stderr chunks."""
        self.logger.info("Running: %s", shlex.join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                yield chunk.decode(errors="replace")
        finally:
            rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"{Path(cmd[0]).name} exited with code {rc}")

    @staticmethod
    def _build_config(firmware_paths: dict[str, str]) -> dict:
        """Assemble the YAML config that `snagrecover -f` consumes.

        It's a flat map of role → ``{path: <file>}`` — the SoC is not
        a key in the file (it's passed via ``-s`` on the CLI)::

            tiboot3:
              path: tiboot3.bin
            tispl:
              path: tispl.bin
            u-boot:
              path: u-boot.img
        """
        return {role: {"path": path} for role, path in firmware_paths.items()}

    async def _run_snagrecover(
        self,
        firmware_paths: dict[str, str],
    ) -> AsyncGenerator[str, None]:
        """Generate the snagrecover YAML and run the tool.

        :param firmware_paths: mapping ``role -> absolute path`` of files
            already on the exporter. The caller is responsible for any
            staging (e.g. uploads from a client).
        """
        if not self.soc:
            raise RuntimeError("Snagboot: 'soc' is not configured. Set `config.soc: <soc_name>` in the exporter YAML.")
        if not firmware_paths:
            raise ValueError("firmware_paths must contain at least one entry")

        with tempfile.TemporaryDirectory(prefix="jumpstarter-snagrecover-") as tmpdir:
            config_path = Path(tmpdir) / "snagrecover.yaml"
            config = self._build_config(firmware_paths)
            with open(config_path, "w") as f:
                yaml.safe_dump(config, f, sort_keys=False)
            self.logger.info("Generated snagrecover config: %s", config)

            # Note: snagrecover's `-f` / `--firmware-file` takes a YAML
            # config file path, while `-F` / `--firmware` takes per-blob
            # Python-literal dicts. We use the file form.
            cmd = [self.snagrecover_path, "-s", self.soc, "-f", str(config_path)]
            async for chunk in self._stream(cmd):
                yield chunk

    @export
    async def snagrecover(
        self,
        firmware: list = None,
    ) -> AsyncGenerator[str, None]:
        """Run `snagrecover` against the DUT.

        Two modes, selected by ``firmware``:

        * **Upload mode** — if ``firmware`` is non-empty, each entry is a
          ``[role, handle, filename]`` triple where ``handle`` is a
          client resource handle and ``filename`` is the basename to
          give the staged file on the exporter. Files are streamed from
          the client into a tempdir and registered in the snagrecover
          YAML config under their roles.
        * **Local mode** — if ``firmware`` is empty/omitted, uses the
          ``firmware`` driver-config field (``role -> absolute path`` on
          the exporter). Useful when firmware is part of exporter setup
          rather than something the client ships on every call.

        Role names (``tiboot3``, ``tispl``, ``u-boot``, ``fsbl``, …) are
        passed through verbatim to the snagrecover YAML.
        """
        if firmware:
            with tempfile.TemporaryDirectory(prefix="jumpstarter-snagrecover-upload-") as tmpdir:
                tmppath = Path(tmpdir)
                firmware_paths: dict[str, str] = {}

                # Stage each uploaded firmware file in the tempdir under
                # its original basename. Absolute paths go into the YAML
                # so snagrecover finds them regardless of its cwd.
                for entry in firmware:
                    role, handle, filename = entry
                    target = tmppath / filename
                    self.logger.info("Staging firmware %s → %s", role, target)
                    async with await FileWriteStream.from_path(str(target)) as stream:
                        async with self.resource(handle) as res:
                            async for piece in res:
                                await stream.send(piece)
                    firmware_paths[role] = str(target)

                async for chunk in self._run_snagrecover(firmware_paths):
                    yield chunk
            return

        # Local mode — no upload, use the configured `firmware` field.
        if not self.firmware:
            raise RuntimeError(
                "Snagboot: no firmware provided and no exporter-local "
                "'firmware' is configured. Either pass firmware files "
                "or set `config.firmware: { <role>: <absolute path>, ... }` "
                "in the exporter YAML."
            )
        async for chunk in self._run_snagrecover(self.firmware):
            yield chunk
