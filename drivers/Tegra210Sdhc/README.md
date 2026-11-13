# Tegra210Sdhc (nvtsdhc)

SD host miniport for `sdport.sys`, for the Switch's microSD slot (Tegra210 SDMMC1, `ACPI\NVDA0101`).

- Based on the SDHC miniport in Microsoft's Windows-driver-samples (`sd/miniport/sdhc`, MIT license).
- Tegra210 limits: 3.3 V, legacy/high speed only (`NV_T210_SAFE_BRINGUP`), no UHS-I voltage switch or tuning.
- `NV_T210_PIO_ONLY` (default off): all data goes through the data port, never DMA. On Windows 11 22621 the
  inbox sdbus could not mount the boot volume on this non-coherent controller (`_CCA = 0`).
- Build: `tools\Build-Driver.ps1 -Wdm -Name nvtsdhc -Sources drivers\Tegra210Sdhc\src\nvtsdhc.c -Libs sdport.lib -Inf drivers\Tegra210Sdhc\nvtsdhc.inf -OutDir build\Tegra210Sdhc`

The UEFI does not describe the SD host yet. `loader\sd-acpi-chainloader.zig` (built as `SDLOADAA64.EFI`,
placed at `EFI\Boot\bootaa64.efi`) installs `acpi\SDH0-Test.asl` as an SSDT and then starts Windows Boot Manager.

Boot diagnostics (`NV_DIAG_STOP`, default on while bringing it up): if fewer than 8 blocks have been read
2.5 s after `DriverEntry`, the driver bugchecks with stop code `0xDSFCCEEK` (stage, flags, last command,
last error command, error kind; see the comment at the top of `src/nvtsdhc.c`). Card detect always reports
a card, since the Switch wires it to a GPIO.

## Switch Lite (HDH-001) read test

For a RAM-booted WinPE that only needs to read its deployment card, add
`-Defines NV_T210_LITE_READ_ONLY=1` to the PowerShell build command above and use a separate output directory.
Use firmware that exposes the external slot as `ACPI\NVDA0101` with `_CCA = 0`, leaves it at 3.3 V
and selects PLLP for SDMMC1. With WinX's SDM1 already present, load the boot manager directly;
adding the example SSDT would describe the same controller twice. Inject the resulting signed package
into the ARM64 WinPE image before boot.

The profile reads the firmware's CAR divider, uses 1- or 4-bit default-speed PIO up to 25 MHz,
and reports write protection to SDPORT. Data-out, block writes, write-protect changes and erase
are rejected before a request is queued. The diagnostic stop timer and crash-dump path are disabled.
For an on-device read check, place a marker file on the external FAT volume, boot to the WinPE RAM
disk, and read that file from the external volume. One HDH-001 running 22621.1 passed this check
with the development build. The default ADMA configuration was outside that test.

## Restricted WINARM writes on HDH-001

Build with `NV_T210_LITE_WINARM_WRITE=1`. The default card profile presents the 80 GiB NTFS partition `WINARM` as the sole, active MBR entry. Windows accepts block writes only inside that partition after checking its identity. Hekate still reads the physical FAT32 volume.

To also mount FAT32 in Windows, pass `--expose-fat32` to the generator. This experimental profile projects FAT32 as the second MBR entry and permits writes there only after validating its extent and boot sector. Earlier trials reported stalls, but the latest owner comparison found similar responsiveness with FAT32 visible or hidden. No reproducible performance difference or root cause is established; the current card uses the visible profile.

The generator reads a saved whole-card image and validates its hybrid MBR, primary GPT and NTFS boot sector. The tested layout places WINARM in MBR slot 4 and GPT slot 11. Both profiles protect the physical partition tables and other partitions from writes. Keep the generated identifiers private.

Build BL33 and the SD miniport from **the same generated header**, and deploy them as a pair. Mixing a one-volume firmware with a two-volume miniport caused `INACCESSIBLE_BOOT_DEVICE` (0x7B). The card comparison used miniport 0.3.2.5 (WINARM only) and 0.3.2.6 (WINARM + FAT32). Source version 0.3.2.7 supports both profiles.

