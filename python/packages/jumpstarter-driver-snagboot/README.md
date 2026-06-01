# Snagboot Driver

`jumpstarter-driver-snagboot` wraps Bootlin's
[snagboot](https://github.com/bootlin/snagboot) tool suite on the
exporter host. Snagboot covers SoC USB recovery and flashing for
families like TI K3 (AM62x/AM64x/J7…), STM32MP, NXP iMX, RK35xx, and
others.

The current release exposes only `snagrecover` (initial USB-ROM
recovery — push tiboot3 / tispl / u-boot etc. into a SoC sitting in
USB boot mode). Other commands (`snagflash`, …) will follow.

The driver does **not** drive the SoC into USB ROM boot mode itself —
the DUT must already be in DFU/USB-recovery state when `snagrecover`
runs. Use [`jumpstarter-driver-dfu`](../jumpstarter-driver-dfu/) (or a
SoC-specific composite like
[`jumpstarter-driver-am62x`](../jumpstarter-driver-am62x/)) with an
`enter_dfu_sequence` to do that step first.

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-snagboot
```

`snagboot` (which provides the `snagrecover` binary) must be installed
on the **exporter** machine — typically via `pipx install snagboot`.

## Configuration

```yaml
export:
  snagboot:
    type: jumpstarter_driver_snagboot.driver.Snagboot
    config:
      soc: am62x
      # snagrecover_path: /usr/local/bin/snagrecover
```

### Config parameters

| Parameter           | Description                                                                              | Type | Required          | Default       |
| ------------------- | ---------------------------------------------------------------------------------------- | ---- | ----------------- | ------------- |
| `soc`               | SoC name as understood by `snagrecover -s` (`am62x`, `stm32mp25`, `imx8mp`, …)           | str  | for `snagrecover` | —             |
| `snagrecover_path`  | Path to the `snagrecover` binary on the exporter                                         | str  | no                | `snagrecover` |

`soc` lives in the exporter config rather than as a per-call argument
because it doesn't change between invocations — it's a property of the
DUT wired to that exporter.

## SnagbootClient API

```{eval-rst}
.. autoclass:: jumpstarter_driver_snagboot.client.SnagbootClient()
    :members: snagrecover
```

`snagrecover()` is **streaming**: it returns a generator that yields
chunks of snagrecover's combined stdout/stderr output as they arrive.

```python
for chunk in snagboot.snagrecover({
    "tiboot3": "tiboot3.bin",
    "tispl":   "tispl.bin",
    "u-boot":  "u-boot.img",
}):
    sys.stdout.write(chunk); sys.stdout.flush()
```

The role names (`tiboot3`, `tispl`, `u-boot`, `fsbl`, …) are passed
through verbatim into the YAML config snagrecover consumes — see the
[snagboot docs](https://docs.bootlin.com/snagboot/) for the full list
of roles per SoC.

## CLI

```shell
$ j snagboot recover \
    -f tiboot3=tiboot3.bin \
    -f tispl=tispl.bin \
    -f u-boot=u-boot.img
```

Each `-f role=path` supplies one firmware image. The SoC is taken from
the exporter config, not the CLI.
