import os
import pty
import tty
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from anyio import BrokenResourceError, EndOfStream

from jumpstarter.client import DriverClient
from jumpstarter.client.adapters import blocking
from jumpstarter.streams.metadata import MetadataStream


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


async def pty_to_remote(remote: MetadataStream, master_fd: int, tg: anyio.abc.TaskGroup):
    try:
        while True:
            await anyio.wait_readable(master_fd)
            try:
                data = os.read(master_fd, 65536)
            except BlockingIOError:
                continue
            if not data:
                break
            await remote.send(data)
    except (EndOfStream, BrokenResourceError, OSError):
        pass
    finally:
        tg.cancel_scope.cancel()


async def remote_to_pty(remote: MetadataStream, master_fd: int, tg: anyio.abc.TaskGroup):
    try:
        while True:
            data = await remote.receive()
            if not data:
                break
            view = memoryview(data)
            while view:
                try:
                    n = os.write(master_fd, view)
                    view = view[n:]
                except BlockingIOError:
                    await anyio.wait_writable(master_fd)
    except (EndOfStream, BrokenResourceError, OSError):
        pass
    finally:
        tg.cancel_scope.cancel()


async def _bridge_loop(client: DriverClient, method: str, master_fd: int):
    """Pump bytes between the PTY master and the remote bytestream.

    Reconnects on EndOfStream / BrokenResourceError so that a server-side
    release()/acquire() cycle (or a brief flasher take-over) appears as a
    pause on the PTY rather than a hard close.
    """
    # Non-blocking so reads/writes suspend via anyio's fd-readiness waits
    # instead of a blocking thread read. A blocking os.read() on the master
    # never returns (the slave is held open for the bridge's lifetime, so it
    # never sees EOF), and anyio's thread reads are not cancellable — which
    # would make the adapter's context manager hang forever on exit.
    os.set_blocking(master_fd, False)

    while True:
        try:
            async with client.stream_async(method) as remote:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(pty_to_remote, remote, master_fd, tg)
                    tg.start_soon(remote_to_pty, remote, master_fd, tg)
        except (EndOfStream, BrokenResourceError, OSError):
            pass
        # Brief backoff so a permanently-broken upstream doesn't spin.
        await anyio.sleep(0.5)