From the repository root in PowerShell:

```powershell
$sd = 'drivers/Tegra210Sdhc'
py -3 "$sd/winarm/configure-target.py" 'D:/images/test-card.img' "$sd/winarm/generated"
Copy-Item "$sd/winarm/generated/winarm-target.h" "$sd/src/"
./tools/Build-Driver.ps1 -Wdm -Name nvtsdhc -Sources "$sd/src/nvtsdhc.c" -Libs sdport.lib -Defines 'NV_T210_LITE_WINARM_WRITE=1' -Inf "$sd/winarm/nvtsdhc.inf" -OutDir build/WINARM-R3
```

For WinPE deployment, use the matching R3 BL33, generated startnet.cmd and the signed test package. Verify the final SYS and INF against their catalog using a test certificate valid at the firmware's cold-start clock. The hardware build and test steps are in the [R3 source tree](https://github.com/ruje0504-2/winx/tree/b44da7475fb281d6e4390d976f8f8e0098154d87/docs/hdh001-winarm-r3). One HDH-001 verified the 4 KiB receipt file after shutdown and a second boot. [Hardware report](https://github.com/steck0714/winx/issues/5)

### T214 SDR104 candidate

The `NV_T214_SDR104=1` bounded WINARM profile adds B01 SDMMC1 signaling
voltage switching, pad calibration, hardware CMD19 tuning and a 199.68 MHz
PLLC4 OUT2 clock. SDR50, DDR50 and proprietary DDR200 are not advertised.
It requires the accompanying `tegra210i2c` 1.0.0.5 package: both drivers
serialize I2C5 access while the boot miniport adjusts the SD LDO2 rail.

Large requests can use a private noncached, below-4-GiB ADMA2 buffer. The
existing MBR presentation and per-card write bounds are applied before
completion; the default WINARM-only view is unchanged. Allocation or mapping
constraints keep PIO available. Controller errors are returned to SDPORT
and a failed DMA operation requires a completed DAT reset before reuse.

Hardware result for 0.4.0.1: Windows reached the desktop and the returned
SYSTEM hive reports `BusSpeed=7` (SDR104), `Signaling18V=1`, `BusWidth=4`,
`ClockKhzActual=199680`, `Tuned=1`, `TuneStatus=0`, 128 tuning iterations
and tap 64. ADMA2 completed 1,867,466 blocks with `AdmaErrors=0`; voltage
switching and pad calibration recorded no failures or fallbacks. These are
driver/host readbacks from the observed run, not a standalone throughput
benchmark or extended integrity/stress qualification.

ARM64 builds and host DMA/filter/write-bound tests pass. Read `Signaling18V`,
`Tuned`, `TuningTap`, `ClockKhzActual`, `AdmaBlocks` and `AdmaErrors` under
`Services\nvtsdhc\Parameters\Diag`.

