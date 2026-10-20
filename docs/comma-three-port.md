# zoompilot on the comma three

This documents the `-tici` branches: what the comma three needs that the comma 3X and comma 4 do
not, why each difference exists, and what is still unverified.

**Status: published as `c3-zp`, and unflown.** `c3-zp` lives on `duoduo2110/openpilot` and is the
comma three branch of this fork: `develop-tici` (the original C3 port, 2026-09-09) merged onto
current `develop` (2026-10-16), with the comma three panda/opendbc pins carried on
`duoduo2110/panda:c3-zp` and `duoduo2110/opendbc:c3-zp`. Upstream retired the `-tici` branch on
2026-10-10 (`6b79bf63e`), which also removed the AGNOS boot tooling; this branch restores it.
The AGNOS boot image is built, published and verified, and `tici_agnos.json` points at it with no
placeholders left.

Install `c3-zp` through the custom software URL in the setup flow.

Nothing here has run on a physical comma three. Where a known good artifact exists the work is
checked against it, which is not the same as having driven a car. Read
[Still to verify on a device](#still-to-verify-on-a-device) before the first drive.

## Device names

comma uses codenames throughout the codebase, and they are not obvious:

| codename | product | internal panda | road cameras | display |
|---|---|---|---|---|
| `tici` | comma three | dos, STM32F413, **USB** | AR0231 | 2160x1080 |
| `tizi` | comma 3X | tres, STM32H7, SPI | OX03C10 | 2160x1080 |
| `mici` | comma four | cuatro, STM32H7, SPI | OS04C10 | 536x240 |

All three are Snapdragon 845 with an Adreno 630, which is why so much of this port is small. There
is no SoC, GPU, tinygrad-backend or userspace split to recreate. `HardwareComma` in
`openpilot/common/hardware/comma/hardware.py` covers all three.

The comma 4 additionally has a "chestnut" accelerator, but it falls back to the same `qcom` model
path the comma three uses whenever chestnut is not active, so that path is well maintained rather
than legacy.

## What upstream removed

comma dropped the comma three over three PRs in August 2025 and then refactored on top of the gap,
so a plain revert is not possible. The removals that matter:

| what | upstream commit | effect on a comma three |
|---|---|---|
| `tici` dropped from the C++ device map | `3b4077d31be58eba4208ae4aa480b499e928cf49` (#38201) | `assert()` fires, so **every native process aborts** |
| AR0231 camera driver | `1d8dc8a69a188285e65595d83224e54d497178dc` (#36070) | no cameras at all |
| tici-specific code | `3e2549f2b8675ce482200e06855857122f3583e3` (#36078) | amplifier, NVMe alert, IRQ affinity, exposure scale |
| pandad USB transport | `96d1b876bbd7441cac08bab842aded6729ae1c6d` (#37217) | cannot reach the panda, which is on USB |
| panda F4 target | panda `1ce986f7` (#2259) | no firmware exists for a dos panda |
| `comma_tici.dts` | agnos-kernel-sdm845 `b90695f101ba` | AGNOS 13+ has no device tree, so it will not boot |

sunnypilot's own answer is a frozen branch, `sync-20251218-tici`, which is sunnypilot master at
2025-12-18 with the first three reverted. It predates the `openpilot/` restructure by 1616 commits,
so nothing from it applies directly, but it is the reference for what the restored code should say.

## What this branch restores

- **`camerad`**: the AR0231 driver, restored as it was. `SensorInfo` has not changed since the
  removal, so the driver needed no adaptation. All three cameras on a comma three are AR0231, so
  this one driver covers the whole camera stack. Probed first, as upstream had it.
- **`hardware.h`**: `tici` back in the device map. Nothing else runs until this is present.
- **amplifier**: the per-device split is back. The comma three is mono and drives the right speaker
  with a different EQ; the 3X is stereo. Both configs were verified byte-identical to the
  pre-removal upstream ones by evaluating both files and comparing the expanded register lists.
- **`set_ir_power`**: guards `tici` again. The early return covered tici and tizi before removal;
  restoring only tizi left the comma three writing to LED sysfs nodes it does not have.
- **NVMe**: the storage-missing offroad alert, and loggerd added to `ignored_processes`, because
  some comma threes drop their NVMe mid-drive and crash loggerd on write. Factory reset wipes it.
  The mount itself also has to be restored. `3cc433660d` ("no nvme", 2025-08-26) dropped
  `/dev/nvme0n1 /data/media` from the AGNOS fstab, along with the `mkdir /data/media` in
  `fs_setup.sh` and `nvme-cli`, in the same commit that bumped the kernel to the pin that deletes
  `comma_tici.dts`. openpilot did not follow: `hw.py` still logs to `/data/media/0/realdata` and
  `hardwared` still raises `Offroad_StorageMissing` on a tici when that is not a mount. We do not
  ship a patched system image, so `tici_storage_init` in `launch_chffrplus.sh` mounts it on boot.
  It never formats; an unformatted drive is a factory reset and that is the user's call.
- **IRQ affinity**: the comma three's pandas are on USB, so `xhci-hcd:usb1`/`usb3` get pinned the
  way `spi_geni` is on the other devices.
- **UI**: AR0231 reports exposure on a different scale, so auto-brightness needs a 6x factor; the
  UI runs at 20 fps as on the 3X; alert volume uses the 3X calibration.
- **AGNOS**: a separate manifest, see below.
- **updater**: branch shortcuts and migrations, see below.

The **pandad USB settle** was on this list and should not have been. It was read as the 60 second
grace period alone, which does look like it has nothing left to attach to. The part that matters is
the restructure around it: upstream's flash and connect loop resets the internal panda on every
pass and never sleeps, which is fine for an H7 that is addressable again milliseconds later over
SPI, and wrong for a dos that has to re-enumerate on USB. Left alone it lists the panda while it is
still coming up, puts a working panda into DFU on the next pass, reflashes its bootstub on the one
after, and never starts pandad. `pandad.py` now waits after a reset on a USB panda, and
`reset_internal_panda`/`recover_internal_panda` hold RST_N and BOOT0 for the widths the last comma
three validated release used instead of the 10 ms an H7 needs.

An **aux panda has nowhere to live**. `sync-20251218-tici` zeroed `ignition_line_pkt` on a dos when
a red panda was present, because a harness box with no harness connector gives the dos false
ignition. pandad has been single panda since `96d1b876bb` and we pass one serial, so there is
nothing to restore, but a comma three with a red panda in a harness box is not a supported setup on
this branch and `check_panda_support()` will take whichever panda answers `is_internal()` first.

Deliberately **not** restored: the `*_tizi.wav` sound overrides,
both of which upstream removed for unrelated reasons and which no longer have anything to attach
to; and the magnetometer, whose `magnetometer` service no longer exists in `cereal/services.py`
and which nothing consumes.

## Outstanding dependencies

### 1. AGNOS boot image

comma deleted `comma_tici.dts` from the AGNOS kernel on 2025-08-26. **AGNOS 12.8 is the last
release that boots a comma three**; this branch pins 19.7.

Only the `boot` partition is affected. `xbl`, `xbl_config`, `abl`, `aop`, `devcfg` and `system` are
shared across all three devices and come from comma unchanged, so `openpilot/common/hardware/comma/tici_agnos.json`
is comma's official 19.7 manifest with a different `boot` entry **and 12.8's `abl`**. `abl` is the
one bootloader partition that is device specific: a newer one rejects the comma three outright with
`Unsupported firmware detected` before openpilot ever starts. See `docs/agnos-tici-boot.md`. `AGNOS_VERSION` stays a single
value because `/VERSION` is written by the shared system image. Manifest selection reads
`/sys/firmware/devicetree/base/model`, so it works before anything else is up
(`launch_chffrplus.sh`, `updated.py`, `agnos.py`, `tools/op.sh`).

Restoring the device tree is small: the 146-line `.dts` plus one line in the qcom `Makefile`. The
per-device DTS layer has not drifted (the 3X's DTS differs by one `qcom,msm-id` entry between the
removal commit and kernel master today), and the tici DTS's only includes and its `rpr0521` light
sensor all still exist, so it is expected to build.

**Caveat that must not be lost:** agnos-builder's public master last bumped its kernel in May 2026,
while the kernel repo has commits through September 2026 and openpilot pins AGNOS 19.7 from
2026-09-01. comma builds 19.7 from something not fully public, so **our image cannot be
bit-identical to comma's**. What we ship is the AGNOS kernel at a pinned commit plus the tici DTB,
paired with comma's official userspace. Kernel/userspace coupling is loose, but this pairing has
never run anywhere.

The `boot` entry in `tici_agnos.json` is a placeholder until the build workflow fills it in.

**Cross-checked against a working image.** FrogPilot publishes an AGNOS 18.4 boot image that comma
three users actually run (`FrogAi/FrogPilot-Resources`, `AGNOS/18.4`), and opgm's C3 branch ships
it. Downloading and inspecting it confirms the approach here:

- Its decompressed sha256 equals the hash embedded in its filename, which pins down what the
  manifest's `hash`/`hash_raw` mean: sha256 of the **decompressed** image, not the `.img.xz`.
- It carries exactly four appended DTBs, with model strings `comma tici`, `comma tizi` and
  `comma mici`. That is the same set our Makefile line produces, and it confirms these boot images
  are multi-device rather than per-device: restoring `comma_tici.dtb` to the build is all that is
  needed, and the result still boots a 3X or a comma four.

So the structure we are producing matches a known-good artifact. What remains unproven is our
specific kernel commit and toolchain, not the method.
`.github/workflows/zoompilot-agnos-tici-boot.yaml` is that workflow;
[docs/agnos-tici-boot.md](agnos-tici-boot.md) covers what it builds, how to verify the result and
how to recover a device that will not boot after flashing one.

### 2. panda firmware and pandad USB

The comma three's internal panda is a "dos", an STM32F413 talking bxCAN over **USB**. The 3X and
comma 4 use STM32H7 pandas over SPI. Upstream deleted the entire F4 target from panda and the USB
transport from pandad, so on this tree a comma three can neither flash nor talk to its panda.

Both are being restored. This is safety-critical firmware that has never been run on a car in this
form; treat the first drive accordingly.

### panda, in detail

The `c3` branch of our panda fork restores the F4 target and the dos board. It builds clean under
`-Werror` for both targets and the firmware fits comfortably: 95104 B of flash (9%) and 199168 of
262144 B RAM (76%), which `check_fw_size.py` accepts. `board/boards/board_declarations.h` had not
changed since the reference C3 branch was cut, so `dos.h` matches today's `struct board` field for
field.

Two gaps remain, both narrower than they were:

1. **`enter_stop_mode()` is a no-op on F4.** The shared implementation is now all H7 registers.
   Sleep and power-off current draw on a comma three are unmeasured.
2. **Fan stall recovery is gone.** The shared fan driver dropped the `fan_stall_recovery` and
   `fan_max_rpm` fields the dos used, and comma's own deleted test said the comma three's fans need
   it. Exercise the fan across its range on hardware.

Panda temperature reads 0 C, which is not a gap so much as the truth: the STM32F413 has no digital
temperature sensor and its analog one is unavailable while the ADC is configured for VBAT, which is
how the dos has always run.

### The CAN packet size, and what it was hiding

`CANPACKET_DATA_SIZE_MAX` is fixed at 64 in opendbc, which makes `CANPacket_t` 72 bytes. A bxCAN
controller can never carry more than 8 data bytes, so on an F4 that is 56 bytes of padding per slot,
and at upstream's ring depths it put `.bss` 66 KB past `_estack` with the stack landing inside the
rx queue. `zoompilot/opendbc` branch `c3` sizes the packet for the controller instead, which is what
the F4 build did before the target was deleted.

The same condition restores the `CANFD` gate, so `hyundai_canfd` and `volkswagen_meb` are no longer
compiled into a classic build at all rather than being accepted and failing safe at runtime.
Verified in the linked binaries: those hooks are absent from the F4 and present on the H7.

One thing this exposes: the host still describes packets with a CAN FD length code, and upstream
never bounded the copy into `CANPacket_t` because both sides always agreed on its size. They no
longer do on an F4, so `can_comms.h` now drops an over-long packet instead.

## Installing on a comma three

The branch normally has to end in `-tici`: `openpilot/common/version.py` derives `channel_type`
from that suffix, and `hardwared.py` refuses to go onroad on a comma three whose channel is not
`tici`, showing `Offroad_TiciSupport`. The branch picker also hides every branch that is not
recognized as tici, so a device on the wrong branch cannot fix itself from the screen.
`SP_BRANCH_MIGRATIONS` therefore maps `develop`, `main` and `danger-unstable` to their `-tici`
equivalents. `c3-zp` is registered explicitly in `TICI_BRANCHES` (`is_tici_branch()`), so it is
treated exactly like a `-tici` channel by the updater, `hardwared` and the branch picker.

Install `c3-zp` from `duoduo2110/openpilot` via the custom software URL in the setup flow. It is the
`c3-zp` fork branch, built from `develop-tici` merged onto current `develop`, with the comma three
panda/opendbc pins carried in `duoduo2110/panda` and `duoduo2110/opendbc`.

Do **not** put a comma three on `main`. That branch is prebuilt, and the driving model pkl is
compiled for a single camera resolution chosen by the machine that built it. A source build on the
device itself picks the correct 1928x1208 model automatically, so the comma three is a source
channel by design.

## Known limitations, and parity with the comma 3X

The bar for this port is parity with the comma 3X, not with upstream. These are equal on both, so
they are documented rather than fixed here:

- Early comma threes shipped a BMX055 IMU, see below. Everything else that was at parity has been
  fixed rather than documented, because only comma threes install this branch.

`BIG_UI` was the exception worth fixing. It was read from an environment variable that nothing sets
on a device, so `FONT_SCALE`, the default font weight and all of `system/ui/text.py` sized for the
comma four's 536x240 panel on a 2160x1080 screen. It now includes the comma three directly.
The comma 3X has the same bug, and is deliberately left alone here rather than
restyling every existing 3X install from a comma three branch. Candidate to upstream.
- Early comma threes shipped a BMX055 IMU. The Python `sensord` rewrite supports only LSM6DS3, and
  the C3-validated sunnypilot branch dropped BMX055 too, so those units are no better off there.
  `openpilot/sunnypilot/system/sensord/` contains BMX055 drivers but is not wired into
  `process_config.py` and is dead code.

## Effect on the comma 3X and comma 4

Every change was reviewed for impact on the other two devices, because this branch is meant to
merge back rather than diverge:

- The 3X amplifier register list was verified byte-identical to the flat one it replaced, by
  evaluating both files and comparing the expanded configs. The comma 4 has no amplifier and never
  reaches that code.
- `selfdrived`'s `ignored_processes` still resolves to exactly `{'mapd'}` off a comma three.
- The AR0231 exposure scale is `1.0` for any other sensor, so brightness is unchanged.
- `tici`, `TICI` and `-tici` gates are additive everywhere else: device map, loggerd geometry,
  soundd, UI frame rate, offroad alerts, branch picker, branch migrations, installer.
- AR0231 is probed first, which is the order upstream shipped for years with all three sensors. A
  failed probe falls through to the next sensor.
- `tici_reset.py` serves both the comma three and the 3X. The NVMe wipe is best effort and fails
  harmlessly on a 3X, which has no such device node. This matches what upstream did.
- The amplifier test now skips where there is no amplifier instead of raising `KeyError`.

## Static analysis

MISRA runs against the F4 target now; it never had. The F4 line in
`panda/tests/misra/test_misra.sh` passed only `board/stm32f4/inc/`, but `core_cm4.h` takes the
CMSIS headers from `board/stm32h7/inc/` through the compiler's `CPPPATH`, so every `__disable_irq`
and register accessor read as an implicit declaration: 49 of 51 findings, hiding the real ones.

With that fixed, three findings were ours and are gone (a 10.6 essential type widening in
`comms_can_write`, and 8.5 twice from the F4 block in `drivers.h` re-declaring `cans[]`,
`process_can` and `can_init`). Eight advisory findings remain on F4, all 17.3 and 8.7 in
declaration headers, none of them behavioural. The H7 target is clean, and so is unmodified
`sunnypilot/panda` master, which is the control that says the rest is attributable to us.

Run it with comma's pinned cppcheck (`cppcheck @ git+https://github.com/commaai/dependencies.git@release-cppcheck`),
`CPPCHECK_DIR` pointed at its `install` directory and `PYTHONPATH` at `opendbc_repo`.

## Still to verify on a device

Nothing here has touched a comma three. In rough order of how likely it is to bite:

1. The AGNOS boot image boots at all. A bad boot partition bricks the device until it is reflashed
   over USB.
2. The panda flashes, enumerates over USB, and passes safety checks.
3. camerad brings up three AR0231s. The BPS path gained gamma and linearization LUT programming
   and a new black-level knee calculation since the AR0231 was removed, and the AR0231's LUTs have
   never been exercised against it, so image quality needs a look even if frames arrive.
4. The amplifier register set produces sane audio.
5. Thermals and the 20 fps UI under load.
6. Whether the device has enough RAM to build openpilot on itself.
7. The first onroad screenshot. Several big-UI onroad renderers load textures out of `icons_mici/`
   at comma four pixel sizes (`blind_spot_indicators.py`, `turn_signal.py`, `augmented_road_view.py`,
   `sidebar.py`, `trips.py`). They load fine and are simply small on a 2160x1080 panel. This is
   inherited from sunnypilot and looks the same on a comma 3X, so it is left alone rather than
   retuned blind, but it is the obvious thing to check once someone can see the screen.
