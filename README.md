# Windows ARM64 drivers for Nintendo Switch Lite

Drivers for the **HDH-001 (Tegra T214)** port of Windows 11 ARM64. They use the ACPI devices exposed by the [WinX companion firmware](https://github.com/ruje0504-2/winx). The tested system runs Windows 11 23H2 (22631.2861).

## Drivers on hardware

| Driver | Result on Switch Lite |
| --- | --- |
| `nvtsdhc` | WINARM boots Windows; the current card also exposes FAT32. The latest owner comparison found no clear responsiveness difference between visible and hidden FAT32. [SDR104 / ADMA2 0.4.0.1](drivers/Tegra210Sdhc/README.md#t214-sdr104-candidate) reports 199.68 MHz / 4-bit / 1.8 V, successful tuning and no recorded ADMA errors. Sustained throughput remains unmeasured. |
| `switchwifi` 0.5.5 | Automatically reconnected to a saved WPA2-Personal network. Windows obtained a DHCP lease and default gateway, and the owner confirmed Internet access. |
| `nvtsdhc` 0.5.0.1 DDR200 candidate | Automatic capability detection, manual tuning with an eight-tap minimum and bounded SDR104 recovery are implemented. ARM64 builds, host fault tests and signed package checks pass. Installed with FAT32 visible; physical timing, fallback and throughput remain unverified. [DDR200](drivers/Tegra210Sdhc/README.md#t214-ddr200-candidate-0501) |
| `switchbt` 1.0.0.18 candidate | Discovery has worked, but initialization/search failures recurred. The latest source removes the persistent failed-boot hold and adds one T214 hardware-reset recovery attempt. ARM64 build, host fault tests and signed package checks pass. Installed with DDR200; discovery and lifecycle verification are pending. |
| GPIO, I²C3 and FTM4 touch | The shared controllers and Lite touch driver handle taps in Windows Setup. |
| `switchsio` (analog sticks and gamepad) | Windows detected the HID gamepad and mouse and the driver received controller input. Both sticks are exposed to generic HID games. In-game use awaits testing. |
| `switchsio` desktop controls | Left stick moves the pointer and ZR/ZL click. Capture toggles desktop mode and **+** opens the On-Screen Keyboard (0.2.2 on the card). Version 0.2.3 adds right-stick scrolling, R3 middle click and D-pad keyboard arrows. Hardware test pending. [Button mapping](drivers/SwitchSensor/README.md) |
| `switchaudio` 1.0.0.9 | The built-in speakers play audio. Headphones and jack switching await testing. |
| `switchfan` 1.0.0.6 | The owner confirmed that the fan stops with the screen off and resumes after wake. The driver releases CPU overclock while the screen is off and retains thermal protection. Brightness and 1996.8 MHz CPU / 1866 MHz RAM were observed with 1.0.0.5. Full SoC sleep remains unverified. [Cooling policy](docs/hdh001/README.md#shared-fan-and-t214-clock-control) |
| `switchbattery` 1.0.0.8 / shutdown | Normal Windows shutdown powered off the tested Lite with Wi-Fi, Bluetooth, audio and gamepad enabled. The battery driver now hands IRPs to battc before KMDF owns them. Native restart subsequently passed on the normal R6 firmware without fan pulses. [Power status](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/power-performance.md) |

Windows uses CPU software rendering only. The Tegra GPU driver for Windows has not been implemented.

The USB-C host package is on the Windows 23H2 card and awaits hardware testing. [USB-C host](drivers/TegraXusb/README.md)

Battery, RTC and power-lifecycle results are tracked in the [porting notes](https://github.com/ruje0504-2/winx/blob/hdh001-hekate-sd/docs/hdh001-home/PORTING.md).

## Building and installing

- Start with the [HDH-001 build and install guide](docs/hdh001/README.md) for the ARM64 toolchain, test signing and device packages.
- Use the same card-specific header for both the Windows SD miniport and WinX BL33. The default exposes only `WINARM`. The `--expose-fat32` profile also mounts FAT32 and is currently selected by the owner. The latest comparison did not establish a clear performance difference; neither a confirmed regression cause nor a performance fix is claimed. [SD miniport build](drivers/Tegra210Sdhc/README.md#restricted-winarm-writes-on-hdh-001)
- For Wi-Fi installation, required board firmware and driver diagnostics, see the [SwitchWifi guide](drivers/SwitchWifi/README.md).

## Upstream

Shared driver implementations, SwitchCenter and SwitchPatcher come from [hayato41200/Windows-for-Switch-Drivers](https://github.com/hayato41200/Windows-for-Switch-Drivers). The upstream project documents the original T210/Erista packages and credits Claude for assistance with its repository and README. Some register sequences follow Hekate and Linux. See the [GPL-2.0 license](LICENSE) and individual source notices.
