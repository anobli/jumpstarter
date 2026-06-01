# DFU Driver

`jumpstarter-driver-dfu` wraps the [`dfu-util`](https://dfu-util.sourceforge.net/)
CLI on the exporter host to flash devices that are in DFU mode over USB.

It is typically the first link in an Android / embedded-Linux flashing chain:
put the SoC into DFU mode (e.g. by power-cycling while a boot-strap pin is
asserted), use this driver to push the bootloader, then continue the flow
with U-Boot or `fastboot`.

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-dfu
```

`dfu-util` itself must be installed on the **exporter** machine
(e.g. `apt install dfu-util` or `brew install dfu-util`).

## Configuration

Minimal configuration — just wraps `dfu-util`:

```yaml
export:
  dfu:
    type: jumpstarter_driver_dfu.driver.Dfu
    config:
      vid_pid: "0483:df11"      # all fields below are optional
      serial: "3271334D3038"
      intf: 0
      dfu_util_path: /usr/local/bin/dfu-util
```

Configuration with **DFU-mode entry orchestration**: wire the driver to a
`power` child and a GPIO child (boot strap or button), then describe the
sequence the driver should run when you call `enter_dfu()`:

```yaml
export:
  dfu:
    type: jumpstarter_driver_dfu.driver.Dfu
    children:
      power: { ref: "power" }
      boot_button: { ref: "boot_button" }
    config:
      vid_pid: "0483:df11"
      enter_dfu_sequence:
        - { log: "Power off and assert boot strap" }
        - { call: "power.off" }
        - { call: "boot_button.on" }       # press / assert
        - { sleep: 0.2 }
        - { call: "power.on" }             # USB re-enumerates here
        - { sleep: 1.5 }
        - { call: "boot_button.off" }      # release
      enter_dfu_wait: true
      enter_dfu_timeout: 15.0
```

A complete example is in
[`examples/exporter.yaml`](./examples/exporter.yaml).

### Config parameters

| Parameter             | Description                                                                                                              | Type           | Required | Default    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------- | -------- | ---------- |
| `dfu_util_path`       | Path to the `dfu-util` binary on the exporter                                                                            | str            | no       | `dfu-util` |
| `vid_pid`             | Default `-d VID:PID` filter (lowercase hex, e.g. `0483:df11`)                                                            | str            | no       | —          |
| `serial`              | Default `-S serial` filter                                                                                               | str            | no       | —          |
| `intf`                | Default `-i intf` filter (interface number)                                                                              | str            | no       | —          |
| `enter_dfu_sequence`  | List of steps to run from `enter_dfu()` (see below)                                                                      | list[dict]     | no       | `[]`       |
| `enter_dfu_wait`      | After running the sequence, poll `dfu-util -l` until a matching device appears                                           | bool           | no       | `true`     |
| `enter_dfu_timeout`   | How long to wait for the DFU device to enumerate after the sequence runs (seconds)                                       | float          | no       | `15.0`     |

`vid_pid` / `serial` / `intf` are the defaults for every call; per-call
arguments on the client API override them. They also drive the device
match used by `enter_dfu_wait` and by `wait_for_device()`.

### `enter_dfu_sequence` steps

Each entry is a single-key dict:

| Step shape                                           | What it does                                                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `{ call: "<child>.<method>", args: [...] }`          | Look up `self.children["<child>"]`, call its `<method>` with `args`. `args` is optional. Awaits if it's a coroutine. |
| `{ sleep: <seconds> }`                               | Sleep (float seconds).                                                                                             |
| `{ log: "<message>" }`                               | Log an info message — useful for tracing the orchestration.                                                        |

Any child driver method can be referenced — e.g. `power.on`, `power.off`,
`power.cycle`, `boot_button.on`, `boot_button.off`, `usb_mux.select`, etc.
Both `jumpstarter-driver-power` and `jumpstarter-driver-gpiod`'s
`DigitalOutput` expose `on()` / `off()` with the same shape, which is why
the same step format covers both.

## DfuClient API

```{eval-rst}
.. autoclass:: jumpstarter_driver_dfu.client.DfuClient()
    :members: list_devices, enter_dfu, wait_for_device, download, download_file, detach
```

The `download` / `download_file` methods are **streaming**: they return a
generator that yields chunks of `dfu-util`'s output (including its
`\r`-animated progress bar) as they arrive. Iterate to consume the flash:

```python
for chunk in dfu.download_file("u-boot.img", alt=2, dfuse_address="0x80000000"):
    sys.stdout.write(chunk)
    sys.stdout.flush()
```

## CLI

```shell
$ j dfu --help
Usage: j dfu [OPTIONS] COMMAND [ARGS]...

  DFU client (wraps dfu-util on the exporter)

Commands:
  detach    Tell the DUT to leave DFU mode (dfu-util -e)
  download  Flash FILE to the DUT via DFU
  enter     Run the configured sequence to put the DUT into DFU mode
  list      List devices in DFU mode visible to the exporter
  wait      Wait for a matching device to appear in DFU mode
```

### Examples

List the DFU devices the exporter can see:

```shell
$ j dfu list
[0483:df11] alt=0, name='@Internal Flash  /0x08000000/04*016Kg,01*064Kg,07*128Kg', serial=3271334D3038
[0483:df11] alt=1, name='@Option Bytes  /0x1FFF7800/01*40 e', serial=3271334D3038
```

Flash a U-Boot image to a DfuSe-style target on alt 1:

```shell
$ j dfu download --alt 1 --address 0x80000000 u-boot.img
```

Match a specific board on a multi-DUT exporter:

```shell
$ j dfu download --vid-pid 0483:df11 --serial-num 3271334D3038 \
    --alt 2 --address 0x80300000 tf-a.stm32
```

Leave DFU mode after the last image:

```shell
$ j dfu detach
```

### Examples (cont.)

Put the DUT in DFU mode using the configured sequence, then flash:

```shell
$ j dfu enter
DFU device detected: 0483:df11

$ j dfu download --alt 1 --address 0x80000000 u-boot.img
$ j dfu detach
```

From Python:

```python
dfu.enter_dfu()                     # runs configured power/gpio sequence
                                    # and waits for the DUT to enumerate

for chunk in dfu.download_file("u-boot.img", alt=1, dfuse_address="0x80000000"):
    sys.stdout.write(chunk); sys.stdout.flush()

dfu.detach()
```

If you'd rather drive the sequence yourself (e.g. the exporter has the
power/gpio drivers but you want different timing per call site), skip
`enter_dfu_sequence` and use the leaf drivers directly — `enter_dfu()`
is a convenience for the common case.

This pattern is what the higher-level `jumpstarter-driver-android-flasher`
composite (forthcoming) automates from a manifest.
