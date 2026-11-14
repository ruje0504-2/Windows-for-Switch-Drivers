# Tegra214Power (`tegra214pep.sys` 0.2.0.0)

Native WDM Windows PoFx Power Engine Plugin (`ACPI\SWOA0E01`, `Core Platform Extensions`) for **Nintendo Switch Lite (HDH-001, Tegra210B01 / T214)**.

## Implemented Power Model

- **Pure WDM boot-start lifetime**: Follows Microsoft's i.MX PEP lifecycle (`DriverEntry` -> `PoFxRegisterPlugin`, `AddDevice` -> display power callback & coalesced `IoWorkItem` registry diagnostics) with zero `WDFLDR.SYS` import.
- **Extended PSCI v1.0 State IDs** (matching LineageOS / Switchroot TF-A `85081cab2050051ea6e93206ef0f80bc0676e613`, built with `PSCI_EXTENDED_STATE_ID=1`):
  - `IDLE_WFI` (`0`): architectural `__dmb(_ARM64_BARRIER_SY); __wfi()`.
  - `IDLE_C7` (`1`): per-core powerdown via `PoFxCallbackProcessorHalt` (`0x40000007`).
  - `IDLE_CC6` (`2`) / Platform state `0`: coordinated 4-core cluster idle (`0x40000010`), retaining SoC/peripheral/DRAM state.
  - `IDLE_SC7` (`3`) / Platform state `1`: coordinated 4-core SoC deep sleep (`0x4000001B`), entering Switchroot/BPMP SC7 with LPDDR4x self-refresh and PMIC `EN0` (`PMU_EXT` / PMC wake 51) wake routing.
- **Safe Opt-In & Peripheral D3 Coordination**:
  - `Parameters\EnableSc7` defaults to `0` (`HKR,"Parameters","EnableSc7",0x00010003,0`), keeping C7/CC6 only until explicitly enabled for SC7 testing.
  - All deeper states require display-off and stock locked `PLLX` (`<= 1020 MHz`, after `SwitchFan` releases any active CPU boost).
  - Platform state `1` (`SC7`) advertises `PowerDeviceD3` (`PEP_DPM_DEVICE_IDLE_CONSTRAINTS`) and deepest component F-states (`PEP_DPM_COMPONENT_IDLE_CONSTRAINTS`) and verifies `D3` completion across mandatory platform/storage/wake buses (`\_SB.SDM1`, `\_SB.PWRB`, `\_SB.I2C1`, `\_SB.I2C3`, `\_SB.I2C5`) plus any registered client stacks (`\_SB.GPU0`, `\_SB.TCH0`, `\_SB.SIO0`, `\_SB.BAT0`, `\_SB.RTC0`).
  - If a peripheral wakes or `EnableSc7` is cleared between `PEP_NOTIFY_PPM_TEST_IDLE_STATE` and `PEP_NOTIFY_PPM_IDLE_EXECUTE`, the final CPU downgrades cleanly to `CC6` (`0x40000010`) and increments `Sc7Downgrades`.

## Build & Host Verification

```sh
BUILD_T214_PEP=1 sh docs/hdh001/build-drivers.sh . build/sc7
python3 drivers/Tegra214Power/tests/test-processor.py
python3 drivers/Tegra214Power/tests/test-notifications.py
```

## Provenance & License

- License: `MIT` (`SPDX-License-Identifier: MIT`).
- References:
  - Microsoft Windows Driver Kit `pep_x.h` (`PEP_KERNEL_INFORMATION_V3`) and [ms-iot/imx-iotcore `mx6pep`](https://github.com/ms-iot/imx-iotcore/blob/public_preview/driver/power/imx6pep/sys/mx6pep.cpp)
  - Switchroot / LineageOS Nintendo TF-A `85081cab2050051ea6e93206ef0f80bc0676e613` (`plat/nvidia/tegra/soc/t210/plat_psci_handlers.c`, `plat/nvidia/tegra/include/t210/tegra_def.h`)
