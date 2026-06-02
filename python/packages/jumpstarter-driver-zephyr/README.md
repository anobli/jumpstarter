# jumpstarter-driver-zephyr

Flash Zephyr firmware to a board attached to a Jumpstarter **exporter**, while
the test runner (twister) stays on the **client / CI** machine.

This is the flash half of the "server-side twister" workflow: twister runs
locally, builds the firmware, and for each test ships only the built image(s) to
the exporter. The exporter owns *how* to flash — the flash command and the
board's probe/target configuration live in this driver's config, so the client
is board-agnostic.

## How it fits together

```
jmp shell --selector <board>
└── j zephyr twister --platform <platform> --twister-out <dir> -T <tests>
    └── west twister --test-only --device-testing -p <platform> \
            --device-serial <pty> --flash-command jmp_flash_wrapper.sh
        └── (per testcase) j zephyr flash --build-dir <dir>
            └── (this driver, client side) tar zephyr.hex/.bin/.elf → exporter
                └── (this driver, exporter side) run the configured flash command
```

Everything runs inside one `jmp shell` lease; the flash step attaches to that
lease via `JUMPSTARTER_HOST`.

## Installation

```bash
pip install git+https://github.com/anobli/jumpstarter-driver-zephyr.git
```

This installs the Zephyr driver and its `j zephyr` client commands (`flash` and
`twister`).

## Exporter configuration

The flash command runs with its working directory set to the directory holding
the uploaded firmware, so it can reference files by bare name (`zephyr.hex`) or
via the `{hex}` / `{bin}` / `{elf}` / `{dir}` tokens (absolute paths).

```yaml
export:
  zephyr:                       # the `twister` subcommand calls `j zephyr flash`,
                                # so the instance must be named `zephyr`
    type: jumpstarter_driver_zephyr.driver.Zephyr
    config:
      flash_command: >-
        openocd -f interface/cmsis-dap.cfg -f target/cc13x2_cc26x2.cfg
        -c "program {hex} verify reset exit"
      flash_timeout: 300        # seconds; 0 disables the timeout
  # ... plus a serial driver, bridged to a pty for twister's --device-serial
```

The board must be left **reset and running** after flashing so twister captures
the boot output over serial (e.g. openocd's `reset` above).

## Configuration reference

| Field           | Required | Default | Description                                                        |
| --------------- | -------- | ------- | ------------------------------------------------------------------ |
| `flash_command` | yes      | —       | Shell command to flash the board. Supports `{hex}/{bin}/{elf}/{dir}` tokens. |
| `flash_timeout` | no       | `300`   | Seconds before the flash command is killed (`timeout`). `0` disables. |

## Usage with twister

The `twister` subcommand drives `west twister` for you: it bridges a serial
driver child to a pty for `--device-serial`, and points twister's
`--flash-command` at the bundled flash wrapper (which calls `j zephyr flash`
per testcase).

```bash
jmp shell --selector <board>
# then:
j zephyr twister --platform <platform> --twister-out <dir> -T <tests>
```

## Manual use / debugging

Inside a `jmp shell` you can flash a build directory directly:

```bash
jmp shell --selector <board>
# then:
j zephyr flash --build-dir twister-out/<platform>/<test>
```

## Image contract

The client uploads the fixed-name artifacts found under `<build_dir>/zephyr/`:
`zephyr.hex` and, when present, `zephyr.bin` / `zephyr.elf`. Single-image boards
are covered. Multi-image / MCUboot / partitioned flows are a known limitation of
the fixed-name convention.

## License

Apache-2.0
