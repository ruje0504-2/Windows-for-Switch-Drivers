# Switch Lite HDH-001

The Lite build uses the existing Tegra210Gpio and Tegra210I2c drivers and the FTM4 implementation in `drivers/SwitchLiteTouch`. The latter matches ACPI SWOA0502, initializes the controller in Windows, handles quick press/release events and retries HID submission. The public desktop checker is built from `touch-check.c`. Real-hardware testing has confirmed a touch press on the Windows setup dialog. The three-circle TOUCH PASS test is pending.

The build script also produces the existing SwitchPowerButton driver for the optional Lite I2C5/PMIC firmware profile. It reads MAX77620 ONOFFSTAT and ONOFFIRQ and submits HID System Power Down input. Use the firmware build with SWITCH_LITE_POWER=1 and stage switchpwrbtn.inf together with the I2C driver. The package compiles for ARM64; its physical button behavior is pending.

The companion Windows firmware provides the GPIO interrupt resources, I²C3, touch power initialization and NTFS boot path. Its Hekate entry is **Windows 11 Lite**. [Firmware source](https://github.com/ruje0504-2/winx/tree/hdh001-hekate-sd)

## Compile the Lite driver set

Set WDK_INCLUDE to the WDK Include/version directory, WDK_LIB to Lib/version/km/ARM64, WDF_INCLUDE to Include/wdf/kmdf/1.15, and WDF_LIB to Lib/wdf/kmdf/ARM64/1.15. CLANG_CL and LLD_LINK select the tools. WDK_EXTRA_LIB can select the matching SPB, GPIO, VHF, and battery-class (`battc.lib`) libraries. The script builds GPIO, I²C, Lite touch, power button, fan, battery, RTC, and Bluetooth.

```sh
sh docs/hdh001/build-drivers.sh . /path/to/output
python3 docs/hdh001/tests/test-ftm4.py
```

The existing `tools/Build-Driver.ps1` also accepts the Lite source via its Sources argument and VHF libraries via Libs. Sign the final SYS and catalog files with the same development certificate used by the target Windows image. Install the GPIO, I²C and touch packages into the offline ARM64 image with DISM, then place the checker at `Users/Public/Desktop/LiteTouchCheck.exe`. Add the power-button package when using the optional power-enabled firmware.

## SD system partition

The bounded-write profile is in `drivers/Tegra210Sdhc/winarm`. Use the existing target generator with a saved image to produce the private header, and build the optional WINARM profile as described in the SD README. The matching INF is version 0.3.2.5.

The returned in-memory MBR places the configured WINARM partition in slot 1 and sets its active flag. This lets the system-partition check used by Windows Setup accept the already-booted MBR volume. The physical card's identifiers and full write bounds remain part of the selected profile's checks.

For the interrupted Home installation tested here, Windows subsequently required its failed specialize state to be restored before retry. The firmware repository records those setup logs and the recovery steps.

## Shared fan and T214 clock control

`drivers/SwitchFan` now supports both the original SWOA0401 device and Lite SWOA0402. Its common fan/I2C/backlight code stays in the upstream source file; only the Mariko clock helper and handoff definition are under `src/t214`. The original device retains its backlight interface. Older Lite firmware exposes PWM1 only; the new optional backlight profile adds a separate PWM0 resource and enables the existing brightness interface.

On Lite, TMP451 uses the Mariko -12.5 C remote offset. The optional firmware handoff requests CPU 1996.8 MHz with MAX77812 M4 at 1120 mV, independently of whether the firmware completed the requested EMC change. The fan timer owns both cooling and CPU transitions. At 80 C or after repeated sensor-read failures it restores the saved boot CPU frequency; at 70 C it can resume the request. It does not touch MAX77812 M1/GPU registers.

Use the matching firmware's `cpu_oc`/`ram_oc` INI options and its LitePerformanceCheck application. Parameters records include CpuActualKhz, RamActualKhz, TempSoC, TempBoard, Duty, SensorFailures and status. Build with `docs/hdh001/build-drivers.sh`; the shared INF is version 1.0.0.6. The existing Erista CPU-clock driver is not loaded for the Lite profile.

Version 1.0.0.6 handles Lite screen-off cooling using the existing display-power notification. It restores the saved boot CPU clock before removing the overclock fan floor. The normal temperature curve stops the fan at 39 C or below and restarts it at 42 C; hot screen-off workloads still get cooling. Failed clock restoration retains the fan floor, and a failed temperature read restarts an already stopped fan immediately. On display wake the fan is set before CPU overclock can resume. A cool system sleep/hibernate D0 exit can also stop the fan; final removal, shutdown and device-only idle retain their existing cooling behavior.

The ARM64 build, CPU/backlight checks, and `python3 drivers/SwitchFan/tests/test-screenoff.py` pass. The latter exercises the actual timer and D0 callbacks, including clock/fan ordering, invalid temperatures, wake and shutdown isolation. Native catalog generation, signing and offline installation readback passed. On 2026-11-07, the owner confirmed one successful screen-off fan stop and wake recovery with 1.0.0.6, then reported lock-screen/black-screen stalls after repeated cycles. Repeated wake is not accepted. `ScreenOffCooling`, `SleepFanOff`, `CpuOverclockActive`, `Duty` and `DriverVersion` update fixed values under the driver's existing Parameters key; no periodic log files are created.

Screen-off cooling does not establish SoC low-power sleep. The observed Modern Standby records had no DRIPS residency, and S4 hibernation is disabled in the tested installation. Physical CPU readback on 1.0.0.5 is 1996.8 MHz and RAM is 1866 MHz, below the requested 2100 MHz. Normal shutdown and native restart have separate owner-confirmed results in the [power status](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/power-performance.md).

## Sleep/wake repair in progress

Input candidates **switchsio 0.2.4.0** and **switchtouch 1.0.1.1** use the
shared `drivers/common/display_idle.h` policy. Display-off releases their KMDF
runtime-power references; display-on requests D0 without waiting inside a
power notification. Their existing suspend/resume callbacks stop polling,
release input and reinitialize hardware. The power button remains the wake
source. These changes are compiled and host-tested, not hardware accepted.

GPIO **1.0.0.5** implements GpioClx's `SaveContext`/`RestoreContext` contract
for all ports, following Switchroot's CNF, output/direction, debounce and
interrupt register set. It restores output latches before enabling outputs,
then enables interrupts after all configuration is restored. Pending wake
events are preserved; power callbacks perform no registry I/O while storage
may still be resuming. ARM64 build, signed catalog checks and actual-callback
tests for repeated register loss and wake preservation pass. The candidate
is not installed and does not by itself implement SC7.
[Switchroot GPIO](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/2d0059fd3167a8df756de2aa0489d4aa70a9fc15/drivers/gpio/gpio-tegra.c#L490)

```sh
python3 drivers/Tegra210Gpio/tests/test-context.py
```

I2C **1.0.0.6** captures the inherited clock source, divisors and stable bus
timing before leaving D0. Reentry restores only that controller's CAR
clock/reset bits, then initializes packet mode and checks configuration/FIFO
completion. It never replays command addresses, FIFO data or a pending SEND
bit. I2C5 restoration uses the existing SD/PMIC gate. Error diagnostics run
after releasing that gate, and the old device-register self-tests were removed
from D0 entry. ARM64 build, signed catalog checks and actual callbacks covering
all six controller routes, repeated context loss and bounded failures pass.
The candidate is not installed. It restores the inherited clock selector;
complete SC7 still requires platform restoration of its parent clocks.
[Switchroot I2C resume](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/2d0059fd3167a8df756de2aa0489d4aa70a9fc15/drivers/i2c/busses/i2c-tegra.c#L2698)

```sh
python3 drivers/Tegra210I2c/tests/test-context.py
```

Power-button **1.0.0.3** distinguishes HID System Power Down from System Wake
Up using the console display state. The passive-IRQ path unmasks only the
press edge and sends a complete report/release pair, so releasing the sleep
button does not create another PMIC wake interrupt. IRQ diagnostics are
deferred outside the passive interrupt lock; D0/suspend callbacks do not write
the registry. The older polling profile also selects the wake report when
the display is off. ARM64 compilation, signing and actual-callback tests for
rapid cycles, short presses and PMIC mask preservation pass. This candidate
is not installed; repeated physical wake/lock-screen behavior is unverified.
[Windows system-control usages](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-keyboard-input)

The current candidate pairs fan **1.0.0.7**, power button **1.0.0.3**, RTC
**1.0.0.3**, Bluetooth **1.0.0.19**, and the DDR200 **0.5.0.2** candidate. The fan's display
power callback no longer writes registry hives while holding its backlight
lock. The power button drains its timer before I2C power-down and optionally
uses a passive wake interrupt instead of polling. The RTC unregisters and
drains its time-change work before its I2C dependency powers down.

The optional `SWITCH_LITE_SLEEP=1` firmware exposes PMU_EXT GSI 118 as a wake
interrupt. Power-button setup
applies Switchroot's PMC interrupt inversion, preserves unrelated PMIC masks,
and restores its original interrupt mask bits at final shutdown. The
[T214 processor backend](../../drivers/Tegra214Power/README.md) uses PoFx for
CPU context save/restore and PSCI C7/CC6, but its 0.1.0.0 boot-start package
blocked cold boot with `0xc000000f`. Exclude it from normal deployment; it requires
explicit `BUILD_T214_PEP=1` and `SWITCH_LITE_PROCESSOR_IDLE=1` development
opt-ins. Direct SC7 sleep/wake is not implemented/accepted yet; the S4
experiment has been removed at the owner's request. Successful compilation and mocked transitions do not prove physical
sleep, wake, display recovery or lower battery consumption.

Bluetooth 1.0.0.17 addresses a returned-card failure with SafeMode=0 and
BootPending=0: patch record 150 timed out, but 1.0.0.15 continued and exposed
a radio that answered no subsequent commands. The replacement validates the
firmware stream and every command status, uses flow control during download,
permits one complete retry only after an acknowledged reset, and verifies
baud-rate fallback. A verified patch is reused across D0 transitions. A failed
probe reports a device error instead of publishing a working radio.

Focused regressions use the actual driver callbacks:

```sh
python3 drivers/SwitchBluetooth/tests/test-probe.py
python3 drivers/SwitchBluetooth/tests/test-cancel.py
python3 drivers/SwitchPowerButton/tests/test-suspend.py
python3 drivers/SwitchPowerButton/tests/test-wake.py
python3 drivers/Tegra214Power/tests/test-processor.py
python3 drivers/common/tests/test-display-idle.py
python3 drivers/SwitchFan/tests/test-screenoff.py
```

## Shared battery, RTC, and volume-button support

The companion firmware's `SWITCH_LITE_SYSTEM=1` option exposes the upstream BAT0, RTC0, and BTNS definitions on Lite. It requires the Lite fan/I2C1 and power/I2C5 profiles. Use `drivers/SwitchBattery` and `drivers/SwitchRtc` directly; no Lite-specific source copies are needed.

- `switchbattery` 1.0.0.6 reads MAX17050 (I2C1, 0x36) and BQ24193 status (I2C1, 0x6B). Lite exposes exactly those two connections. The optional third BM92T connection is absent, so the USB-C role-switching timer and charger writes are not activated. The original Erista three-connection path remains available.
- `switchrtc` 1.0.0.2 reads MAX77620 RTC (I2C5, 0x68). It learns a private Windows time offset after manual/NTP time changes and applies it on later starts, preserving the hardware calendar used by Horizon.
- Volume up/down use the Windows inbox ACPI0011 button driver through the existing GPIO driver: PX6/GPIO190 and PX7/GPIO191, with both-edge interrupts. This adds input support, not an audio endpoint.

The ARM64 build, signed SYS/catalog verification, and PnPUtil staging in ARM64 WinPE 22621.1 pass. Sign the final INF/SYS catalogs and use [Install-LiteSystem.cmd](Install-LiteSystem.cmd) beside the `switchfan`, `switchpwrbtn`, `switchbattery`, `switchrtc`, and `switchaudio` package directories. Include LitePerformanceCheck.exe for clock readings and brightness controls. Run it with `online` from an elevated Home prompt, or `offline W:\` from ARM64 maintenance WinPE with WINARM's actual drive letter. Existing GPIO/I²C drivers and the Stage 6 test certificate are prerequisites. Battery readings, learned RTC time across boots, and physical volume-key input remain unverified on HDH-001.

## EMC failure and fan-floor correction

The first physical performance run reported CPU 998.4 MHz, failed BPMP initialization, and a running temperature timer at 34 C / 33 C with duty zero. SwitchFan 1.0.0.4 separates CPU preparation from EMC status and keeps the minimum fan duty for a valid configured CPU request. PMIC voltage/readback, CPU clock checks, and thermal fallback remain in place. The companion firmware corrects the ATOMICS trigger/result registers and maps the Mariko BPMP mailbox carveout. Target clock rates and cooling after this correction still await hardware confirmation.

## Audio integration and display rollback

`drivers/SwitchAudio` remains the upstream PortCls/WaveRT implementation. Its cross-build uses `portcls.lib` and the upstream native-driver linker settings. The companion firmware prepares APE, I2C1, RT5639, I2S1 and PV6 headphone detection using shared R403 code. The target is stereo speaker/headphone playback and automatic jack switching; no microphone endpoint is provided. The owner confirmed audible speaker output after the later pad/1.0.0.9 update described below. Headphones and jack switching still need acceptance.

The display-only sources stay in the existing WinX tree. After the physical Lite went black during display-device restart, CPU-rendered rotation was withdrawn from the active profile. The Home installer no longer installs WinX display or LiteLandscape, and the companion firmware uses BasicDisplay in the native orientation. The proposed PE trial was cancelled. BasicDisplay does not provide GPU rendering acceleration.

## Bluetooth preparation

The shared `drivers/SwitchBluetooth` implementation uses UART-D H4, the
inbox BthMini child, patchram and 3 Mbaud flow control. The companion firmware's
`SWITCH_LITE_BLUETOOTH=1` option supplies UART-D/PH3/PH4/PH5, the PMIC 32 kHz
output and BTH0 resources. Do not add a separate Lite Bluetooth source copy.

The installed **1.0.0.18** retains strict patch/status/baud verification
and HCI cancellation/drain. A stale `BootPending` value is diagnostic only;
it no longer disables all later boots, and orderly D0 exit clears it. On a
failed T214 probe, one PH4 reset and full retry are permitted before UART/HCI
traffic starts. PH3 is held awake; Wi-Fi power and the shared 32 kHz clock are
untouched. These follow the Switchroot B01 Bluetooth wiring and the existing
Lite DXE power sequence. ARM64 compilation, host fault tests and signed
catalog checks pass. It was installed with DDR200 0.5.0.1 on 2026-11-07;
SYS/INF/CAT readback matched. Older matching Bluetooth packages were removed
to prevent reselection, while the existing radio identity and paired-device
registry state were preserved. Repeat physical discovery and shutdown remain
unverified for this replacement.

The latest signed candidate is **1.0.0.19**. It also restores UART-D's own
clock enable/reset before probing after D0 context loss, and rejects an
unavailable PLLP parent without touching UART registers. A healthy cold-start
host is left enabled; shared PLLP, Wi-Fi power and paired-device state are not
modified. Actual-callback tests cover repeated host context loss, inaccessible
clocks, stuck reset/enable bits and orderly D0 cleanup. ARM64 compilation and
SYS/catalog verification pass. This candidate is not installed or hardware
accepted; 1.0.0.18 remains on the card with DDR200 0.5.0.1.
[Switchroot UART initialization](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/2d0059fd3167a8df756de2aa0489d4aa70a9fc15/drivers/tty/serial/serial-tegra.c#L1108)

The installer includes Bluetooth when a signed `switchbt/switchbt.inf` is
present. Supply the private Switch-specific patch at `Firmware/BCM4356A3.hcd`;
it is copied before driver staging and is not distributed in this repository.
Use `kits/Switch-Bluetooth/Bluetooth.ps1 -Mode Status` to read bounded existing
diagnostics. The child is added after a delay. Check the actually loaded
`DriverVersion`, since an older staged package can supersede a file-only rollback.

### Built-in gamepad and motion

On HDH-001 the internal SIO controller owns the IMU. The optional
`switchsio.inf` package binds to `ACPI\SWOA0902` on UART-C. It uses VHF
for 14 buttons, a hat and four stick axes, and serves the latest accelerometer
sample through the existing SwitchSensor status IOCTL. The Home button uses
its separate GPIO. Build the firmware with `-D SWITCH_LITE_SIO=1` alongside
the existing Lite touch/GPIO profile. Install the signed driver package with
`pnputil /add-driver switchsio\switchsio.inf /install`.

The protocol fixture covers the handshake, split replies, damaged headers,
stick limits, button mapping and partial motion samples. The ARM64 driver and
optional firmware build successfully. Physical input and motion are pending.
[Switchroot Lite device tree](https://github.com/CTCaer/switch-l4t-platform-t210-nx/blob/linux-5.1.2/kernel-dts/tegra210b01-vali.dts)


## Audio 1.0.0.8 follow-up

The physical Lite reached Home with BasicDisplay. ADMA/I2S counters advanced during playback, but the owner heard no internal-speaker output and had not tested headphones. The companion firmware now applies Switchroot's NX bias/class-D initialization for Lite. The shared 1.0.0.8 driver snapshots codec power, mixers, clocks and private tuning on jack-route changes; rendering behavior and endpoint format are unchanged. The existing one-shot TestTone option is disabled on the tested Home installation; the normal Windows startup sound is the first audible check. Actual sound remains pending.

Offline Add-Driver initially staged 1.0.0.8 without replacing the existing device's 1.0.0.7 binding. Native maintenance removed the identified superseded non-boot-critical package, retained the signed 1.0.0.8 package, and marked only the audio devnode with CONFIGFLAG_REINSTALL (0x20) for the next enumeration. See the [Windows PnP reinstall flag](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/determining-whether-a-device-is-plugged-in). Driver staging does not establish successful runtime binding or sound output.

The same boot confirmed the Bluetooth patch loaded, the 3 Mbaud path and BthMini child started without transport errors. Pairing/range still need physical confirmation. CPU readback remained 1996.8 MHz, and the owner confirmed the fan turns. EMC readback was still 204 MHz before the companion RAM R2 correction.

## R2 hardware follow-up: startup sound

The next physical boot bound audio 1.0.0.8, cleared the reinstall flag and read all recorded codec registers successfully. Jack=0 selected the speakers, SPK_VOL=0x1414 was unmuted, and ADMA completed 439 periods. These observations establish driver activation and data-path activity, not audible playback.

The owner clarified that only the Windows startup sound had been checked. The offline SOFTWARE hive had `Microsoft\Windows\CurrentVersion\Authentication\LogonUI\BootAnimation\DisableStartupSound=1`, with no overriding sound policy. The normal startup sound is enabled by setting this value to 0. The optional driver TestTone remains 0. No additional driver or firmware change is made for this configuration correction. Desktop playback and headphone output remain untested.

The same boot read CPU 1996.8 MHz and RAM 1866 MHz. BPMP rounded below the 2100 MHz request, so the firmware correctly reported a target mismatch rather than claiming 2100 MHz success.

## Backlight, audio pads and paging follow-up

`SWITCH_LITE_BACKLIGHT=1` adds PWM0 (0x7000A000, four bytes) to FAN0 separately from PWM1. Shared SwitchFan 1.0.0.5 retains the fan/CPU behavior and enables its existing brightness IOCTLs and display-power callback when PWM0 is present. It preserves the firmware divider, uses the inherited Lite brightness on first start, and restores the saved user setting thereafter. LitePerformanceCheck adds touch-sized Darker/Brighter buttons. Old Lite firmware remains supported without backlight access. This does not provide GPU acceleration or software rotation.

The owner confirmed silence during Windows Settings playback after enabling startup sound. SwitchAudio 1.0.0.8 was active, the speaker route was unmuted, codec reads succeeded, and ADMA completed transfers. The new Lite firmware explicitly selects 1.8 V for AUDIO/AUDIO_HV, exits pad DPD when required, and uses the Vali DAP1 output pinmux. These settings follow Switchroot's board DT and B01 PMC implementation; they are a candidate correction, not yet proven to restore sound. SwitchAudio 1.0.0.9 adds readback of pads, clock source, digital mixer and existing PCM-buffer peak/nonzero counts at the existing start/stop snapshots. It produces no extra sound and does no additional per-position/render work. TestTone remains disabled.

Windows kept TempPageFile=1 and created no persistent pagefile despite the requested 512–2048 MB setting. Inspection of the target SDPORT binary with Microsoft public symbols found that SdhostQueryRemovability reads `_ADR`/`_RMV` on a slot child, and SdhostPdoDeviceUsageNotification rejects paging for a removable slot. Home had no slot child. The companion `SWITCH_LITE_BOOT_SD=1` profile now describes slot 0 / memory function 0 as a fixed boot device for the Windows session. The card is removable only after shutdown. This changes ACPI metadata, not the SD miniport, card partitions or 80 GiB write limits. Returned-card readback confirms a 536870912-byte pagefile.sys, the 512–2048 MB request and no TempPageFile value; crash-dump/hibernate support is not claimed.

The paired update has been deployed to the existing WINARM installation and the card was safely ejected. Native ARM64 DISM staged SwitchFan 1.0.0.5 and SwitchAudio 1.0.0.9; both existing devnodes are marked for next-boot PnP reinstallation. The performance/brightness application is on the public desktop. The 512–2048 MB pagefile request, disabled extra test tone, completed setup state and BCD were preserved. On the next physical run, the owner confirmed audible output and working brightness buttons. Returned-card readback confirms active audio 1.0.0.9 and fan 1.0.0.5, ConfigFlags=0 for both, and a 512 MiB pagefile. Headphones, jack switching and display wake remain unconfirmed. The owner also confirmed that restart and shutdown still hang; shutdown makes the screen black without actually powering off.

References: [Switchroot display/backlight wiring](https://github.com/CTCaer/switch-l4t-platform-t210-nx/blob/linux-5.1.2/kernel-dts/nx-platforms/tegra210-nx-common-disp.dtsi), [B01 pad control](https://github.com/CTCaer/switch-l4t-kernel-4.9/blob/linux-5.1.2/drivers/soc/tegra/pmc.c), and [Windows SD slot ACPI objects](https://learn.microsoft.com/en-us/windows-hardware/drivers/bringup/other-acpi-namespace-objects#sd-host-controllers-and-devices).

The shared long-term scope, including Switchroot-based Wi-Fi and Lite SIO, is tracked in the [porting roadmap](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/PORTING.md).

## Wi-Fi and power boundaries

**Wi-Fi works on Switch Lite (HDH-001).** SwitchWifi 0.5.5 connected automatically to a saved WPA2-Personal network on Windows 11 ARM64 23H2. Windows recorded successful authentication, an IPv4 DHCP lease and a default gateway. The owner confirmed Internet access. [Driver and hardware results](../../drivers/SwitchWifi/README.md)

Normal Windows shutdown powered off the tested HDH-001 after installing
SwitchBattery **1.0.0.8**, with Wi-Fi, Bluetooth, audio and the gamepad enabled.
The owner observed the R6 diagnostic firmware's two fan pulses followed by
complete power removal. No device-isolation utility was needed. The normal R6 firmware selects native PMC/Hekate restart and disables the
diagnostic fan pulses. The owner subsequently confirmed that Windows Restart
also completed normally.

The previous battery IOCTL handler passed the underlying IRP of a KMDF-owned
request to `BatteryClassIoctl`, which can complete or retain that IRP itself.
Version 1.0.0.8 uses a WDM preprocess callback before KMDF takes ownership, as in
[Microsoft's battery sample](https://github.com/microsoft/Windows-driver-samples/blob/main/simbatt/func/wdf.c).
A separate lock protects the battery class handle without blocking the I2C
lock used by its callbacks. Only unhandled IRPs continue into KMDF. The actual
handler's immediate, pending, failed and fallback paths passed an ASan/UBSan
host test; the ARM64 package and signatures were checked before deployment.
Run `python3 drivers/SwitchBattery/tests/test-irp-ownership.py` for that test.
The installed Bluetooth 1.0.0.15 cancellation fix is also retained; it did not
resolve the full shutdown failure on its own. [Firmware and physical evidence](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/power-performance.md)

## Windows Wi-Fi packet core

The ARM64 WiFiCx driver uses NetAdapterCx packet queues and the Broadcom msgbuf transport. Current hardware evidence and the meaning of each diagnostic field are in the [driver notes](../../drivers/SwitchWifi/README.md).


For a Wi-Fi-only kit, use `Install-LiteSystem.cmd online wifi` or
`Install-LiteSystem.cmd offline W:\ wifi`. Supply the signed `switchwifi`
directory, matching `WifiPrepare.efi` and the private A3 firmware/NVRAM
pair in `Firmware`. The installer keeps an existing complete board pair.
The runtime BL33 must be prepared separately.
