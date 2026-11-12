# Switch Lite fan control

Current version: **1.0.0.12**. On 2026-11-12, SSH confirmed the physical Lite
selected `oem86.inf`, runtime `DriverVersion=12`, and `switchfan.sys` SHA-256
`6a1c51a93ef0521482cff45bdb74442dd4aa22711f525adbede603dfc8e896ca`.

| SoC temperature | Requested PWM |
| --- | --- |
| 35 C | 32% (76/236) |
| 38 C | 50% (118/236) |
| 40 C | 75% (177/236) |
| 43 C | 90% (212/236) |
| 45 C and above | 100% (236/236) |

At 33–34 C the previous duty is retained, capped at 76/236; at 32 C or below
the thermal curve requests zero. The existing CPU-overclock cooling floor can
keep the fan on below those thresholds. Failed sensor readings retain fail-safe
cooling. These are PWM requests; fan RPM has not been measured.

At the owner's request, `CpuOverclockEnabled=1` was restored on the device and
the driver read back **1996800 kHz** after its device restart. Thermal throttling,
the screen-off clock restoration policy and system sleep settings were retained.
Host controls covering actual fan callbacks and T214 clock ordering pass; a
physical temperature sweep across every threshold has not been performed.

The optional GPU PMIC bridge is maintained only in
[mesa-switch-woa](https://github.com/ruje0504-2/mesa-switch-woa/tree/main/tegra-x1-wddm/integration/switch-lite/windows/SwitchFan).
The local `src/t214/gpu_pmic.h` forwards to that implementation. Builds defining
`SWITCH_LITE_GPU` must add `mesa-switch-woa/tegra-x1-wddm` to the include path;
GPU driver implementation and test records belong in that main repository.
