# Windows ARM64 drivers for Nintendo Switch Lite

Drivers for the **HDH-001 (Tegra T214)** port of Windows 11 ARM64. They use the ACPI devices exposed by the [WinX companion firmware](https://github.com/ruje0504-2/winx). The tested system runs Windows 11 23H2 (22631.2861).

## Drivers on hardware

Package versions below reflect the 2026-11-12/13 UTC device inventories where available.
Installation alone does not establish feature acceptance; older default-branch
source and earlier validation baselines are identified explicitly.

| Driver | Result on Switch Lite |
| --- | --- |
| `nvtsdhc` **0.5.1.1** | Installed as `oem89.inf`; planned restart, Code 0, exact SYS hash and an 8 MiB write/flush/readback check passed. DDR200 capability was advertised, but a 128-sample scan found only a one-tap window and fell back to SDR104. Crash-dump takeover covers both modes; actual dump capture remains unverified. [Storage evidence](drivers/Tegra210Sdhc/README.md#0511-installation-and-runtime-result) |
| `switchwifi` 0.5.7.0 | The owner confirms 0.5.7 usable on the Lite after the SD update on November 12. SSH independently confirms `oem88.inf`, Code 0 and the exact installed SYS hash. It checks firmware disconnect state before treating a missing event as device failure and records the leave phase. Repeated reconnects and sustained stability remain pending. [Evidence and references](drivers/SwitchWifi/README.md) |
| `switchbt` 1.0.0.19 | The owner confirmed Bluetooth working after the combined SD 0.5.0.3 update. The driver retains strict firmware checks, bounded reset recovery and UART-D host preparation. Repeated startup and sleep/wake remain separate tests. |
| GPIO 1.0.0.4, I²C 1.0.0.5, Lite touch 1.0.1.2 | These package versions are selected on the device. Touch taps were confirmed in Windows Setup; default-branch Lite touch source remains 1.0.1.0, so installation of 1.0.1.2 is not new functional acceptance. |
| `switchsio` 0.2.5.0 (analog sticks and gamepad) | The current device package is 0.2.5.0; default-branch source remains 0.2.3.0. Windows previously detected the HID gamepad/mouse and controller input. In-game use awaits testing. |
| `switchsio` 0.2.5.0 desktop controls | Current installed version verified. Left-stick pointer, ZR/ZL clicks, Capture desktop mode and **+** On-Screen Keyboard are the existing mapping; right-stick scrolling, R3 middle click and D-pad arrows were added in 0.2.3. This inventory does not newly validate those features on 0.2.5.0. [Button mapping](drivers/SwitchSensor/README.md) |
| `switchaudio` 1.0.0.9 | The built-in speakers play audio. Headphones and jack switching await testing. |
| `switchrtc` 1.0.0.3 | Installed package reverified after the SD update. Earlier RTC/time restoration passed; this version inventory is not a new long-term clock-retention test. |
| `switchfan` 1.0.0.12 | Installed version and driver hash verified over SSH. The active curve reaches 32% PWM at 35 C, 75% at 40 C and 100% at 45 C. CPU readback returned to the owner's requested 1996.8 MHz; thermal throttling and screen-off policy are retained. PWM commands are not measured RPM. [Cooling policy](drivers/SwitchFan/README.md) |
| `switchpwrbtn` 1.0.0.6 installed; 1.0.0.4 default source | The device inventory reports 1.0.0.6. The earlier owner-confirmed S0 screen-off/wake result and 12 power/12 wake reports apply to 1.0.0.4. This does not validate the uncommitted 1.0.0.6 source or implement SC7/S4. [Default source](drivers/SwitchPowerButton) |
| `switchbattery` 1.0.0.11 / shutdown | Includes the battery request-ownership correction introduced in 1.0.0.8, on which normal shutdown powered off the tested Lite with Wi-Fi, Bluetooth, audio and gamepad enabled. Native restart subsequently passed on the normal R6 firmware without fan pulses. [Power status](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/power-performance.md) |
| `switchbattery` 1.0.0.11 / charging | Enables BQ24193 hardware charge termination and an eight-hour safety timer independently of the optional USB-C PD resource. Installed register readback passed; a complete full-charge/recharge cycle remains unverified. [Charging policy](docs/hdh001/README.md#charge-termination) |

The experimental Tegra GPU driver and Mesa NVK ICD are maintained together. Native Vulkan triangle readback and Display-class Code 0 were verified on the Lite. DWM acceleration is in development. [GPU project](https://github.com/ruje0504-2/mesa-switch-woa)

The USB-C host package is on the Windows 23H2 card and awaits hardware testing. [USB-C host](drivers/TegraXusb/README.md)

Battery, RTC and power-lifecycle results are tracked in the [porting notes](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/PORTING.md).

SC7 development is paused; its experimental sleep/wake code is preserved on [`sc7-test`](https://github.com/ruje0504-2/Windows-for-Switch-Drivers/tree/sc7-test). The default branch excludes its PEP, wake-interrupt and input-idle experiments. S4 remains removed.

## Building and installing

- Start with the [HDH-001 build and install guide](docs/hdh001/README.md) for the ARM64 toolchain, test signing and device packages.
- Use the same card-specific header for both the Windows SD miniport and WinX BL33. The default exposes only `WINARM`. The `--expose-fat32` profile also mounts FAT32 and is currently selected by the owner. The latest comparison did not establish a clear performance difference; neither a confirmed regression cause nor a performance fix is claimed. [SD miniport build](drivers/Tegra210Sdhc/README.md#restricted-winarm-writes-on-hdh-001)
- For Wi-Fi installation, required board firmware and driver diagnostics, see the [SwitchWifi guide](drivers/SwitchWifi/README.md).

## Upstream

Shared driver implementations, SwitchCenter and SwitchPatcher come from [hayato41200/Windows-for-Switch-Drivers](https://github.com/hayato41200/Windows-for-Switch-Drivers). The upstream project documents the original T210/Erista packages and credits Claude for assistance with its repository and README. Some register sequences follow Hekate and Linux. See the [GPL-2.0 license](LICENSE) and individual source notices.