Hardware references: [Hekate SDMMC](https://github.com/CTCaer/hekate/blob/v6.5.3/bdk/storage/sdmmc_driver.c),
[Hekate clock setup](https://github.com/CTCaer/hekate/blob/v6.5.3/bdk/soc/clock.c),
and [Switchroot Linux](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/linux-dev/drivers/mmc/host/sdhci-tegra.c).

Version 0.4.0.1 repairs a reset-all power-state mismatch: the card's external
rail may remain on while SDHCI `POWER_CONTROL` has reset to zero. A subsequent
3.3 V request now restores and checks the controller power bits instead of
returning early. The regression reproduces that cleared-register state and
also checks a real power cycle when leaving 1.8 V. This fix passes the host
regression and ARM64 build; the owner subsequently reached Windows with 0.4.0.1. Its signaling, tuning
and ADMA readbacks are recorded above; sustained throughput is still unmeasured.

### FAT32-visible and resume candidate (0.4.0.3)

The owner requested another FAT32-visible trial using SDR104/ADMA2, preserving
the hidden profile. Both builds share the same validated write bounds. DMA
read completion now dispatches identity checks only for the few relevant
metadata sectors, rather than once per returned sector. Tests compare the
result with the previous sector-by-sector path across 1–128-block reads in
both partition layouts. The card's FAT32 FSInfo signatures and free-count
field were valid; no FSInfo metadata is rewritten or fabricated.

Version 0.4.0.3 also removes synchronous registry writes from SDPORT callbacks.
A queued worker writes an immutable copy, so it never retains the slot extension
or re-enters the boot disk from a bus/request callback. I/O statistics refresh
at most once per 30 seconds of active traffic, rather than every 1,024 commands.
Clock, identity and error transitions still request a snapshot. There is no
new timer or log file. The focused queue test covers callback re-entry, snapshot
lifetime, busy-traffic throttling and registry-open failure.

The miniport also saves the quiesced SDHCI host/power/clock/interrupt settings
and T214 tuning registers. Resume restores configuration with SDCLK held off
until the internal clock is stable; commands, FIFO contents, DMA pointers and
old interrupt status are never replayed. A clock failure closes the DMA gate.
This covers controller context loss with card power retained, not a card power
cycle or platform-wide power loss. Fixed `ContextSaves`,
`ContextRestores` and `ResumeStatus` values join the existing diagnostic key.

The ARM64 builds and ASan/UBSan DMA/filter/write-bound/context regressions pass.
This is a performance and resume candidate: physical desktop responsiveness
and repeated sleep/wake still require measurement. Use the matching
FAT32-visible header in BL33 and the miniport; do not mix the two layouts.

### Runtime transfers and hidden-FAT comparison

Switchroot's [B01 microSD configuration](https://github.com/CTCaer/switch-l4t-platform-t210-nx/blob/linux-5.1.2/kernel-dts/nx-platforms/tegra210b01-nx-common.dtsi)
sets an 8 MiB ADMA request limit. This port uses a 512 KiB runtime buffer/request
limit, with a 64 KiB allocation fallback. Descriptors retain nonzero 32 KiB
segments and checked write bounds. Bounded diagnostics include `AdmaBufferBytes`,
`AdmaRequests`, `AdmaMaxBlocks` and `PioFallbackRequests`.

Both hidden and visible builds retain SDR104/ADMA2 and the same runtime
transfer limit. Earlier comparisons reported roughly one-minute stalls, but
on the latest trial the owner found no clear responsiveness difference.
The November 7 trial kept FAT32 visible with package 0.5.0.3 and a matching
BL33; the later November 12 inventory below identifies 0.4.0.1. This is not evidence of
a completed performance fix. Keep both source profiles available.

Candidate 0.4.2.0 removed the cancelled S4 experiment. That version did
not advertise dump/hibernate support, allocate a dump DMA buffer or bypass the
I2C gate in a dump path. Version 0.5.1.0 adds the separate dump path described
below. Normal SDR104, card identity/write bounds and the
retained-power controller-context helpers remain. Those helpers do not provide
whole-system sleep or re-identification after card power loss.

Build with `NV_T210_LITE_WINARM_WRITE=1` and `NV_T214_SDR104=1`, using the same
private card header in BL33 and the miniport. Direct retained-memory sleep/wake
is still under development. DDR200 is a separate extension; see the
[long-term goal](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/PORTING.md#windows-ddr200-goal).

### T214 DDR200 candidate (0.5.0.1)

Build with `NV_T210_LITE_WINARM_WRITE=1`, `NV_T214_SDR104=1` and
`NV_T214_DDR200=1`. The normal build keeps the extension disabled. Use the
same private visible/hidden partition header as BL33.

The candidate follows Switchroot's SD 6.x SCR and CMD6 vendor-function-14
detection. At a quiesced SDPORT tuning callback it first verifies SDR104,
selects SDR12 access mode, switches CMD6 group 2 to 14, and sets the host to
DDR50 with a 399.36 MHz module clock / 199.68 MHz card clock. It scans 128
manual taps, requires a contiguous window of at least eight, selects its
midpoint and compares four readbacks of the WINARM boot sector against the
SDR104 copy before admitting normal I/O. It does not implement HS400 DLL
synchronization or assume support from a card's brand.

Unsupported cards remain in SDR104. A failed transition, narrow window,
timeout or readback mismatch triggers one power-cycle/reinitialization to
SDR104, including tuning and volume-identity checks. The private path issues
no storage writes. It must preserve the card's CID and SDPORT-owned RCA;
otherwise it returns failure and blocks ordinary data I/O until a fresh
SDPORT enumeration, rather than pretending that recovery succeeded. Physical
fallback behavior, including any RCA change, still requires a Lite test.

Existing bounded registry diagnostics gain `Ddr200CardInfo`, `Ddr200Capable`,
`Ddr200Active`, `Ddr200Status`, `Ddr200RecoveryStatus`, `Ddr200Fallbacks`,
`Ddr200Window` and `Ddr200Tap`. Card-info bits 1/2/4 mean completed SCR/CMD6/CID
observations; the first two can be absent after reinitialization until SDPORT
reads them again. SDPORT's logical `BusSpeed=7` remains SDR104; use
`Ddr200Active` with the clock/tuning evidence to identify the private mode.

Host sanitizer tests cover capability parsing, seven/eight-tap boundaries,
clock limits, actual mode-transition code, timeouts, corrupted readbacks,
CID/RCA mismatch and both partition views. ARM64 builds pass. The signed
FAT32-visible 0.5.0.1 package was installed with Bluetooth 1.0.0.18 on
2026-11-07; its SYS/INF/CAT matched on readback. The existing visible-profile
BL33 and all physical partition boundaries were retained. These are
**code/build results, not physical DDR200 acceptance or a throughput claim**.
The owner photographed **INACCESSIBLE_BOOT_DEVICE** before the desktop on
the next boot. The returned card had crash dumps/error logging disabled and
no current driver record; the failing operation and cause remain unknown. The readback-verified
installation is therefore **not a successful boot test**. Platform sleep/wake
is isolated on [`sc7-test`](https://github.com/ruje0504-2/Windows-for-Switch-Drivers/tree/sc7-test).

Resume candidate **0.5.0.2** also saves the active CAR source and base clock.
It restores SDMMC1's own enable/reset before accessing a lost controller,
and restores the saved SDR104 or DDR200 source/divisor before the first card
clock pulse. The previous sequence briefly enabled the saved DDR divisor on
the SDR104 source before switching back. Only SDMMC1's set/clear clock bits
are changed; old commands, DMA pointers and interrupt status are not replayed.
PLL, clock-enable, reset, source-readback or internal-clock failure blocks
data I/O. ARM64 builds for both partition views, actual-callback fault tests
and signed visible-package checks pass. These changes are included in the installed 0.5.0.3 candidate below;
physical acceptance remains pending. It covers retained card power,
not platform-wide power loss or a card power-cycle/re-identification. It does
not depend on the experimental PEP and does not establish a cold-boot fix.

Recovery candidate **0.5.0.3** fixes a reproduced event-mask loss: the private
DDR200 fallback performs a controller reset-all, which clears SDPORT's status
and signal enables. Recovery previously returned without restoring those
masks, so later interrupt-driven requests could stop completing. The callback
now masks private polling traffic, clears its pending events and restores the
caller's original masks on success and failure. The new regression failed
before the fix and passes afterwards for both partition views, including
zero masks and failed card recovery. Both ARM64 builds pass. This is a
code-level defect and a candidate fix, not proof of the photographed blue
screen's root cause or a successful hardware boot. It retains DDR200 and the
SDR104 fallback; it does not enable SC7.

The signed FAT32-visible 0.5.0.3 package was installed with Bluetooth 1.0.0.19
on 2026-11-07. SYS/INF/CAT readbacks matched. The original boot/firmware files,
other drivers, service start settings and Bluetooth pairing state were retained.
After this update, the owner confirmed **successful desktop boot and working
Bluetooth**. This accepts the observed boot and Bluetooth recovery on the
tested Lite. Actual DDR200 selection, electrical tuning/fallback and throughput
remain unverified until the runtime parameters are read; a successful boot
alone does not distinguish DDR200 from SDR104.

```sh
python3 drivers/Tegra210Sdhc/tests/test-ddr200.py
python3 drivers/Tegra210Sdhc/tests/test-ddr200-transport.py
python3 drivers/Tegra210Sdhc/tests/test-t214-context.py
python3 drivers/Tegra210Sdhc/tests/test-diagnostics.py
```

References: [Microsoft SD miniport](https://github.com/microsoft/Windows-driver-samples/tree/main/sd/miniport/sdhc)
and [Switchroot SDHCI transfer limits](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/linux-5.1.2/drivers/mmc/host/sdhci.c).

### Crash-dump source candidate (0.5.1.0)

The read-only device inventory at **2026-11-12 23:56:31–23:57:09 UTC** found
`nvtsdhc` **0.4.0.1**, Code 0, with a 44,432-byte SYS and SHA256
`5ce2520d09ce67cc5b008da099d04cb821e9d314f1264c7ac8fb7ccb1447bd1b`.
This supersedes the earlier package selection as the current installed version.
Recent boots also recorded volmgr 46 (dump initialization failure), despite
an existing 512 MiB pagefile and small-dump configuration. That observation
does not establish the cause of the GPU failures or prove which dump component
failed. [GPU investigation and collector](https://github.com/ruje0504-2/mesa-switch-woa/blob/main/tegra-x1-wddm/docs/NATIVE_DESKTOP_158.md)

The bounded SDR104 profile, including its **DDR200 extension**, now advertises
SDPORT dump support. Build with `NV_T210_LITE_WINARM_WRITE=1` and
`NV_T214_SDR104=1`; retain `NV_T214_DDR200=1` for a DDR200-capable package.
Use the same private partition-view header as the installed BL33. The legacy
Lite profile without T214 board support continues to reject dump writes.
The installed package version alone does not identify its active bus mode.

- Dump initialization maps the board registers at PASSIVE_LEVEL, without
  changing the live controller, acquiring the I2C gate or allocating DMA memory.
  It recognizes both PLLC4 OUT2 (SDR104) and OUT0 (DDR200) clock sources.
- At the dump stack's reset-all, takeover checks the current CAR state,
  gates SDCLK before changing the source, restores SDMMC1's own clock/reset
  when needed, and selects the common 199.68 MHz parent. Controller reset and
  a card power cycle remove inherited DMA and proprietary DDR200 state before
  SDPORT re-enumerates the card. It does not rely on a cached DDR flag: the
  normal driver may have changed modes after dump preparation. No partition
  table, filesystem format or card contents are reinitialized.
- The dump instance re-enumerates at 3.3 V/default card speed, uses PIO with a
  maximum 16 KiB request, and does not advertise voltage switching or tuning.
  Normal SDR104/DDR200 and ADMA2 operation are retained.
- When the dump stack takes over, LDO2 access uses bounded polling without
  waiting on a gate that a stopped CPU could hold. Normal I2C serialization
  remains enabled. The card is power-cycled instead of trusting cached voltage.
- Before its first write, the dump instance can read and validate the physical
  MBR and NTFS boot sector itself. The same card identity and WINARM extent
  checks apply. FAT32, partition-table, erase and out-of-range writes remain
  denied in dump mode, including in the FAT32-visible build.
- Polled event enables are accumulated from the status-enable register.
  The previous signal-enable read lost events because dump mode suppresses
  IRQ signals. Disabling one event now preserves other polled events without
  enabling IRQ delivery. Identity I/O restores the caller's masks and timeout.

The new board helper retains GPL-2.0-only notices and identifies the reused
identity-polling code from this repository's `f6ee89ede53e` experiment. The
Microsoft callback framework remains attributed separately. The callback and
initialization contracts were checked against WDK 10.0.26100.0 `sdport.h` and
the [Microsoft SDHC sample](https://github.com/microsoft/Windows-driver-samples/blob/main/sd/miniport/sdhc/sdhc.c).
SDPORT shares its dump/hibernate capability flag; this change does not enable
Windows hibernation, alter sleep settings or establish S4/resume support.

**Validation is local only.** ASan/UBSan tests exercise the actual helpers and
PIO write callback with synthetic identities in both partition views: mapping
failures, a late switch from SDR104 to DDR200, CAR clock/reset/PLL failures,
the actual reset-all/power sequence, a held I2C gate, event masks, data comparison across partial writes,
completion ordering, wrong card/volume, timeout/CRC/reset failures and write
bounds. Existing DMA, retained-context, diagnostics and DDR200 tests pass.
Eight ARM64 configuration builds pass with warnings treated as errors (SDR104,
DDR200 and bounded PIO in both views, plus read-only and generic). Those build
fixtures contain synthetic card identities and **must not be installed**.
No driver installation, forced bugcheck or actual dump capture was performed.

```sh
python3 drivers/Tegra210Sdhc/tests/test-crashdump.py
```

Hardware acceptance still requires a package built for the actual card, a
normal boot/read/write check, successful dump-stack initialization and a
retrieved dump that a debugger can open. Record that package's version and
hash separately; compilation and Code 0 do not establish successful capture.

### DDR200 fallback evidence (0.5.1.1)

The read-only registry snapshot at **2026-11-13 00:23:58 UTC** reports
`Ddr200Active=0`, `Ddr200Status=0xc0000185`, `Ddr200RecoveryStatus=0`,
`Ddr200Fallbacks=1`, `Ddr200Window=1`, `Ddr200CardInfo=4`, and
`Ddr200Capable=0`. Standard bus mode is SDR104 (`BusSpeed=7`), four-bit,
1.8 V, with `ClockKhzActual=199680` and no recorded ADMA errors. These are
driver diagnostic snapshots, not a throughput measurement or fresh MMIO probe.

The active driver contains DDR200 support despite its bound INF version
0.4.0.1. Pre-install backup at 00:32:58 UTC confirms the running service uses
`System32\drivers\nvtsdhc.sys` (44,432 bytes, SHA256 `5ce2520d…`), while
exporting the bound `oem23.inf` produces a different 31,632-byte SYS
(`25a456e2…`). Thus the bound package label does not version the separately
updated system file. Both the live image and the original DriverStore package
are retained as distinct backups before replacing the package.

The source permits the vendor transition only after a positive
capability check. Successful fallback deliberately discards the SCR/CMD6
cache while retaining CID/RCA, so the later `Ddr200Capable=0` does **not**
prove that the card never advertised DDR200. A one-tap window fails the
eight-tap requirement, but the old record does not reveal how many taps
were tested before the tuning engine stopped.

Candidate **0.5.1.1** adds four persistent diagnostic values without changing
clock limits, tuning decisions or the fallback sequence:

| Value | Meaning |
| --- | --- |
| `Ddr200CapabilityAtAttempt` | 0: not observed; 1: unsupported SCR/CMD6 capability; 2: capability advertised. Retained across fallback. |
| `Ddr200TuningSamples` | Number of CMD19 samples actually issued in the last DDR tuning attempt. |
| `Ddr200TuningTimeouts` | Number of missing buffer-ready responses in that attempt. |
| `Ddr200TuningControl2` | Last sampled host-control-2 value, before cleanup clears the tuning bits. |

The actual-function host model distinguishes a 128-sample narrow window from
an early one-sample end, unsupported capability from unavailable metadata,
and preserves the original advertised capability after recovery. Both
partition views and the expanded queued diagnostic snapshot pass. The actual
card's live GPT/NTFS/FAT metadata and visible MBR projection also match its
saved private identity and the package's generated header; no partition
metadata was written during that check.

[Switchroot's DDR200 implementation](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/2d0059fd3167a8df756de2aa0489d4aa70a9fc15/drivers/mmc/host/sdhci-tegra.c#L1585)
uses the same minimum window and describes cards that require DLL-based
synchronization instead of the DDR50-host/manual-window method. The existing
one-tap record does not distinguish that hardware limitation from an early
tuning stop or another signaling/configuration issue. Do not relax the write
qualification threshold or label the card unsupported from that record alone.

### 0.5.1.1 installation and runtime result

The per-card FAT32-visible package from source **`cca463704d5a78577d86cc1adde1ce83dd547ae2`**
was installed online with PnPUtil at 2026-11-13 **00:49:31–00:49:34 UTC**.
Microsoft Inf2Cat 10.0.26100.0 (`/os:10_CO_ARM64`) reported zero errors and
warnings. Native SignTool verified the signatures and both INF/SYS catalog
members before installation. PnPUtil returned **3010**, requiring a restart.
The single requested restart produced boot **00:52:13 UTC**. SSH verification
at **00:54:02–00:54:13 UTC** confirms **0.5.1.1 / `oem89.inf` / Code 0** and
the expected active SYS. This is a Windows boot/storage check, not a new
physical GPU desktop acceptance.

| Package file | SHA256 |
| --- | --- |
| `nvtsdhc.sys` (46,520 bytes) | `5286cb72a99a7bdc3e6e9fe4ac8f4e7a50c0d068917cdd217506daf320aeca7b` |
| `nvtsdhc.inf` | `a48863e62bfec269b525708a7f978288b964d791362f9e1e0551481d0bf72d67` |
| `nvtsdhc.cat` | `3800c39e424977a4f80613b976484b2c4dacc7f9c726fdd0c8198a85e6ba0fe0` |

An **8 MiB** deterministic file was written with write-through, flushed to
disk, reopened and hashed. Expected/read SHA256 both equal
`bdfdf42093da25984ceec68638bf9a19d72e66737ec6f87f8b6f240e3563d617`;
the temporary file was then removed. This verifies ordinary filesystem I/O,
not power-loss integrity or sustained throughput. Both partition extents
and the full `powercfg /query` output are unchanged. Fan version 12 and CPU
**1996800 kHz** remain selected; Wi-Fi remains **0.5.7 / `oem88.inf`**.

The new diagnostic values report:

| Field | Value |
| --- | --- |
| Capability at attempt | **2: advertised** |
| Actual tuning samples / timeouts | **128 / 1** |
| Longest passing window | **1 tap** (minimum accepted: 8) |
| Last host-control-2 | `0x000c` |
| DDR attempt status / recovery status | `0xc0000185` / `0` |
| Active DDR200 / fallback count | **0 / 1** |
| Current standard timing / clock | **SDR104 / 199.68 MHz** |

Thus this boot passed capability detection and completed the full scan,
then rejected the narrow window and recovered to SDR104. It was not an
unsupported-capability rejection or an early one-sample stop. The reason
for the narrow physical window remains unproven; do not equate the cleared
post-recovery capability cache with lack of card support.

At **00:55:00 UTC**, an explicit successful query found no volmgr events
since this boot; the real 512 MiB pagefile and small-dump configuration remain.
Earlier boots repeatedly logged volmgr 46. The absence of a new initialization
error is progress, but **no forced crash or actual dump capture was performed**.
The retained postboot JSON is 14,769 bytes, SHA256
`80a13bbce2f0d8e70915ac0463dfd7b449a144915c7198ae5b7f6603d1a2f407`.
