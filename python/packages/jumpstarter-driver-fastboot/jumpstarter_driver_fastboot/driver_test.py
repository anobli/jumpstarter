import asyncio

import pytest

from .driver import Fastboot
from jumpstarter.common.utils import serve


def test_drivers_fastboot():
    """Driver instantiates and exposes a working state interface."""
    instance = Fastboot()
    with serve(instance) as client:
        assert client.state().ok
        _ = client.snapshot()


def test_match_args_empty():
    assert Fastboot()._match_args() == []


def test_match_args_with_serial():
    assert Fastboot(serial="abc123")._match_args() == ["-s", "abc123"]


def test_flash_local_without_configured_path_raises():
    """Calling flash() without a file and without a configured path fails fast."""
    inst = Fastboot()  # no `partitions` config

    async def _go():
        async for _ in inst.flash("boot"):
            pass

    with pytest.raises(RuntimeError, match="no exporter-local path"):
        asyncio.run(_go())
