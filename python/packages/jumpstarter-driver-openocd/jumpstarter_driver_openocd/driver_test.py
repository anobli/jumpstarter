import io
import os
import stat
import sys
import tarfile
from pathlib import Path
from typing import cast

import click as _click
import pytest

from .client import OpenOCDClient
from .driver import OpenOCD
from jumpstarter.common.utils import serve


def _write_fake_openocd(dir_path: Path, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Drop a tiny Python script that records argv + cwd and exits with
    the requested code. Tests put `dir_path` on PATH and instantiate the
    driver with the default `command_openocd="openocd"`."""
    script = dir_path / "openocd"
    script.write_text(
        f"""#!{sys.executable}
import os, sys, pathlib
cwd = pathlib.Path.cwd()
(cwd / "argv.txt").write_text("\\n".join(sys.argv[1:]))
(cwd / "cwd.txt").write_text(str(cwd))
sys.stdout.write("FAKE-OPENOCD-STDOUT\\n")
sys.stderr.write({stderr!r})
sys.exit({exit_code})
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _make_bundle(tmp_path: Path) -> Path:
    src = tmp_path / "bundle_src"
    src.mkdir()
    (src / "zephyr.hex").write_bytes(b":00000001FF\n")
    (src / "openocd.cfg").write_text("# fake openocd config\n")
    return src


@pytest.fixture
def fake_openocd_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_openocd(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


def test_flash_happy_path(tmp_path, fake_openocd_on_path):
    """End-to-end: client tars a dir, driver extracts + runs openocd."""
    bundle_src = _make_bundle(tmp_path)

    with serve(OpenOCD()) as client:
        client = cast(OpenOCDClient, client)
        result = client.flash_dir(
            str(bundle_src),
            args=[
                "-f",
                "{bundle}/openocd.cfg",
                "-c",
                "program {bundle}/zephyr.hex verify reset exit",
            ],
        )

    assert result["returncode"] == 0, result
    assert "FAKE-OPENOCD-STDOUT" in result["stdout"]
    assert result["stderr"] == ""


def test_flash_substitutes_bundle_and_stages_files(tmp_path, monkeypatch):
    """{bundle} placeholder is substituted; the substituted paths point
    at files that actually exist when openocd runs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = tmp_path / "out"
    recorder.mkdir()

    script = bin_dir / "openocd"
    script.write_text(
        f"""#!{sys.executable}
import sys, pathlib, shutil
cwd = pathlib.Path.cwd()
out = pathlib.Path({str(recorder)!r})
(out / "argv.txt").write_text("\\n".join(sys.argv[1:]))
(out / "exists.txt").write_text(
    "\\n".join(f"{{p}}={{pathlib.Path(p).is_file()}}" for p in sys.argv[1:] if "/" in p)
)
for p in cwd.rglob("*"):
    if p.is_file():
        dst = out / "tree" / p.relative_to(cwd)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
sys.exit(0)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    bundle_src = _make_bundle(tmp_path)
    with serve(OpenOCD()) as client:
        client = cast(OpenOCDClient, client)
        # Args here use standalone path tokens (not embedded inside a -c
        # string) so we can directly check each substituted path exists.
        result = client.flash_dir(
            str(bundle_src),
            args=["-f", "{bundle}/openocd.cfg", "--image", "{bundle}/zephyr.hex"],
        )
    assert result["returncode"] == 0

    argv = (recorder / "argv.txt").read_text().splitlines()
    assert not any("{bundle}" in a for a in argv), argv

    exists = dict(line.rsplit("=", 1) for line in (recorder / "exists.txt").read_text().splitlines() if line)
    cfg = next(p for p in exists if p.endswith("openocd.cfg"))
    hex_ = next(p for p in exists if p.endswith("zephyr.hex"))
    assert exists[cfg] == "True"
    assert exists[hex_] == "True"

    files = sorted(p.name for p in (recorder / "tree").rglob("*") if p.is_file())
    assert "zephyr.hex" in files
    assert "openocd.cfg" in files


def test_flash_argv_layout(tmp_path, monkeypatch):
    """``openocd [-s static]* -s <bundle> <client args>`` — in that order."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = tmp_path / "argv-out"
    script = bin_dir / "openocd"
    script.write_text(
        f"""#!{sys.executable}
import sys, pathlib
pathlib.Path({str(recorder)!r}).write_text("\\n".join(sys.argv[1:]))
sys.exit(0)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    bundle_src = _make_bundle(tmp_path)
    static_a = tmp_path / "a"
    static_b = tmp_path / "b"
    static_a.mkdir()
    static_b.mkdir()

    with serve(OpenOCD(search_dirs=[str(static_a), str(static_b)])) as client:
        client = cast(OpenOCDClient, client)
        client.flash_dir(str(bundle_src), args=["-c", "init", "-c", "exit"])

    argv = recorder.read_text().splitlines()
    assert argv[0:2] == ["-s", str(static_a)]
    assert argv[2:4] == ["-s", str(static_b)]
    assert argv[4] == "-s"
    assert argv[5].endswith("/bundle")
    assert argv[6:] == ["-c", "init", "-c", "exit"]


def test_flash_propagates_nonzero_exit(tmp_path, monkeypatch):
    """A non-zero openocd exit must surface in the result dict."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_openocd(bin_dir, exit_code=42, stderr="boom\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    bundle_src = _make_bundle(tmp_path)
    with serve(OpenOCD()) as client:
        client = cast(OpenOCDClient, client)
        result = client.flash_dir(str(bundle_src), args=[])

    assert result["returncode"] == 42
    assert "boom" in result["stderr"]


def test_flash_tar_refuses_path_traversal(tmp_path, fake_openocd_on_path):
    """Tarball with a ``..`` entry must be rejected by the extraction
    filter; openocd must NOT be invoked."""
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as tf:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"pwned"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with serve(OpenOCD()) as client:
        client = cast(OpenOCDClient, client)
        # The data filter raises an OutsideDestinationError inside the
        # driver; jumpstarter's RPC wraps it in an ExceptionGroup. We
        # don't care about the wrapping — we care that the call fails,
        # so the bundle is rejected before openocd ever runs.
        with pytest.raises(BaseException):  # noqa: B017, PT011
            client.flash_tar(str(evil), args=[])


def test_flash_dir_rejects_non_directory(tmp_path):
    """flash_dir on a missing or non-dir path must fail fast — before
    we even try to call the exporter."""
    with serve(OpenOCD()) as client:
        client = cast(OpenOCDClient, client)
        with pytest.raises(_click.UsageError):
            client.flash_dir(str(tmp_path / "does-not-exist"), args=[])
