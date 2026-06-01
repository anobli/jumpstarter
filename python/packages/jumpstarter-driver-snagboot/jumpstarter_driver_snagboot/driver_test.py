import asyncio

import pytest
import yaml

from .driver import Snagboot
from jumpstarter.common.utils import serve


def test_drivers_snagboot():
    """Driver instantiates and exposes a working state interface."""
    instance = Snagboot(soc="am62x")
    with serve(instance) as client:
        assert client.state().ok
        _ = client.snapshot()


def test_build_config_shape():
    """The generated YAML matches what snagrecover -f expects: a flat
    role → {path: ...} map (no SoC key on top — that comes from -s)."""
    cfg = Snagboot._build_config(
        {"tiboot3": "tiboot3.bin", "tispl": "tispl.bin", "u-boot": "u-boot.img"},
    )
    assert cfg == {
        "tiboot3": {"path": "tiboot3.bin"},
        "tispl": {"path": "tispl.bin"},
        "u-boot": {"path": "u-boot.img"},
    }
    text = yaml.safe_dump(cfg, sort_keys=False)
    assert "tiboot3" in text
    assert "u-boot" in text


def test_build_config_empty_firmware():
    """Empty firmware mapping yields an empty dict (caller validates)."""
    assert Snagboot._build_config({}) == {}


def test_snagrecover_without_soc_raises():
    """Calling snagrecover with no SoC configured fails fast."""
    inst = Snagboot()  # no soc

    async def _go():
        gen = inst.snagrecover([["tiboot3", "handle1", "tiboot3.bin"]])
        # The generator's first iteration triggers the body
        async for _ in gen:
            pass

    with pytest.raises(RuntimeError, match="'soc' is not configured"):
        asyncio.run(_go())


def test_snagrecover_local_without_configured_firmware_raises():
    """Empty/no `firmware` arg falls through to local mode, which
    requires the driver-level `firmware` config to be set."""
    inst = Snagboot(soc="am62x")  # no `firmware` config

    async def _go(arg):
        async for _ in inst.snagrecover(arg):
            pass

    with pytest.raises(RuntimeError, match="no firmware provided"):
        asyncio.run(_go(None))
    with pytest.raises(RuntimeError, match="no firmware provided"):
        asyncio.run(_go([]))
