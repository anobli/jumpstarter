# Copyright (c) 2026 BayLibre
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from typing import cast

import pytest

from .client import ZephyrClient
from .driver import Zephyr
from jumpstarter.common.utils import serve


def _make_build_dir(tmp_path: Path, *, hex_=True, bin_=False, elf=False) -> Path:
    """Create a minimal Zephyr build dir with the requested fixed-name artifacts
    under its ``zephyr/`` subdir."""
    build = tmp_path / "build"
    zdir = build / "zephyr"
    zdir.mkdir(parents=True)
    if hex_:
        (zdir / "zephyr.hex").write_text(":00000001FF\n")
    if bin_:
        (zdir / "zephyr.bin").write_bytes(b"\x00\x01\x02\x03")
    if elf:
        (zdir / "zephyr.elf").write_bytes(b"\x7fELF")
    return build


def _flash(client, build_dir, **kw) -> list[str]:
    """Drain the streaming flash and return the emitted lines."""
    return list(cast(ZephyrClient, client).flash_build_dir(str(build_dir), **kw))


def test_flash_happy_path(tmp_path):
    """Client tars the artifacts, driver extracts them and runs the command."""
    build = _make_build_dir(tmp_path)
    with serve(Zephyr(flash_command="echo FAKE-FLASH", flash_timeout=0)) as client:
        lines = _flash(client, build)
    out = "\n".join(lines)
    assert "FAKE-FLASH" in out
    assert "Flash complete" in out


def test_command_runs_in_upload_dir_with_artifact(tmp_path):
    """The command runs with cwd set to the upload dir, where zephyr.hex
    actually exists at flash time."""
    build = _make_build_dir(tmp_path)
    rec = tmp_path / "rec"
    rec.mkdir()
    cmd = f"test -f zephyr.hex && echo HEX_PRESENT; pwd > {rec}/cwd.txt"
    with serve(Zephyr(flash_command=cmd, flash_timeout=0)) as client:
        lines = _flash(client, build)
    assert "HEX_PRESENT" in "\n".join(lines)
    cwd = (rec / "cwd.txt").read_text().strip()
    assert Path(cwd) / "zephyr.hex"  # cwd is the extraction dir


def test_token_substitution(tmp_path):
    """{hex}/{bin}/{dir} are substituted with absolute paths to existing files."""
    build = _make_build_dir(tmp_path, hex_=True, bin_=True)
    rec = tmp_path / "rec"
    rec.mkdir()
    cmd = (
        f"test -f {{hex}} && echo HEX_OK; "
        f"test -f {{bin}} && echo BIN_OK; "
        f'printf "%s\\n%s\\n" "{{hex}}" "{{dir}}" > {rec}/tokens.txt'
    )
    with serve(Zephyr(flash_command=cmd, flash_timeout=0)) as client:
        lines = _flash(client, build)
    out = "\n".join(lines)
    assert "HEX_OK" in out and "BIN_OK" in out
    hex_path, dir_path = (rec / "tokens.txt").read_text().splitlines()
    assert "{hex}" not in hex_path
    assert hex_path == f"{dir_path}/zephyr.hex"


def test_uploads_all_present_artifacts(tmp_path):
    """hex + bin + elf are all uploaded and extracted on the exporter."""
    build = _make_build_dir(tmp_path, hex_=True, bin_=True, elf=True)
    rec = tmp_path / "rec"
    rec.mkdir()
    with serve(Zephyr(flash_command=f"ls > {rec}/files.txt", flash_timeout=0)) as client:
        _flash(client, build)
    files = set((rec / "files.txt").read_text().split())
    assert {"zephyr.hex", "zephyr.bin", "zephyr.elf"} <= files


def test_only_present_artifacts_uploaded(tmp_path):
    """bin/elf absent locally => not present on the exporter."""
    build = _make_build_dir(tmp_path, hex_=True, bin_=False, elf=False)
    rec = tmp_path / "rec"
    rec.mkdir()
    with serve(Zephyr(flash_command=f"ls > {rec}/files.txt", flash_timeout=0)) as client:
        _flash(client, build)
    files = set((rec / "files.txt").read_text().split())
    assert "zephyr.hex" in files
    assert "zephyr.bin" not in files
    assert "zephyr.elf" not in files


def test_board_id_is_informational(tmp_path):
    """board_id is accepted (twister contract) and surfaced as a log line."""
    build = _make_build_dir(tmp_path)
    with serve(Zephyr(flash_command="true", flash_timeout=0)) as client:
        lines = _flash(client, build, board_id="XDS110-ABC123")
    assert any("XDS110-ABC123" in line for line in lines)


def test_flash_with_timeout(tmp_path):
    """A non-zero flash_timeout wraps the command in `timeout` and still works."""
    build = _make_build_dir(tmp_path)
    with serve(Zephyr(flash_command="echo within-timeout", flash_timeout=5)) as client:
        lines = _flash(client, build)
    assert "within-timeout" in "\n".join(lines)


def test_flash_propagates_failure(tmp_path):
    """A non-zero flash command must surface as an error to the client."""
    build = _make_build_dir(tmp_path)
    with serve(Zephyr(flash_command="echo nope; exit 7", flash_timeout=0)) as client:
        with pytest.raises(BaseException):  # noqa: B017, PT011 — RPC may wrap it
            _flash(client, build)


def test_missing_artifacts_fails_fast(tmp_path):
    """No artifacts under build_dir/zephyr => client errors before the exporter."""
    empty = tmp_path / "build"
    (empty / "zephyr").mkdir(parents=True)
    with serve(Zephyr(flash_command="true", flash_timeout=0)) as client:
        with pytest.raises(FileNotFoundError):
            _flash(client, empty)


def _argv(**kw) -> list[str]:
    """Build a twister argv via the pure helper, no RPC connection needed."""
    client = ZephyrClient.__new__(ZephyrClient)
    return client._twister_argv("plat", "/tty", ["root"], **kw)


def test_pytest_args_emitted_as_single_eq_token():
    """Each pytest arg becomes one --pytest-args=<value> token, so a value that
    starts with a dash survives twister's argparse instead of being misread as a
    twister option (the AbsoluteLinkError-adjacent `expected one argument` bug)."""
    argv = _argv(pytest_args=["--ot-hardware-map=/srv/map.yml"])
    assert "--pytest-args=--ot-hardware-map=/srv/map.yml" in argv
    # The value must NOT land as its own token next to a bare --pytest-args.
    assert "--pytest-args" not in argv
    assert "--ot-hardware-map=/srv/map.yml" not in argv


def test_multiple_pytest_args_each_get_their_own_token():
    argv = _argv(pytest_args=["-k", "my_test"])
    assert argv.count("--pytest-args=-k") == 1
    assert argv.count("--pytest-args=my_test") == 1


def test_twister_args_and_pytest_args_coexist():
    """Verbatim twister args precede the forwarded pytest args."""
    argv = _argv(
        twister_args=["-x", "my_fixture"],
        pytest_args=["--ot-hardware-map=/srv/map.yml"],
    )
    assert argv[-3:] == ["-x", "my_fixture", "--pytest-args=--ot-hardware-map=/srv/map.yml"]


def test_no_pytest_args_adds_nothing():
    assert not any(a.startswith("--pytest-args") for a in _argv())
