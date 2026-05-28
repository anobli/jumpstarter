import os
import time
from types import SimpleNamespace
from typing import cast

from anyio import create_memory_object_stream
from anyio.streams.stapled import StapledObjectStream

from . import driver as driver_module
from .client import PySerialClient
from .driver import PySerial, ThrottledStream
from jumpstarter.common.utils import serve


def test_bare_pyserial():
    with serve(PySerial(url="loop://")) as client:
        with client.stream() as stream:
            stream.send(b"hello")
            assert "hello".startswith(stream.receive().decode("utf-8"))


def test_bare_open_pyserial():
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        stream = client.open_stream()
        stream.send(b"hello")
        assert "hello".startswith(stream.receive().decode("utf-8"))
        client.close()


def test_pexpect_open_pyserial_forget_close():
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)  # this is only necessary for the editor to recognize the client methods
        pexpect = client.open()
        pexpect.sendline("hello")
        assert pexpect.expect("hello") == 0


def test_pexpect_open_pyserial():
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        pexpect = client.open()
        pexpect.sendline("hello")
        assert pexpect.expect("hello") == 0
        client.close()


def test_pexpect_context_pyserial():
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        with client.pexpect() as pexpect:
            pexpect.sendline("hello")
            assert pexpect.expect("hello") == 0


def test_can_open_not_present():
    with serve(PySerial(url="/dev/doesNotExist", check_present=False)):
        # we only verify that the context manager does not raise an exception
        pass


def test_cps_throttling():
    """Test that CPS throttling is configured correctly."""
    cps = 5  # 5 characters per second
    test_data = b"hello"  # 5 characters

    with serve(PySerial(url="loop://", cps=cps)) as client:
        with client.stream() as stream:
            # Just verify that the throttling doesn't break functionality
            # The actual timing test is done at the async level
            stream.send(test_data)

            # Verify data was sent correctly (receive character by character)
            received_data = b""
            for _ in range(len(test_data)):
                received_data += stream.receive()
            assert test_data == received_data


def test_no_cps_throttling():
    """Test that without CPS throttling, transmission is fast."""
    test_data = b"hello"

    with serve(PySerial(url="loop://")) as client:  # No CPS specified
        with client.stream() as stream:
            start_time = time.perf_counter()
            stream.send(test_data)
            end_time = time.perf_counter()

            elapsed_time = end_time - start_time
            # Without throttling, should be fast; allow headroom for CI noise
            assert elapsed_time < 0.5, f"Expected fast transmission, got {elapsed_time}s"

            received = stream.receive()
            assert test_data.decode("utf-8").startswith(received.decode("utf-8"))


def test_cps_zero_disables_throttling():
    """Test that CPS=0 disables throttling."""
    test_data = b"hello"

    with serve(PySerial(url="loop://", cps=0)) as client:
        with client.stream() as stream:
            start_time = time.perf_counter()
            stream.send(test_data)
            end_time = time.perf_counter()

            elapsed_time = end_time - start_time
            # With CPS=0, should be fast (no throttling) – allow headroom
            assert elapsed_time < 0.5, f"Expected fast transmission with cps=0, got {elapsed_time}s"

            received = stream.receive()
            assert test_data.decode("utf-8").startswith(received.decode("utf-8"))


def test_throttled_stream_async():
    """Test that ThrottledStream works correctly at the async level."""
    import anyio

    async def _test():
        cps = 5  # 5 characters per second
        test_data = b"hello world!"  # 12 characters
        expected_min_time = (len(test_data) - 1) / cps  # Should take at least 11/5 = 2.2 seconds

        # Create a memory stream for testing
        tx, rx = create_memory_object_stream[bytes](32)  # ty: ignore[call-non-callable]
        stapled_stream = StapledObjectStream(tx, rx)
        # Wrap it with throttling and ensure proper closure
        async with ThrottledStream(stream=stapled_stream, cps=cps) as throttled_stream:
            start_time = time.perf_counter()
            await throttled_stream.send(test_data)
            end_time = time.perf_counter()

            elapsed_time = end_time - start_time
            # Allow some overhead for CI environments but not excessive delay
            expected_max_time = expected_min_time * 1.5  # 50% overhead for CI slowness
            assert expected_min_time <= elapsed_time <= expected_max_time, (
                f"Expected {expected_min_time}s-{expected_max_time}s, got {elapsed_time}s"
            )

            # Verify data was sent correctly (character by character)
            received_data = b""
            for _ in range(len(test_data)):
                received_data += await throttled_stream.receive()
            assert test_data == received_data

    anyio.run(_test)


def test_cps_with_pexpect():
    """Test that CPS throttling works with pexpect interface."""
    cps = 10  # 10 characters per second

    with serve(PySerial(url="loop://", cps=cps)) as client:
        client = cast(PySerialClient, client)
        with client.pexpect() as pexpect:
            # Just verify that pexpect works with throttling enabled
            pexpect.sendline("test")
            assert pexpect.expect("test") == 0
            # We don't test timing here since pexpect has complex buffering


