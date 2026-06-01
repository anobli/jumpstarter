# Fastboot Driver

`jumpstarter-driver-fastboot` wraps the Android `fastboot` CLI on the
exporter host. The DUT must already be in fastboot mode for this driver
to do anything — typically that means pairing it with something that
gets the DUT there, e.g. running `j am62x boot-to-fastboot` or sending
`fastboot 0` from a U-Boot prompt yourself.

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-fastboot

# Optional: for YAML manifest support (JSON works without this)
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-fastboot[yaml]
```

`fastboot` (from `android-tools`) must be installed on the **exporter**
machine — typically `apt install android-tools-fastboot` or
`brew install android-platform-tools`.

## Configuration

```yaml
export:
  fastboot:
    type: jumpstarter_driver_fastboot.driver.Fastboot
    config:
      # All fields below are optional.
      serial: "1234567890"
      partitions:
        bootloader: /var/lib/jumpstarter/firmware/bootloader.img
        boot:       /var/lib/jumpstarter/firmware/boot.img
        super:      /var/lib/jumpstarter/firmware/super.img
      fastboot_path: /usr/local/bin/fastboot
```

### Config parameters

| Parameter       | Description                                                                              | Type           | Required | Default    |
| --------------- | ---------------------------------------------------------------------------------------- | -------------- | -------- | ---------- |
| `fastboot_path` | Path to the `fastboot` binary on the exporter                                            | str            | no       | `fastboot` |
| `serial`        | `-s <serial>` filter (device serial or USB path like `usb:1-2.3`) for multi-DUT setups   | str            | no       | —          |
| `partitions`    | Default `partition -> absolute exporter-local path`. Used when `flash()` is called without a file | dict[str,str]  | no       | `{}`       |

## FastbootClient API

```{eval-rst}
.. autoclass:: jumpstarter_driver_fastboot.client.FastbootClient()
    :members: devices, getvar, flash, flashall, erase, reboot, set_active
```

`flash`, `flashall`, `erase`, `reboot`, and `set_active` are **streaming**: they
return a generator yielding chunks of fastboot's combined
stdout/stderr as it arrives. `getvar` and `devices` are blocking —
they capture the full output and return parsed values.

```python
# Upload an image from the client and flash it
for chunk in fastboot.flash("boot", "boot.img"):
    sys.stdout.write(chunk); sys.stdout.flush()

# Use an exporter-baked image (config.partitions[boot])
for chunk in fastboot.flash("boot"):
    sys.stdout.write(chunk); sys.stdout.flush()

# Flash multiple partitions from a manifest bundle
# Can use either a directory or zip file
for chunk in fastboot.flashall("firmware-bundle/"):  # directory (auto-compressed)
    sys.stdout.write(chunk); sys.stdout.flush()

for chunk in fastboot.flashall("firmware-bundle.zip"):  # or pre-made zip
    sys.stdout.write(chunk); sys.stdout.flush()

# Misc
fastboot.set_active("a")
for chunk in fastboot.reboot(): pass
print(fastboot.getvar("version"))
```

## CLI

```shell
$ j fastboot --help
Usage: j fastboot [OPTIONS] COMMAND [ARGS]...

Commands:
  devices     List devices in fastboot mode
  erase       Erase PARTITION
  flash       Flash PARTITION with FILE.
  flashall    Flash all partitions from a manifest bundle.
  getvar      Query a fastboot variable
  reboot      Reboot the device (TARGET: bootloader/recovery/fastboot/none)
  set-active  Set the active boot slot (A/B systems)
```

### Examples

```shell
$ j fastboot devices
0a3b8b3c        fastboot

$ j fastboot getvar version-bootloader
2024.04-rc1

$ j fastboot flash boot ~/builds/boot.img       # uploads from client
$ j fastboot flash super                        # uses exporter-local config

$ j fastboot flashall firmware-bundle.zip       # flash from zip file
$ j fastboot flashall firmware-bundle/          # flash from directory (auto-compressed)
$ j fastboot flashall firmware-bundle/ --wipe   # with userdata wipe

$ j fastboot erase userdata
$ j fastboot set-active a
$ j fastboot reboot
```

## Creating a Flashall Bundle

The `flashall` command accepts either:
- **A directory** containing manifest.yaml (or .json) and images - automatically compressed before upload (**best for development**)
- **A zip file** containing the manifest and images (**best for CI/CD**)

### Manifest Format

The manifest is a simple flat dictionary mapping partition names to filenames:

**manifest.yaml:**
```yaml
boot_a: boot.img
boot_b: boot.img
vendor_boot_a: vendor_boot.img
vendor_boot_b: vendor_boot.img
super: super.img
vbmeta_a: vbmeta.img
vbmeta_b: vbmeta.img
userdata: userdata.img
```

**manifest.json:**
```json
{
  "boot_a": "boot.img",
  "boot_b": "boot.img",
  "super": "super.img",
  "userdata": "userdata.img"
}
```

### Developer Workflow: Using a directory

```shell
# Your Android build output directory already has the images
cd ~/android/out/target/product/mydevice/

# Create a simple manifest.yaml
cat > manifest.yaml << 'EOF'
boot_a: boot.img
boot_b: boot.img
super: super.img
userdata: userdata.img
EOF

# Flash directly from the build directory (auto-compressed and uploaded)
j fastboot flashall .

# Or flash with userdata wipe
j fastboot flashall . --wipe
```

### CI Workflow: Using a zip artifact

```shell
# In your CI pipeline, create a zip bundle
cd build-output/
zip -r firmware-bundle.zip manifest.yaml *.img

# Later, flash from the artifact
j fastboot flashall firmware-bundle.zip --wipe
```

### Additional Commands for CI

For a complete CI flash sequence, you may need additional fastboot commands:

```shell
# Format device (creates GPT partition table)
j fastboot oem format  # Note: 'oem' command not yet implemented in driver

# Erase misc partition
j fastboot erase misc

# Flash all images with wipe
j fastboot flashall firmware-bundle.zip --wipe

# Reboot to bootloader (if needed between operations)
j fastboot reboot bootloader
```
