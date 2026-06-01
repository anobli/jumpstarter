# AM62x Driver

`jumpstarter-driver-am62x` is a SoC-level driver for **TI AM62x**.

It ships two related driver classes:

| Class       | What it is                                                                   | Use it when…                                                                       |
| ----------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `AM62xDfu`  | A `Dfu` subclass with AM62x-specific defaults (ROM VID:PID + entry sequence) | You only need DFU and want a flat client (`am62x.<dfu-method>`)                    |
| `AM62x`     | A composite that auto-wires an `AM62xDfu` child + grows SoC-level commands   | You want `am62x.dfu.<method>` and room to add SoC commands at the top level        |

Most users want **`AM62x`**.

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-am62x
```

`dfu-util` must be installed on the **exporter** machine.

## Configuration (recommended — `AM62x` composite)

```yaml
export:
  am62x:
    type: jumpstarter_driver_am62x.driver.AM62x
    children:
      power: { ref: "power" }
      boot_button: { ref: "boot_button" }
  power:
    type: jumpstarter_driver_yepkit.driver.Ykush
    config: { serial: "YK112233", port: "1" }
  boot_button:
    type: jumpstarter_driver_gpiod.driver.DigitalOutput
    config: { device: "/dev/gpiochip0", line: 17 }
```

The composite auto-creates a `dfu` child of type `AM62xDfu`, sharing
`power` and `boot_button` with it.

### Overriding the DFU defaults

If you need to tweak the DFU layer beyond what AM62x exposes (for
example, raise `enter_dfu_timeout` or replace the entry sequence), supply
an explicit `dfu:` child — the auto-creation is skipped:

```yaml
am62x:
  type: jumpstarter_driver_am62x.driver.AM62x
  children:
    power: { ref: "power" }
    boot_button: { ref: "boot_button" }
    dfu:
      type: jumpstarter_driver_am62x.driver.AM62xDfu
      config:
        enter_dfu_timeout: 30.0
        enter_dfu_sequence:
          - { call: "power.off" }
          - { call: "boot_button.on" }
          - { sleep: 0.5 }
          - { call: "power.on" }
          - { sleep: 2.0 }
          - { call: "boot_button.off" }
      children:
        power: { ref: "power" }
        boot_button: { ref: "boot_button" }
```

### Defaults baked into `AM62xDfu`

| Setting              | Value                                                                            |
| -------------------- | -------------------------------------------------------------------------------- |
| `vid_pid`            | `0451:6165` (TI ROM USB recovery)                                                |
| `enter_dfu_sequence` | `power.off → boot_button.on → sleep 0.2 → power.on → sleep 1.5 → boot_button.off` |

All other config (`enter_dfu_wait`, `enter_dfu_timeout`, `serial`,
`intf`, `dfu_util_path`) inherits from
[`jumpstarter-driver-dfu`](../jumpstarter-driver-dfu/).

## API and CLI

### Through the `AM62x` composite

```python
am62x.dfu.enter_dfu()
for chunk in am62x.dfu.download_file("tiboot3.bin", alt=1, dfuse_address="0x70000000"):
    sys.stdout.write(chunk); sys.stdout.flush()
am62x.dfu.detach()
```

```shell
$ j am62x dfu enter
$ j am62x dfu list
$ j am62x dfu download --alt 1 --address 0x70000000 tiboot3.bin
$ j am62x dfu detach
```

SoC-level commands added directly to the `AM62x` driver class show up
as `am62x.<method>` and `j am62x <subcommand>`.

### Using `AM62xDfu` directly

If you don't need the SoC composite, you can expose `AM62xDfu` at the
top level — its client behaves identically to `DfuClient`:

```yaml
am62x_dfu:
  type: jumpstarter_driver_am62x.driver.AM62xDfu
  children:
    power: { ref: "power" }
    boot_button: { ref: "boot_button" }
```

```shell
$ j am62x_dfu enter
$ j am62x_dfu download --alt 1 --address 0x70000000 tiboot3.bin
```

See the [`jumpstarter-driver-dfu`](../jumpstarter-driver-dfu/) docs for
the complete DFU API reference.
