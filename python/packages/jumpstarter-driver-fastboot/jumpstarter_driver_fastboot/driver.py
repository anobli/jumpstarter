import asyncio
import json
import shlex
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from anyio.streams.file import FileWriteStream

from jumpstarter.driver import Driver, export

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


@dataclass(kw_only=True)
class Fastboot(Driver):
    """Driver wrapping the ``fastboot`` CLI on the exporter host.

    The DUT must already be in fastboot mode (e.g. by running
    ``j am62x boot-to-fastboot`` or otherwise sending ``fastboot 0``
    from U-Boot). This driver only orchestrates host-side
    ``fastboot`` invocations.
    """

    fastboot_path: str = "fastboot"

    # Optional ``-s <serial>`` filter for multi-DUT exporters. Accepts
    # either a device serial number or a fastboot USB path
    # (``usb:1-2.3``).
    serial: str | None = None

    # Exporter-local default images: ``partition -> absolute path``.
    # When ``flash()`` is called without a file, the path for the
    # requested partition is looked up here.
    partitions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_fastboot.client.FastbootClient"

    def _match_args(self) -> list[str]:
        if self.serial:
            return ["-s", self.serial]
        return []

    async def _stream(self, args: list[str]) -> AsyncGenerator[str, None]:
        """Run a fastboot command and yield combined stdout/stderr chunks."""
        cmd = [self.fastboot_path, *self._match_args(), *args]
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
            raise RuntimeError(f"fastboot exited with code {rc}")

    async def _capture(self, args: list[str]) -> str:
        out: list[str] = []
        async for chunk in self._stream(args):
            out.append(chunk)
        return "".join(out)

    @export
    async def devices(self) -> list[dict[str, str]]:
        """``fastboot devices`` — list devices currently in fastboot mode."""
        try:
            output = await self._capture(["devices"])
        except RuntimeError:
            output = ""
        devices: list[dict[str, str]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append({"serial": parts[0], "type": parts[1]})
        return devices

    @export
    async def getvar(self, var: str) -> str:
        """``fastboot getvar VAR`` — return the string value (or empty)."""
        output = await self._capture(["getvar", var])
        # fastboot output looks like:
        #   <var>: <value>
        #   Finished. Total time: 0.001s
        for line in output.splitlines():
            if line.startswith(f"{var}:"):
                return line.split(":", 1)[1].strip()
        return ""

    @export
    async def flash(
        self,
        partition: str,
        handle: str | None = None,
        filename: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """``fastboot flash PARTITION FILE``.

        Two modes, selected by ``handle``:

        * **Upload mode** — when ``handle`` is provided, the client
          resource handle is staged into a tempdir on the exporter
          (under ``filename`` if given, else the partition name) and
          fastboot is invoked against the staged file.
        * **Local mode** — when ``handle`` is omitted, the exporter's
          ``partitions[partition]`` config is used. Useful when image
          paths are baked into the exporter setup.
        """
        if handle is not None:
            with tempfile.TemporaryDirectory(prefix="jumpstarter-fastboot-") as tmpdir:
                target = Path(tmpdir) / (filename or partition)
                self.logger.info("Staging %s → %s", partition, target)
                async with await FileWriteStream.from_path(str(target)) as stream:
                    async with self.resource(handle) as res:
                        async for piece in res:
                            await stream.send(piece)
                async for chunk in self._stream(["flash", partition, str(target)]):
                    yield chunk
            return

        # Local mode — use the configured `partitions` mapping.
        local = self.partitions.get(partition)
        if not local:
            raise RuntimeError(
                f"Fastboot: no file provided and no exporter-local path "
                f"configured for partition {partition!r}. Either pass a "
                f"file or set `config.partitions.{partition}` in the "
                f"exporter YAML."
            )
        async for chunk in self._stream(["flash", partition, local]):
            yield chunk

    @export
    async def erase(self, partition: str) -> AsyncGenerator[str, None]:
        """``fastboot erase PARTITION``."""
        async for chunk in self._stream(["erase", partition]):
            yield chunk

    @export
    async def reboot(self, target: str | None = None) -> AsyncGenerator[str, None]:
        """``fastboot reboot [TARGET]``.

        ``target`` may be ``bootloader``, ``recovery``, ``fastboot``,
        or ``None`` for a normal reboot.
        """
        args = ["reboot"]
        if target:
            args.append(target)
        async for chunk in self._stream(args):
            yield chunk

    @export
    async def set_active(self, slot: str) -> AsyncGenerator[str, None]:
        """``fastboot set_active SLOT`` — for A/B partitioned systems."""
        async for chunk in self._stream(["set_active", slot]):
            yield chunk

    async def _download_and_extract_bundle(self, handle: str, tmppath: Path) -> Path:
        """Download and extract the zip bundle. Returns zip path."""
        zip_path = tmppath / "bundle.zip"
        async with await FileWriteStream.from_path(str(zip_path)) as stream:
            async with self.resource(handle) as res:
                async for piece in res:
                    await stream.send(piece)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmppath)
        return zip_path

    @staticmethod
    def _find_manifest(tmppath: Path) -> Path:
        """Find and return the manifest file path."""
        for name in ["manifest.yaml", "manifest.yml", "manifest.json"]:
            candidate = tmppath / name
            if candidate.exists():
                return candidate
        raise RuntimeError("No manifest.yaml or manifest.json found in bundle")

    @staticmethod
    def _parse_manifest(manifest_path: Path) -> dict[str, str]:
        """Parse manifest file (YAML or JSON) and return partition mapping."""
        manifest_text = manifest_path.read_text()
        if manifest_path.suffix in [".yaml", ".yml"]:
            if not HAS_YAML:
                raise RuntimeError("YAML manifest requires PyYAML. Install with: pip install pyyaml")
            partitions = yaml.safe_load(manifest_text)
        else:
            partitions = json.loads(manifest_text)

        if not isinstance(partitions, dict):
            raise RuntimeError("Manifest must be a dictionary mapping partition names to filenames")

        if not partitions:
            raise RuntimeError("Manifest contains no partitions to flash")

        return partitions

    async def _flash_single_partition(
        self,
        part_name: str,
        img_path: Path,
        wipe: bool,
    ) -> AsyncGenerator[str, None]:
        """Flash a single partition image."""
        args = ["flash", part_name, str(img_path)]
        if wipe and part_name == "userdata":
            args.insert(1, "-w")
        async for chunk in self._stream(args):
            yield chunk

    @export
    async def flashall(
        self,
        handle: str,
        wipe: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Flash multiple partitions defined in a manifest file.

        Expects a zip bundle containing a manifest file (``manifest.yaml``
        or ``manifest.json``) and all referenced image files.

        The manifest is a simple mapping of partition names to filenames:

        .. code-block:: yaml

            boot_a: boot.img
            boot_b: boot.img
            vendor_boot_a: vendor_boot.img
            vendor_boot_b: vendor_boot.img
            super: super.img
            userdata: userdata.img

        Or in JSON:

        .. code-block:: json

            {
              "boot_a": "boot.img",
              "boot_b": "boot.img",
              "super": "super.img"
            }

        :param handle: Resource handle to zip bundle (manifest + images)
        :param wipe: If True, wipes userdata (``-w`` flag)
        """
        with tempfile.TemporaryDirectory(prefix="jumpstarter-fastboot-flashall-") as tmpdir:
            tmppath = Path(tmpdir)

            # Download and extract zip bundle
            yield "Downloading bundle...\n"
            await self._download_and_extract_bundle(handle, tmppath)

            yield "Extracting bundle...\n"
            manifest_path = self._find_manifest(tmppath)

            # Parse manifest
            yield f"Reading manifest: {manifest_path.name}\n"
            partitions = self._parse_manifest(manifest_path)
            yield f"Found {len(partitions)} partition(s) in manifest\n"

            # Process each partition
            for idx, (part_name, file_ref) in enumerate(partitions.items(), 1):
                if not file_ref or not isinstance(file_ref, str):
                    yield f"⚠ Skipping {part_name}: invalid filename\n"
                    continue

                img_path = tmppath / file_ref
                if not img_path.exists():
                    yield f"⚠ Skipping {part_name}: file not found: {img_path}\n"
                    continue

                yield f"[{idx}/{len(partitions)}] Flashing {part_name}: {img_path.name}\n"
                async for chunk in self._flash_single_partition(part_name, img_path, wipe):
                    yield chunk

            yield "\n✓ Flashall complete\n"
