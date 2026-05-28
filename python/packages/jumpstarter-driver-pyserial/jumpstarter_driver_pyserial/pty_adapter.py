import io
import os
import pty
import tty
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO, cast

import anyio
from anyio import BrokenResourceError, EndOfStream
from anyio.streams.file import FileReadStream, FileWriteStream

from jumpstarter.client import DriverClient
from jumpstarter.client.adapters import blocking


@blocking
@asynccontextmanager
async def PtyAdapter(
    *,
    client: DriverClient,
    method: str = "connect",
    symlink_path: str | os.PathLike | None = None,
):
    """Open a local PTY and bridge it to a remote bytestream.

    Yields the slave device path (e.g. ``/dev/pts/7``) that local
    consumers — Twister via ``--device-serial-pty``, pyserial, picocom —
    can attach to. The bridge survives transient disconnects of the
    upstream stream (e.g. during a PySerial release()/acquire() cycle),
    keeping the slave path stable for the lifetime of this context.

    Args:
        client: DriverClient exposing a bytestream via ``method``.
        method: Name of the exportstream method (default ``connect``).
        symlink_path: If given, a stable symlink to the slave is created
            so callers can address the PTY by a known path.
    """
    master_fd, slave_fd = pty.openpty()
    # Raw on both ends: the bridge is a transparent byte pipe. If we left
    # the slave in cooked mode with ECHO, every byte written by the bridge
    # would be echoed back through the master and re-sent upstream,
    # forming a feedback loop.
    tty.setraw(master_fd)
    tty.setraw(slave_fd)
    slave_path = os.ttyname(slave_fd)

    symlink: Path | None = None
    if symlink_path is not None:
        symlink = Path(symlink_path)
        symlink.unlink(missing_ok=True)
        symlink.symlink_to(slave_path)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_bridge_loop, client, method, master_fd)
            try:
                yield slave_path
            finally:
                tg.cancel_scope.cancel()
    finally:
        if symlink is not None:
            symlink.unlink(missing_ok=True)
        # slave_fd is kept open through the bridge's lifetime so a consumer
        # closing/reopening the slave doesn't deliver EIO to the master.
        os.close(slave_fd)
        os.close(master_fd)


async def _bridge_loop(client: DriverClient, method: str, master_fd: int):
    """Pump bytes between the PTY master and the remote bytestream.

    Reconnects on EndOfStream / BrokenResourceError so that a server-side
    release()/acquire() cycle (or a brief flasher take-over) appears as a
    pause on the PTY rather than a hard close.
    """
    # Two independent Python file objects over the same fd; closefd=False
    # because the outer adapter owns the fd's lifetime.
    rx = FileReadStream(cast(BinaryIO, io.FileIO(master_fd, mode="rb", closefd=False)))
    tx = FileWriteStream(cast(BinaryIO, io.FileIO(master_fd, mode="wb", closefd=False)))

    while True:
        try:
            async with client.stream_async(method) as remote:
                async with anyio.create_task_group() as tg:

                    async def pump(src, dst, *, tg=tg):
                        try:
                            while True:
                                data = await src.receive()
                                if not data:
                                    break
                                await dst.send(data)
                        except (EndOfStream, BrokenResourceError):
                            pass
                        finally:
                            tg.cancel_scope.cancel()

                    tg.start_soon(pump, rx, remote)
                    tg.start_soon(pump, remote, tx)
        except (EndOfStream, BrokenResourceError, OSError):
            pass
        # Brief backoff so a permanently-broken upstream doesn't spin.
        await anyio.sleep(0.5)