def test_disable_hupcl_applies_termios_flags(monkeypatch):
    calls = {}

    class FakeSerial:
        @staticmethod
        def fileno():
            return 42

    def fake_tcgetattr(fd):
        calls["fd_get"] = fd
        return [0, 0, 0x4000 | 0x0008, 0, 0, 0, []]

    def fake_tcsetattr(fd, when, attrs):
        calls["fd_set"] = fd
        calls["when"] = when
        calls["attrs"] = attrs

    monkeypatch.setattr(driver_module.os, "name", "posix")

    monkeypatch.setattr(
        driver_module,
        "termios",
        SimpleNamespace(HUPCL=0x4000, TCSANOW=0, tcgetattr=fake_tcgetattr, tcsetattr=fake_tcsetattr),
    )

    driver = PySerial(url="/dev/ttyUSB0", check_present=False, disable_hupcl=True)
    driver._maybe_disable_hupcl(FakeSerial())

    assert calls["fd_get"] == 42
    assert calls["fd_set"] == 42
    assert calls["when"] == 0
    assert calls["attrs"][2] & 0x4000 == 0


def test_release_blocks_new_connect_until_acquire():
    """release() should make subsequent connect() block; acquire() releases."""
    import threading

    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        client.release()

        opened = threading.Event()
        completed = threading.Event()

        def open_stream():
            with client.stream() as _stream:
                opened.set()
            completed.set()

        worker = threading.Thread(target=open_stream, daemon=True)
        worker.start()
        # While released, the stream call must block.
        assert not opened.wait(0.5)

        client.acquire()
        # After acquire, the stream must open promptly.
        assert opened.wait(5.0), "stream did not open after acquire()"
        assert completed.wait(5.0)
        worker.join(timeout=5.0)


def test_release_tears_down_active_stream():
    """release() should close streams currently held by clients (EOF)."""
    import threading

    from anyio import BrokenResourceError, EndOfStream

    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        with client.stream() as stream:
            # Trigger release from another thread so this thread can block on receive.
            threading.Thread(
                target=lambda: (time.sleep(0.1), client.release()),
                daemon=True,
            ).start()

            try:
                stream.receive()
            except (EndOfStream, BrokenResourceError):
                pass
            else:
                raise AssertionError("expected EOF after release()")

        # Re-acquire to leave the driver in a clean state.
        client.acquire()


def test_released_context_manager_reacquires_on_exit():
    """The released() context manager must re-acquire even on exceptions."""
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)

        with client.released():
            pass
        # Acquire should have run — next stream must open.
        with client.stream() as stream:
            stream.send(b"x")
            assert stream.receive().startswith(b"x")

        try:
            with client.released():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        # Still re-acquired despite the exception.
        with client.stream() as stream:
            stream.send(b"y")
            assert stream.receive().startswith(b"y")


def test_acquire_without_release_is_noop():
    """acquire() on an already-acquired port should not raise or block."""
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        client.acquire()  # no-op
        # Stream still works.
        with client.stream() as stream:
            stream.send(b"hi")
            assert stream.receive().startswith(b"hi")


def test_pty_yields_valid_slave_path():
    """pty() must yield a path that exists and looks like a PTY slave."""
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        with client.pty() as slave_path:
            assert slave_path.startswith("/dev/pts/") or slave_path.startswith("/dev/")
            assert os.path.exists(slave_path)


def test_pty_creates_and_removes_symlink(tmp_path):
    """When symlink_path is given, it should exist while in context, gone after."""
    symlink = tmp_path / "jmp-tty"
    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        with client.pty(symlink_path=str(symlink)) as slave_path:
            assert symlink.is_symlink()
            assert os.readlink(str(symlink)) == slave_path
        assert not symlink.exists()


def test_pty_slave_is_raw_mode():
    """Bridge should put the slave in raw mode so consumers see a transparent
    byte pipe (no line discipline, no echo). Verifying termios flags is
    cheaper and more deterministic than a loopback echo test."""
    import termios

    with serve(PySerial(url="loop://")) as client:
        client = cast(PySerialClient, client)
        with client.pty() as slave_path:
            fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)
            try:
                iflag, oflag, _cflag, lflag, *_ = termios.tcgetattr(fd)
                # Echo and canonical input must be off.
                assert lflag & termios.ECHO == 0, "ECHO should be cleared"
                assert lflag & termios.ICANON == 0, "ICANON should be cleared"
                # Output post-processing off (no LF→CRLF translation).
                assert oflag & termios.OPOST == 0, "OPOST should be cleared"
            finally:
                os.close(fd)


def test_disable_hupcl_noop_when_disabled(monkeypatch):
    called = {"tcgetattr": False}

    def fake_tcgetattr(_fd):
        called["tcgetattr"] = True
        return [0, 0, 0x4000, 0, 0, 0, []]

    monkeypatch.setattr(
        driver_module,
        "termios",
        SimpleNamespace(HUPCL=0x4000, TCSANOW=0, tcgetattr=fake_tcgetattr, tcsetattr=lambda *_: None),
    )

    driver = PySerial(url="/dev/ttyUSB0", check_present=False, disable_hupcl=False)
    driver._maybe_disable_hupcl(None)

    assert called["tcgetattr"] is False
