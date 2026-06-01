import asyncio
import inspect
import os
import re
import shlex
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from anyio.streams.file import FileWriteStream

from jumpstarter.common.exceptions import ConfigurationError
from jumpstarter.driver import Driver, export

# Parses lines like:
#   Found DFU: [0483:df11] ver=0200, devnum=18, cfg=1, intf=0,
#              path="3-1", alt=1, name="@Internal Flash  /...", serial="3271334D3038"
_FOUND_RE = re.compile(
    r"Found\s+(?P<mode>DFU|Runtime):\s*"
    r"\[(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})\]"
    r"(?P<rest>.*)$"
)
_KV_RE = re.compile(r"(\w+)=(?:\"([^\"]*)\"|(\S+?))(?:,|\s|$)")


@dataclass(kw_only=True)
class Dfu(Driver):
    """DFU (Device Firmware Update) driver.

    Wraps the ``dfu-util`` CLI on the exporter host. The DUT must be
    physically connected to the exporter via USB and already in DFU
    mode for the driver to interact with it.
    """

    dfu_util_path: str = "dfu-util"
    vid_pid: str | None = None
    serial: str | None = None
    intf: str | None = None

    # Sequence executed by `enter_dfu()` to put the DUT into DFU mode.
    # Each step is one of:
    #   { "call": "<child>.<method>", "args": [...] }   invoke child method
    #   { "sleep": <seconds> }                           sleep
    #   { "log": "<message>" }                           log a message
    enter_dfu_sequence: list[dict[str, Any]] = field(default_factory=list)
    # If True, after running enter_dfu_sequence, poll until at least one
    # device matching `vid_pid`/`serial` (if set) shows up in `dfu-util -l`,
    # or fail after `enter_dfu_timeout` seconds.
    enter_dfu_wait: bool = True
    enter_dfu_timeout: float = 15.0

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        # Fail fast at exporter startup if the configured sequence
        # references a child that wasn't wired in. The same set of
        # checks would otherwise only trip at the first enter_dfu()
        # call. Empty sequences are fine — Dfu can be used purely as a
        # `dfu-util` wrapper without any orchestration children.
        for name in _children_referenced_in(self.enter_dfu_sequence):
            if name not in self.children:
                raise ConfigurationError(
                    f"Dfu: enter_dfu_sequence references child '{name}' "
                    f"which is not wired in (set `children.{name}.ref: ...` "
                    f"in the exporter config)"
                )

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_dfu.client.DfuClient"

    def _build_match_args(self, vid_pid: str | None, serial: str | None, intf: str | None) -> list[str]:
        """Build dfu-util device-selection args, with per-call overrides."""
        args: list[str] = []
        eff_vid_pid = vid_pid or self.vid_pid
        eff_serial = serial or self.serial
        eff_intf = intf if intf is not None else self.intf
        if eff_vid_pid:
            args += ["-d", eff_vid_pid]
        if eff_serial:
            args += ["-S", eff_serial]
        if eff_intf is not None:
            args += ["-i", str(eff_intf)]
        return args

    async def _stream(self, args: list[str]) -> AsyncGenerator[str, None]:
        """Run dfu-util and yield stdout/stderr chunks as they arrive.

        Reads in small chunks (not lines) so that ``\\r``-terminated
        progress updates from ``dfu-util`` reach the client live.
        """
        cmd = [self.dfu_util_path, *args]
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
            raise RuntimeError(f"dfu-util exited with code {rc}")

    async def _capture(self, args: list[str]) -> str:
        """Run dfu-util to completion and return its full combined output."""
        out: list[str] = []
        async for chunk in self._stream(args):
            out.append(chunk)
        return "".join(out)

    @staticmethod
    def _parse_list(output: str) -> list[dict[str, Any]]:
        """Parse the output of ``dfu-util -l`` into a list of device dicts."""
        devices: list[dict[str, Any]] = []
        for line in output.splitlines():
            m = _FOUND_RE.search(line)
            if not m:
                continue
            entry: dict[str, Any] = {
                "mode": m.group("mode"),
                "vid": m.group("vid").lower(),
                "pid": m.group("pid").lower(),
            }
            for kv in _KV_RE.finditer(m.group("rest")):
                key = kv.group(1)
                value = kv.group(2) if kv.group(2) is not None else kv.group(3)
                entry[key] = value
            devices.append(entry)
        return devices

    @export
    async def list_devices(self) -> list[dict[str, Any]]:
        """Return DFU devices currently visible to the exporter."""
        try:
            output = await self._capture(["-l"])
        except RuntimeError:
            # Some dfu-util versions exit non-zero when no devices are present
            output = ""
        return self._parse_list(output)

    async def _run_step(self, step: dict[str, Any]) -> None:
        """Execute one step of an enter/leave sequence."""
        if "sleep" in step:
            await asyncio.sleep(float(step["sleep"]))
            return
        if "log" in step:
            self.logger.info("%s", step["log"])
            return
        if "call" in step:
            target = str(step["call"])
            if "." not in target:
                raise ValueError(f"Invalid 'call' target {target!r}: expected '<child>.<method>'")
            child_name, method_name = target.split(".", 1)
            if child_name not in self.children:
                raise ValueError(
                    f"Step refers to unknown child {child_name!r}; available children: {sorted(self.children)}"
                )
            child = self.children[child_name]
            method = getattr(child, method_name, None)
            if method is None or not callable(method):
                raise ValueError(f"Child {child_name!r} has no callable method {method_name!r}")
            args = step.get("args", [])
            self.logger.info("Step: %s(%s)", target, ", ".join(repr(a) for a in args))
            result = method(*args)
            if inspect.isawaitable(result):
                await result
            return
        raise ValueError(f"Unrecognised step: {step!r}")

    def _device_match(self, dev: dict[str, Any]) -> bool:
        """True if a device entry from list_devices matches the configured filter."""
        if self.vid_pid:
            want = self.vid_pid.lower().replace("0x", "")
            got = f"{dev.get('vid', '')}:{dev.get('pid', '')}".lower()
            if got != want:
                return False
        if self.serial and dev.get("serial") != self.serial:
            return False
        return True

    @export
    async def enter_dfu(self) -> str:
        """Run the configured sequence to put the DUT into DFU mode.

        After the sequence, if ``enter_dfu_wait`` is True, polls
        ``dfu-util -l`` until a matching device appears (or timeout).
        Returns a short human-readable summary.
        """
        if not self.enter_dfu_sequence:
            raise RuntimeError("enter_dfu_sequence is empty; configure it in the exporter YAML to use enter_dfu()")

        self.logger.info("Entering DFU mode (%d steps)", len(self.enter_dfu_sequence))
        for step in self.enter_dfu_sequence:
            await self._run_step(step)

        if not self.enter_dfu_wait:
            return "DFU sequence executed (wait disabled)"

        deadline = time.monotonic() + self.enter_dfu_timeout
        last_seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                devices = await self.list_devices()
            except Exception as e:  # noqa: BLE001 — keep polling, surface at end
                self.logger.debug("list_devices failed during wait: %s", e)
                devices = []
            last_seen = devices
            matches = [d for d in devices if self._device_match(d)]
            if matches:
                summary = f"DFU device detected: {matches[0].get('vid')}:{matches[0].get('pid')}"
                self.logger.info(summary)
                return summary
            await asyncio.sleep(0.5)

        raise TimeoutError(f"Timed out after {self.enter_dfu_timeout}s waiting for DFU device. Last seen: {last_seen}")

    @export
    async def wait_for_device(self, timeout: float = 15.0) -> dict[str, Any]:
        """Poll ``dfu-util -l`` until a matching device shows up, or timeout."""
        deadline = time.monotonic() + float(timeout)
        last_seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                devices = await self.list_devices()
            except Exception:  # noqa: BLE001
                devices = []
            last_seen = devices
            for d in devices:
                if self._device_match(d):
                    return d
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Timed out after {timeout}s waiting for DFU device. Last seen: {last_seen}")

    @export
    async def download(
        self,
        handle: str,
        alt: str = "0",
        dfuse_address: str | None = None,
        vid_pid: str | None = None,
        serial: str | None = None,
        intf: str | None = None,
        transfer_size: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Flash ``handle`` to the device, streaming dfu-util output.

        :param handle: client-side resource handle for the file to send.
        :param alt: DFU alt setting to write to.
        :param dfuse_address: ``-s`` argument for DfuSe targets (STM32, etc).
            May include suffixes like ``:leave`` or ``:force``.
        :param vid_pid: per-call override for ``-d VID:PID``.
        :param serial: per-call override for ``-S serial``.
        :param intf: per-call override for ``-i intf``.
        :param transfer_size: per-call override for ``-t size``.
        """
        with _temporary_filename() as filename:
            async with await FileWriteStream.from_path(filename) as stream:
                async with self.resource(handle) as res:
                    async for piece in res:
                        await stream.send(piece)

            args = ["-D", filename, "-a", str(alt)]
            args += self._build_match_args(vid_pid, serial, intf)
            if dfuse_address:
                args += ["-s", dfuse_address]
            if transfer_size:
                args += ["-t", str(transfer_size)]

            async for chunk in self._stream(args):
                yield chunk

    @export
    async def detach(
        self,
        vid_pid: str | None = None,
        serial: str | None = None,
        intf: str | None = None,
    ) -> str:
        """Tell the device to leave DFU mode (``dfu-util -e``)."""
        args = ["-e", *self._build_match_args(vid_pid, serial, intf)]
        return await self._capture(args)


def _children_referenced_in(sequence: list[dict[str, Any]]) -> set[str]:
    """Return child names referenced by `call:` steps of an enter/leave sequence."""
    refs: set[str] = set()
    for step in sequence:
        target = step.get("call") if isinstance(step, dict) else None
        if isinstance(target, str) and "." in target:
            refs.add(target.split(".", 1)[0])
    return refs


@contextmanager
def _temporary_filename():
    fd, name = tempfile.mkstemp(prefix="jumpstarter-dfu-")
    os.close(fd)
    try:
        yield name
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
