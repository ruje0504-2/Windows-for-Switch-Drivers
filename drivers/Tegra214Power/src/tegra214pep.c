/* SPDX-License-Identifier: MIT
 * T214 processor & platform SC7 power backend for Windows PoFx.
 *
 * Follows Microsoft's native WDM PEP notification contract (i.MX PEP lifetime,
 * zero WDFLDR dependency) and Switchroot's pinned T210B01 TF-A extended PSCI
 * state IDs (85081cab2050051ea6e93206ef0f80bc0676e613, PSCI_EXTENDED_STATE_ID=1):
 *   - C7  (core powerdown):     0x40000007
 *   - CC6 (cluster idle):       0x40000010
 *   - SC7 (SoC system suspend): 0x4000001B
 *
 * PoFx owns CPU context save/restore and resume entry points. Never issue a
 * context-losing PSCI CPU_SUSPEND directly from an ordinary driver thread.
 * SC7 remains gated behind Parameters\EnableSc7=1 (default 0), display-off,
 * stock PLLX clock (<= 1020 MHz), and D3 confirmation across critical S0-idle
 * peripherals (SDM1, PWRB, I2C1, I2C3, I2C5, plus any active GPU0, TCH0, SIO0,
 * BAT0, RTC0 stacks).
 */
#include <ntddk.h>
#include <ntstrsafe.h>
#define PEP_KERNEL_INFORMATION_VERSION PEP_KERNEL_INFORMATION_V3
#include <pep_x.h>
#include <intrin.h>

#define CPU_COUNT              4u
#define MAX_COMP_TRACKED       8u

#define IDLE_WFI               0u
#define IDLE_C7                1u
#define IDLE_CC6               2u
#define IDLE_SC7               3u
#define IDLE_COUNT             4u

#define PLAT_STATE_CC6         0u
#define PLAT_STATE_SC7         1u
#define PLAT_STATE_COUNT       2u

/* TF-A 85081cab builds Tegra with PSCI_EXTENDED_STATE_ID=1 (PSTATE_TYPE_SHIFT=30). */
#define PSCI_EXT_POWERDOWN     (1u << 30)
#define PSCI_CORE_POWERDOWN    (PSCI_EXT_POWERDOWN | 0x07u) /* 0x40000007 */
#define PSCI_CLUSTER_IDLE      (PSCI_EXT_POWERDOWN | 0x10u) /* 0x40000010 */
#define PSCI_SOC_POWERDOWN     (PSCI_EXT_POWERDOWN | 0x1Bu) /* 0x4000001B */

#define VETO_DISPLAY_OR_CLOCK  1u
#define VETO_SC7_DISABLED      2u
#define VETO_SC7_DEVICES_BUSY  3u
#define VETO_SYSTEM_TRANSITION 4u
#define VETO_COUNT             4u

#define ADMIT_PERIPHERAL_MASK      0x00FFFFFFu
#define ADMIT_POWER_IRP_TRANSITION (1u << 27)
#define ADMIT_DISPLAY_OFF          (1u << 28)
#define ADMIT_SC7_ENABLED          (1u << 29)
#define ADMIT_PPM_SYSTEM_STATE     (1u << 30)
#define ADMIT_SYSTEM_TRANSITION    (ADMIT_POWER_IRP_TRANSITION | ADMIT_PPM_SYSTEM_STATE)

typedef enum _T214_HANDLE_TAG {
    T214HandleCpu = 1,
    T214HandlePeripheral = 2
} T214_HANDLE_TAG;

typedef struct _T214_CPU {
    ULONG Tag;
    ULONG Index;
    POHANDLE Kernel;
    BOOLEAN Enumerated;
    volatile LONG Halted;
    volatile LONG Attempts;
    volatile LONG Returns;
    volatile LONG LastStatus;
} T214_CPU;

typedef struct _T214_PERIPHERAL {
    ULONG Tag;
    ULONG BitIndex;
    PCWSTR Path;
    UNICODE_STRING Name;
    BOOLEAN Required;
    POHANDLE Kernel;
    volatile LONG Registered;
    volatile LONG Started;
    volatile LONG PowerState;
    volatile LONG DumpPowerReady;
    ULONG ComponentCount;
    ULONG ComponentIdleCount[MAX_COMP_TRACKED];
    volatile LONG ComponentFState[MAX_COMP_TRACKED];
} T214_PERIPHERAL;

static T214_CPU Cpus[CPU_COUNT];
static T214_PERIPHERAL Peripherals[] = {
    {T214HandlePeripheral, 0u, L"\\_SB.SDM1", {0}, TRUE,  NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 1u, L"\\_SB.PWRB", {0}, TRUE,  NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 2u, L"\\_SB.I2C1", {0}, TRUE,  NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 3u, L"\\_SB.I2C3", {0}, TRUE,  NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 4u, L"\\_SB.I2C5", {0}, TRUE,  NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 5u, L"\\_SB.GPU0", {0}, FALSE, NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 6u, L"\\_SB.TCH0", {0}, FALSE, NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 7u, L"\\_SB.SIO0", {0}, FALSE, NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 8u, L"\\_SB.BAT0", {0}, FALSE, NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}},
    {T214HandlePeripheral, 9u, L"\\_SB.RTC0", {0}, FALSE, NULL, 0, 0, PowerDeviceUnspecified, 0, 0, {0}, {0}}
};
#define PERIPHERAL_COUNT RTL_NUMBER_OF(Peripherals)

static PEP_KERNEL_INFORMATION Kernel;
static KSPIN_LOCK PeripheralLock;
static volatile LONG AdmissionState = 0x1F;
static volatile LONG DisplayOffState;
static volatile LONG PendingDisplayOff;
static volatile LONG Sc7Enabled;
static volatile LONG Cc6Attempts;
static volatile LONG Sc7Attempts;
static volatile LONG Sc7Returns;
static volatile LONG Sc7Failures;
static volatile LONG Sc7Downgrades;
static volatile LONG LastSc7Status;
static NTSTATUS RegisterStatus;
static PUCHAR Car;
static WCHAR Parameters[320];
static const GUID DisplayGuid = {0x6fe69556,0x704a,0x47a0,{0x8f,0x24,0xc2,0x8d,0x93,0x6f,0xda,0x47}};
static UNICODE_STRING CpuNames[CPU_COUNT];
static const PCWSTR CpuPaths[CPU_COUNT] = {L"\\_SB.CPU0",L"\\_SB.CPU1",L"\\_SB.CPU2",L"\\_SB.CPU3"};

DRIVER_INITIALIZE DriverEntry;
DRIVER_ADD_DEVICE AddDevice;
DRIVER_DISPATCH DispatchPnp;
DRIVER_DISPATCH DispatchPower;
DRIVER_DISPATCH DispatchForward;
IO_WORKITEM_ROUTINE Snapshot;
PEPCALLBACKPOWERONCRASHDUMPDEVICE PowerOnCrashdumpDevice;

/* PEP is loaded in Core Platform Extensions, before ordinary device stacks.
 * Follow Microsoft's native i.MX PEP lifetime instead of importing WDFLDR.
 * PoFx has no unregister call; processor state and driver code stay resident.
 * The PnP object owns only display notifications and deferred diagnostics. */
typedef struct _T214_DEVICE {
    PDEVICE_OBJECT Lower;
    PIO_WORKITEM Work;
    PVOID DisplayCallback;
    KSPIN_LOCK Lock;
    KEVENT Idle;
    ULONG Generation;
    BOOLEAN Queued;
    BOOLEAN Dirty;
    BOOLEAN Stopping;
} T214_DEVICE;
static T214_DEVICE *AttachedDevice;

static VOID Trace(PCWSTR Name, ULONG Value)
{
    if (Parameters[0] && KeGetCurrentIrql() == PASSIVE_LEVEL) {
        (VOID)RtlWriteRegistryValue(RTL_REGISTRY_ABSOLUTE, Parameters, Name, REG_DWORD, &Value, sizeof(Value));
    }
}

static VOID UpdateAdmissionBits(ULONG ClearMask, ULONG SetMask)
{
    LONG oldVal, newVal;
    do {
        oldVal = InterlockedCompareExchange(&AdmissionState, 0, 0);
        newVal = (LONG)((((ULONG)oldVal) & ~ClearMask) | SetMask);
    } while (InterlockedCompareExchange(&AdmissionState, newVal, oldVal) != oldVal);
}

static ULONG ReadAdmissionSnapshot(VOID)
{
    return (ULONG)InterlockedCompareExchange(&AdmissionState, 0, 0);
}

static LONG ReadEnableSc7FromRegistry(VOID)
{
    ULONG Value = 0;
    RTL_QUERY_REGISTRY_TABLE Table[2];

    if (!Parameters[0] || KeGetCurrentIrql() != PASSIVE_LEVEL) {
        return InterlockedCompareExchange(&Sc7Enabled, 0, 0);
    }
    RtlZeroMemory(Table, sizeof(Table));
    Table[0].Flags = RTL_QUERY_REGISTRY_DIRECT | RTL_QUERY_REGISTRY_TYPECHECK;
    Table[0].Name = L"EnableSc7";
    Table[0].EntryContext = &Value;
    Table[0].DefaultType = (REG_DWORD << RTL_QUERY_REGISTRY_TYPECHECK_SHIFT) | REG_NONE;
    if (!NT_SUCCESS(RtlQueryRegistryValues(RTL_REGISTRY_ABSOLUTE, Parameters, Table, NULL, NULL))) {
        Value = 0;
    }
    return Value != 0 ? 1 : 0;
}

static VOID RefreshEnableSc7FromRegistry(VOID)
{
    LONG sc7 = ReadEnableSc7FromRegistry();
    InterlockedExchange(&Sc7Enabled, sc7);
    UpdateAdmissionBits(ADMIT_SC7_ENABLED, sc7 ? ADMIT_SC7_ENABLED : 0u);
}

static T214_CPU *FindCpu(PCUNICODE_STRING Id)
{
    ULONG i;
    if (!Id) return NULL;
    for (i = 0; i < CPU_COUNT; i++) {
        if (RtlEqualUnicodeString(Id, &CpuNames[i], TRUE)) {
            return &Cpus[i];
        }
    }
    return NULL;
}

static T214_PERIPHERAL *FindPeripheral(PCUNICODE_STRING Id)
{
    ULONG i;
    if (!Id) return NULL;
    for (i = 0; i < PERIPHERAL_COUNT; i++) {
        if (RtlEqualUnicodeString(Id, &Peripherals[i].Name, TRUE)) {
            return &Peripherals[i];
        }
    }
    return NULL;
}

static T214_CPU *CpuHandle(PEPHANDLE Handle)
{
    ULONG i;
    for (i = 0; i < CPU_COUNT; i++) {
        if (Handle == (PEPHANDLE)&Cpus[i]) {
            return &Cpus[i];
        }
    }
    return NULL;
}

static T214_PERIPHERAL *PeripheralHandle(PEPHANDLE Handle)
{
    ULONG i;
    for (i = 0; i < PERIPHERAL_COUNT; i++) {
        if (Handle == (PEPHANDLE)&Peripherals[i]) {
            return &Peripherals[i];
        }
    }
    return NULL;
}

static ULONG OnlineCount(VOID)
{
    ULONG count = 0, i;
    for (i = 0; i < CPU_COUNT; i++) {
        if (Cpus[i].Enumerated && Cpus[i].Kernel) {
            count++;
        }
    }
    return count;
}

static BOOLEAN ClockCanPowerDown(VOID)
{
    static const ULONG Pdiv[] = {1,2,3,4,5,6,8,10,12,16,12,16,20,24,32,32};
    ULONG policy, pll, m, n, p, khz;

    /* SwitchFan restores the boot CPU clock (~998.4-1020 MHz) before screen-off
     * cooling settles. Never race an in-progress PLL relock or power-gate at OC. */
    if (!Car) {
        return FALSE;
    }
    policy = READ_REGISTER_ULONG((PULONG)(Car + 0x20));
    pll = READ_REGISTER_ULONG((PULONG)(Car + 0xe0));
    m = pll & 0xffu;
    n = (pll >> 8) & 0xffu;
    p = (pll >> 20) & 0x1fu;
    if (policy != 0x20008888u || (pll & 0xc8000000u) != 0x48000000u ||
        m == 0 || p >= RTL_NUMBER_OF(Pdiv)) {
        return FALSE;
    }
    khz = (38400u * n) / (m * Pdiv[p]);
    return khz > 0u && khz <= 1020000u;
}

static ULONG RecomputeSc7BlockingMaskLocked(VOID)
{
    ULONG mask = 0, i, c;
    for (i = 0; i < PERIPHERAL_COUNT; i++) {
        T214_PERIPHERAL *p = &Peripherals[i];
        if (!p->Registered) {
            if (p->Required) {
                mask |= (1u << p->BitIndex);
            }
            continue;
        }
        if (p->PowerState != (LONG)PowerDeviceD3 || p->ComponentCount > MAX_COMP_TRACKED) {
            mask |= (1u << p->BitIndex);
            continue;
        }
        for (c = 0; c < p->ComponentCount && c < MAX_COMP_TRACKED; c++) {
            ULONG idle_cnt = p->ComponentIdleCount[c];
            LONG fstate = p->ComponentFState[c];
            if (idle_cnt > 1u && (fstate < 0 || (ULONG)fstate < idle_cnt - 1u)) {
                mask |= (1u << p->BitIndex);
                break;
            }
        }
    }
    UpdateAdmissionBits(ADMIT_PERIPHERAL_MASK, mask & ADMIT_PERIPHERAL_MASK);
    return mask;
}

static ULONG ComputeSc7BlockingMask(VOID)
{
    return ReadAdmissionSnapshot() & ADMIT_PERIPHERAL_MASK;
}

BOOLEAN PowerOnCrashdumpDevice(PPEP_CRASHDUMP_INFORMATION CrashdumpInformation)
{
    T214_PERIPHERAL *dev = CrashdumpInformation ? PeripheralHandle(CrashdumpInformation->DeviceHandle) : NULL;
    /* HIGH_LEVEL, interrupts disabled: the normal WDF/I2C power-on path is
     * unavailable.  Only acknowledge an already completed D0 with no PoFx
     * components. D3/component recovery needs a separate dump-safe path. */
    return dev != NULL && InterlockedCompareExchange(&dev->DumpPowerReady, 0, 0) != 0;
}

static BOOLEAN DeviceNotify(ULONG Notification, PVOID Data)
{
    switch (Notification) {
    case PEP_DPM_PREPARE_DEVICE: {
        PPEP_PREPARE_DEVICE a = Data;
        a->DeviceAccepted = (FindCpu(a->DeviceId) != NULL || FindPeripheral(a->DeviceId) != NULL);
        return TRUE;
    }
    case PEP_DPM_ABANDON_DEVICE: {
        PPEP_ABANDON_DEVICE a = Data;
        a->DeviceAccepted = (FindCpu(a->DeviceId) != NULL || FindPeripheral(a->DeviceId) != NULL);
        return TRUE;
    }
    case PEP_DPM_REGISTER_DEVICE: {
        PPEP_REGISTER_DEVICE a = Data;
        T214_CPU *cpu = FindCpu(a->DeviceId);
        T214_PERIPHERAL *dev = FindPeripheral(a->DeviceId);
        a->DeviceAccepted = PepDeviceNotAccepted;
        if (cpu) {
            cpu->Kernel = a->KernelHandle;
            a->DeviceHandle = (PEPHANDLE)cpu;
            a->DeviceAccepted = PepDeviceAccepted;
            return TRUE;
        }
        if (dev) {
            ULONG c, count = 0;
            ULONG idle_counts[MAX_COMP_TRACKED] = {0};
            KIRQL irql;
            if (a->Register != NULL) {
                PPEP_COMPONENT_V2 *comps = &a->Register->Components[0];
                count = a->Register->ComponentCount;
                for (c = 0; c < count && c < MAX_COMP_TRACKED; c++) {
                    PPEP_COMPONENT_V2 comp = comps[c];
                    idle_counts[c] = comp != NULL ? comp->IdleStateCount : 1u;
                }
            }
            KeAcquireSpinLock(&PeripheralLock, &irql);
            dev->Kernel = a->KernelHandle;
            for (c = 0; c < MAX_COMP_TRACKED; c++) {
                dev->ComponentIdleCount[c] = idle_counts[c];
                InterlockedExchange(&dev->ComponentFState[c], 0);
            }
            dev->ComponentCount = count;
            InterlockedExchange(&dev->DumpPowerReady, 0);
            InterlockedExchange(&dev->Started, 0);
            InterlockedExchange(&dev->PowerState, PowerDeviceD0);
            InterlockedExchange(&dev->Registered, 1);
            (VOID)RecomputeSc7BlockingMaskLocked();
            KeReleaseSpinLock(&PeripheralLock, irql);
            a->DeviceHandle = (PEPHANDLE)dev;
            a->DeviceAccepted = PepDeviceAccepted;
            return TRUE;
        }
        return TRUE;
    }
    case PEP_DPM_UNREGISTER_DEVICE: {
        PEPHANDLE h = ((PPEP_UNREGISTER_DEVICE)Data)->DeviceHandle;
        T214_CPU *cpu = CpuHandle(h);
        T214_PERIPHERAL *dev = PeripheralHandle(h);
        if (cpu) {
            cpu->Kernel = NULL;
            cpu->Enumerated = FALSE;
            return TRUE;
        }
        if (dev) {
            ULONG c;
            KIRQL irql;
            KeAcquireSpinLock(&PeripheralLock, &irql);
            InterlockedExchange(&dev->Registered, 0);
            InterlockedExchange(&dev->DumpPowerReady, 0);
            InterlockedExchange(&dev->Started, 0);
            InterlockedExchange(&dev->PowerState, PowerDeviceUnspecified);
            dev->Kernel = NULL;
            dev->ComponentCount = 0;
            for (c = 0; c < MAX_COMP_TRACKED; c++) {
                dev->ComponentIdleCount[c] = 0;
                InterlockedExchange(&dev->ComponentFState[c], 0);
            }
            (VOID)RecomputeSc7BlockingMaskLocked();
            KeReleaseSpinLock(&PeripheralLock, irql);
            return TRUE;
        }
        return FALSE;
    }
    case PEP_DPM_DEVICE_STARTED: {
        T214_PERIPHERAL *dev = PeripheralHandle(((PPEP_DEVICE_STARTED)Data)->DeviceHandle);
        if (dev) {
            KIRQL irql;
            KeAcquireSpinLock(&PeripheralLock, &irql);
            if (dev->Registered) {
                InterlockedExchange(&dev->Started, 1);
            }
            KeReleaseSpinLock(&PeripheralLock, irql);
            return TRUE;
        }
        return CpuHandle(((PPEP_DEVICE_STARTED)Data)->DeviceHandle) != NULL;
    }
    case PEP_DPM_DEVICE_POWER_STATE: {
        PPEP_DEVICE_POWER_STATE a = Data;
        T214_PERIPHERAL *dev = PeripheralHandle(a->DeviceHandle);
        DEVICE_POWER_STATE pstate;
        BOOLEAN complete;
        KIRQL irql;
        if (CpuHandle(a->DeviceHandle) != NULL) {
            return TRUE;
        }
        if (!dev) {
            return FALSE;
        }
        pstate = a->PowerState;
        complete = a->Complete;
        KeAcquireSpinLock(&PeripheralLock, &irql);
        if (!dev->Registered) {
            KeReleaseSpinLock(&PeripheralLock, irql);
            return FALSE;
        }
        InterlockedExchange(&dev->DumpPowerReady,
                            (dev->ComponentCount == 0u &&
                             pstate == PowerDeviceD0 &&
                             complete) ? 1 : 0);
        if (pstate != PowerDeviceD3) {
            /* Mark active/non-D3 as soon as leaving D3 begins so SC7 cannot race a waking device. */
            InterlockedExchange(&dev->PowerState, (LONG)pstate);
        } else if (complete) {
            /* Mark D3 only after the driver has completed its D3 transition. */
            InterlockedExchange(&dev->PowerState, (LONG)PowerDeviceD3);
        } else {
            /* Incomplete transition into D3 must never appear D3-ready. */
            InterlockedExchange(&dev->PowerState, (LONG)PowerDeviceUnspecified);
        }
        (VOID)RecomputeSc7BlockingMaskLocked();
        KeReleaseSpinLock(&PeripheralLock, irql);
        return TRUE;
    }
    case PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE: {
        PPEP_NOTIFY_COMPONENT_IDLE_STATE a = Data;
        T214_PERIPHERAL *dev = PeripheralHandle(a->DeviceHandle);
        if (!dev && CpuHandle(a->DeviceHandle) == NULL) {
            return FALSE;
        }
        if (dev) {
            ULONG comp = a->Component;
            ULONG idleState = a->IdleState;
            BOOLEAN driverNotified = a->DriverNotified;
            KIRQL irql;
            KeAcquireSpinLock(&PeripheralLock, &irql);
            if (!dev->Registered) {
                KeReleaseSpinLock(&PeripheralLock, irql);
                return FALSE;
            }
            if (comp < dev->ComponentCount && comp < MAX_COMP_TRACKED) {
                LONG current = dev->ComponentFState[comp];
                if (!driverNotified) {
                    if (idleState == 0u || (current > 0 && idleState < (ULONG)current)) {
                        InterlockedExchange(&dev->ComponentFState[comp], (LONG)idleState);
                    }
                } else {
                    InterlockedExchange(&dev->ComponentFState[comp], (LONG)idleState);
                }
                (VOID)RecomputeSc7BlockingMaskLocked();
            }
            KeReleaseSpinLock(&PeripheralLock, irql);
        }
        a->Completed = TRUE;
        return TRUE;
    }
    case PEP_DPM_DEVICE_IDLE_CONSTRAINTS: {
        PPEP_DEVICE_PLATFORM_CONSTRAINTS a = Data;
        T214_PERIPHERAL *dev = PeripheralHandle(a->DeviceHandle);
        if (CpuHandle(a->DeviceHandle) != NULL) {
            if (a->PlatformStateCount > PLAT_STATE_CC6) a->MinimumDStates[PLAT_STATE_CC6] = PowerDeviceUnspecified;
            if (a->PlatformStateCount > PLAT_STATE_SC7) a->MinimumDStates[PLAT_STATE_SC7] = PowerDeviceUnspecified;
            return TRUE;
        }
        if (!dev) {
            return FALSE;
        }
        if (a->PlatformStateCount > PLAT_STATE_CC6) {
            a->MinimumDStates[PLAT_STATE_CC6] = PowerDeviceUnspecified;
        }
        if (a->PlatformStateCount > PLAT_STATE_SC7) {
            a->MinimumDStates[PLAT_STATE_SC7] = PowerDeviceD3;
        }
        return TRUE;
    }
    case PEP_DPM_COMPONENT_IDLE_CONSTRAINTS: {
        PPEP_COMPONENT_PLATFORM_CONSTRAINTS a = Data;
        T214_PERIPHERAL *dev = PeripheralHandle(a->DeviceHandle);
        ULONG deepest = PEP_UNSPECIFIED_MINIMUM_STATE;
        if (CpuHandle(a->DeviceHandle) != NULL) {
            if (a->PlatformStateCount > PLAT_STATE_CC6) a->MinimumFStates[PLAT_STATE_CC6] = PEP_UNSPECIFIED_MINIMUM_STATE;
            if (a->PlatformStateCount > PLAT_STATE_SC7) a->MinimumFStates[PLAT_STATE_SC7] = PEP_UNSPECIFIED_MINIMUM_STATE;
            return TRUE;
        }
        if (!dev) {
            return FALSE;
        }
        {
            ULONG comp = a->Component;
            KIRQL irql;
            KeAcquireSpinLock(&PeripheralLock, &irql);
            if (dev->Registered && comp < dev->ComponentCount && comp < MAX_COMP_TRACKED &&
                dev->ComponentIdleCount[comp] > 1u) {
                deepest = dev->ComponentIdleCount[comp] - 1u;
            }
            KeReleaseSpinLock(&PeripheralLock, irql);
        }
        if (a->PlatformStateCount > PLAT_STATE_CC6) {
            a->MinimumFStates[PLAT_STATE_CC6] = PEP_UNSPECIFIED_MINIMUM_STATE;
        }
        if (a->PlatformStateCount > PLAT_STATE_SC7) {
            a->MinimumFStates[PLAT_STATE_SC7] = deepest;
        }
        return TRUE;
    }
    case PEP_DPM_WORK: {
        PPEP_WORK a = Data;
        a->NeedWork = FALSE;
        return TRUE;
    }
    case PEP_DPM_POWER_CONTROL_REQUEST: {
        PPEP_POWER_CONTROL_REQUEST a = Data;
        a->BytesReturned = 0;
        a->Status = STATUS_NOT_SUPPORTED;
        return TRUE;
    }
    case PEP_DPM_POWER_CONTROL_COMPLETE:
    case PEP_DPM_SYSTEM_LATENCY_UPDATE:
        return TRUE;
    case PEP_DPM_REGISTER_CRASHDUMP_DEVICE: {
        PPEP_REGISTER_CRASHDUMP_DEVICE a = Data;
        T214_PERIPHERAL *dev = PeripheralHandle(a->DeviceHandle);
        if (dev == NULL || !InterlockedCompareExchange(&dev->Registered, 0, 0)) {
            return FALSE;
        }
        a->PowerOnDumpDeviceCallback = PowerOnCrashdumpDevice;
        return TRUE;
    }
    default:
        return FALSE;
    }
}

static BOOLEAN ProcessorNotify(PEPHANDLE Handle, ULONG Notification, PVOID Data)
{
    T214_CPU *cpu = CpuHandle(Handle);
    switch (Notification) {
    case PEP_NOTIFY_PPM_QUERY_CAPABILITIES: {
        PPEP_PPM_QUERY_CAPABILITIES a = Data;
        if (!cpu) return FALSE;
        a->FeedbackCounterCount = 0;
        a->IdleStateCount = IDLE_COUNT;
        a->PerformanceStatesSupported = FALSE;
        a->ParkingSupported = FALSE;
        a->DiscretePerformanceStateCount = 0;
        cpu->Enumerated = TRUE;
        return TRUE;
    }
    case PEP_NOTIFY_PPM_QUERY_IDLE_STATES_V2: {
        PPEP_PPM_QUERY_IDLE_STATES_V2 a = Data;
        PPEP_PROCESSOR_IDLE_STATE_V2 states;
        ULONG i;
        if (!cpu || a->Count != IDLE_COUNT) return FALSE;
        states = &a->IdleStates[0];
        RtlZeroMemory(states, sizeof(states[0]) * IDLE_COUNT);
        for (i = 0; i < IDLE_COUNT; i++) {
            PPEP_PROCESSOR_IDLE_STATE_V2 s = &states[i];
            s->Interruptible = TRUE;
            s->WakesSpuriously = TRUE;
            s->CacheCoherent = (i == IDLE_WFI);
            s->ThreadContextRetained = (i == IDLE_WFI);
            s->PlatformOnly = (i == IDLE_CC6 || i == IDLE_SC7);
        }
        /* 100 ns units: WFI (0), C7 (1 ms / 10 ms), CC6 (2 ms / 20 ms), SC7 (15 ms / 500 ms). */
        states[IDLE_WFI].Latency = 0;
        states[IDLE_WFI].BreakEvenDuration = 0;
        states[IDLE_C7].Latency = 10000;
        states[IDLE_C7].BreakEvenDuration = 100000;
        states[IDLE_CC6].Latency = 20000;
        states[IDLE_CC6].BreakEvenDuration = 200000;
        states[IDLE_SC7].Latency = 150000;
        states[IDLE_SC7].BreakEvenDuration = 5000000;
        return TRUE;
    }
    case PEP_NOTIFY_PPM_QUERY_PLATFORM_STATES:
        ((PPEP_PPM_QUERY_PLATFORM_STATES)Data)->PlatformStateCount = PLAT_STATE_COUNT;
        return TRUE;
    case PEP_NOTIFY_PPM_QUERY_COORDINATED_STATES: {
        PPEP_PPM_QUERY_COORDINATED_STATES a = Data;
        PPEP_COORDINATED_IDLE_STATE cstates;
        ULONG online = OnlineCount();
        if (a->Count != PLAT_STATE_COUNT) return FALSE;
        cstates = &a->States[0];
        cstates[PLAT_STATE_CC6].Latency = 20000;
        cstates[PLAT_STATE_CC6].BreakEvenDuration = 200000;
        cstates[PLAT_STATE_CC6].DependencyCount = online;
        cstates[PLAT_STATE_CC6].MaximumDependencySize = 2;

        cstates[PLAT_STATE_SC7].Latency = 150000;
        cstates[PLAT_STATE_SC7].BreakEvenDuration = 5000000;
        cstates[PLAT_STATE_SC7].DependencyCount = online;
        cstates[PLAT_STATE_SC7].MaximumDependencySize = 3;
        return TRUE;
    }
    case PEP_NOTIFY_PPM_QUERY_COORDINATED_DEPENDENCY: {
        PPEP_PPM_QUERY_COORDINATED_DEPENDENCY a = Data;
        PPEP_COORDINATED_DEPENDENCY_OPTION opts;
        ULONG index = 0, i;
        if (a->StateIndex >= PLAT_STATE_COUNT) return FALSE;
        if (a->StateIndex == PLAT_STATE_CC6 && a->DependencySize < 2) return FALSE;
        if (a->StateIndex == PLAT_STATE_SC7 && a->DependencySize < 3) return FALSE;
        opts = &a->Options[0];
        for (i = 0; i < CPU_COUNT; i++) {
            if (!Cpus[i].Enumerated || !Cpus[i].Kernel) {
                continue;
            }
            if (index++ != a->DependencyIndex) {
                continue;
            }
            a->TargetProcessor = Cpus[i].Kernel;
            if (a->StateIndex == PLAT_STATE_CC6) {
                a->DependencySizeUsed = 2;
                opts[0].ExpectedStateIndex = IDLE_CC6;
                opts[0].LooseDependency = TRUE;
                opts[0].InitiatingState = TRUE;
                opts[0].DependentState = TRUE;
                opts[1].ExpectedStateIndex = IDLE_C7;
                opts[1].LooseDependency = TRUE;
                opts[1].InitiatingState = FALSE;
                opts[1].DependentState = TRUE;
            } else {
                a->DependencySizeUsed = 3;
                opts[0].ExpectedStateIndex = IDLE_SC7;
                opts[0].LooseDependency = TRUE;
                opts[0].InitiatingState = TRUE;
                opts[0].DependentState = TRUE;
                opts[1].ExpectedStateIndex = IDLE_CC6;
                opts[1].LooseDependency = TRUE;
                opts[1].InitiatingState = FALSE;
                opts[1].DependentState = TRUE;
                opts[2].ExpectedStateIndex = IDLE_C7;
                opts[2].LooseDependency = TRUE;
                opts[2].InitiatingState = FALSE;
                opts[2].DependentState = TRUE;
            }
            return TRUE;
        }
        return FALSE;
    }
    case PEP_NOTIFY_PPM_TEST_IDLE_STATE: {
        PPEP_PPM_TEST_IDLE_STATE a = Data;
        ULONG snap = ReadAdmissionSnapshot();
        if (a->ProcessorState != IDLE_WFI || a->PlatformState != PEP_PLATFORM_IDLE_STATE_NONE) {
            if ((snap & ADMIT_SYSTEM_TRANSITION) != 0u) {
                a->VetoReason = VETO_SYSTEM_TRANSITION;
                return TRUE;
            }
            if ((snap & ADMIT_DISPLAY_OFF) == 0u || !ClockCanPowerDown()) {
                a->VetoReason = VETO_DISPLAY_OR_CLOCK;
                return TRUE;
            }
            /* Re-sample coherent admission snapshot after CAR MMIO read. */
            snap = ReadAdmissionSnapshot();
            if ((snap & ADMIT_SYSTEM_TRANSITION) != 0u) {
                a->VetoReason = VETO_SYSTEM_TRANSITION;
                return TRUE;
            }
            if ((snap & ADMIT_DISPLAY_OFF) == 0u) {
                a->VetoReason = VETO_DISPLAY_OR_CLOCK;
                return TRUE;
            }
        }
        if (a->ProcessorState == IDLE_SC7 || a->PlatformState == PLAT_STATE_SC7) {
            if ((snap & ADMIT_SC7_ENABLED) == 0u) {
                a->VetoReason = VETO_SC7_DISABLED;
                return TRUE;
            }
            if ((snap & ADMIT_PERIPHERAL_MASK) != 0u) {
                a->VetoReason = VETO_SC7_DEVICES_BUSY;
                return TRUE;
            }
        }
        a->VetoReason = PEP_IDLE_VETO_NONE;
        return TRUE;
    }
    case PEP_NOTIFY_PPM_IDLE_EXECUTE: {
        PPEP_PPM_IDLE_EXECUTE_V2 a = Data;
        ULONG state, snap;
        BOOLEAN isSc7 = FALSE;
        if (!cpu) return FALSE;
        if (a->ProcessorState >= IDLE_COUNT ||
            (a->PlatformState != PEP_PLATFORM_IDLE_STATE_NONE && a->PlatformState >= PLAT_STATE_COUNT)) {
            a->Status = STATUS_INVALID_PARAMETER;
            return TRUE;
        }
        if (a->ProcessorState == IDLE_WFI) {
            __dmb(_ARM64_BARRIER_SY);
            __wfi();
            a->Status = STATUS_SUCCESS;
            return TRUE;
        }
        snap = ReadAdmissionSnapshot();
        if ((snap & ADMIT_SYSTEM_TRANSITION) != 0u ||
            (snap & ADMIT_DISPLAY_OFF) == 0u ||
            !ClockCanPowerDown()) {
            __dmb(_ARM64_BARRIER_SY);
            __wfi();
            a->Status = STATUS_SUCCESS;
            return TRUE;
        }
        /* Re-sample coherent admission word after CAR MMIO read so final admission is atomic. */
        snap = ReadAdmissionSnapshot();
        if ((snap & ADMIT_SYSTEM_TRANSITION) != 0u || (snap & ADMIT_DISPLAY_OFF) == 0u) {
            __dmb(_ARM64_BARRIER_SY);
            __wfi();
            a->Status = STATUS_SUCCESS;
            return TRUE;
        }
        if (a->PlatformState == PLAT_STATE_SC7) {
            if ((snap & ADMIT_SC7_ENABLED) != 0u && (snap & ADMIT_PERIPHERAL_MASK) == 0u) {
                state = PSCI_SOC_POWERDOWN;
                isSc7 = TRUE;
                InterlockedIncrement(&Sc7Attempts);
            } else {
                /* Mid-transition peripheral wakeup or SC7 disable: downgrade cleanly to CC6. */
                state = PSCI_CLUSTER_IDLE;
                InterlockedIncrement(&Sc7Downgrades);
                InterlockedIncrement(&Cc6Attempts);
            }
        } else if (a->PlatformState == PLAT_STATE_CC6) {
            state = PSCI_CLUSTER_IDLE;
            InterlockedIncrement(&Cc6Attempts);
        } else {
            state = PSCI_CORE_POWERDOWN;
        }
        InterlockedIncrement(&cpu->Attempts);
        InterlockedExchange(&cpu->Halted, 1);
        a->Status = Kernel.ProcessorHalt(
            PROCESSOR_HALT_VIA_PSCI_CPU_SUSPEND | PROCESSOR_HALT_CACHE_FLUSH_OVERRIDE,
            &state, NULL);
        InterlockedExchange(&cpu->Halted, 0);
        InterlockedExchange(&cpu->LastStatus, a->Status);
        InterlockedIncrement(&cpu->Returns);
        if (isSc7) {
            InterlockedExchange(&LastSc7Status, a->Status);
            if (NT_SUCCESS(a->Status)) {
                InterlockedIncrement(&Sc7Returns);
            } else {
                InterlockedIncrement(&Sc7Failures);
            }
        }
        return TRUE;
    }
    case PEP_NOTIFY_PPM_IDLE_COMPLETE:
        return TRUE;
    case PEP_NOTIFY_PPM_ENTER_SYSTEM_STATE: {
        PPEP_PPM_ENTER_SYSTEM_STATE a = Data;
        if (!a || a->TargetState > PowerSystemWorking) {
            UpdateAdmissionBits(0u, ADMIT_PPM_SYSTEM_STATE);
        }
        return TRUE;
    }
    case PEP_NOTIFY_PPM_RESUME_FROM_SYSTEM_STATE:
        UpdateAdmissionBits(ADMIT_PPM_SYSTEM_STATE, 0u);
        return TRUE;
    case PEP_NOTIFY_PPM_LPI_SUPPORTED:
        ((PPEP_PPM_LPI_SUPPORTED)Data)->OverrideLpiStates = TRUE;
        return TRUE;
    case PEP_NOTIFY_PPM_IS_PROCESSOR_HALTED:
        if (!cpu) return FALSE;
        ((PPEP_PPM_IS_PROCESSOR_HALTED)Data)->Halted =
            (BOOLEAN)InterlockedCompareExchange(&cpu->Halted, 0, 0);
        return TRUE;
    case PEP_NOTIFY_PPM_INITIATE_WAKE:
        ((PPEP_PPM_INITIATE_WAKE)Data)->NeedInterruptForCompletion = TRUE;
        return TRUE;
    case PEP_NOTIFY_PPM_QUERY_VETO_REASONS:
        ((PPEP_PPM_QUERY_VETO_REASONS)Data)->VetoReasonCount = VETO_COUNT;
        return TRUE;
    case PEP_NOTIFY_PPM_QUERY_VETO_REASON: {
        PPEP_PPM_QUERY_VETO_REASON a = Data;
        static const WCHAR v1[] = L"Display active or CPU clock transition";
        static const WCHAR v2[] = L"SC7 opt-in disabled (EnableSc7=0)";
        static const WCHAR v3[] = L"Critical peripherals not yet in D3";
        static const WCHAR v4[] = L"System power state transition in progress";
        const WCHAR *src = NULL;
        USHORT bytes = 0;
        if (a->VetoReason == VETO_DISPLAY_OR_CLOCK) {
            src = v1; bytes = sizeof(v1);
        } else if (a->VetoReason == VETO_SC7_DISABLED) {
            src = v2; bytes = sizeof(v2);
        } else if (a->VetoReason == VETO_SC7_DEVICES_BUSY) {
            src = v3; bytes = sizeof(v3);
        } else if (a->VetoReason == VETO_SYSTEM_TRANSITION) {
            src = v4; bytes = sizeof(v4);
        } else {
            return FALSE;
        }
        if (a->Name && a->NameSize >= bytes) {
            RtlCopyMemory(a->Name, src, bytes);
        }
        a->NameSize = bytes;
        return TRUE;
    }
    default:
        return FALSE;
    }
}

static VOID QueueSnapshotLocked(T214_DEVICE *Device)
{
    if (!Device->Stopping) {
        Device->Dirty = TRUE;
        if (!Device->Queued) {
            Device->Queued = TRUE;
            KeClearEvent(&Device->Idle);
            IoQueueWorkItem(Device->Work, Snapshot, DelayedWorkQueue, Device);
        }
    }
}

static VOID QueueSnapshot(T214_DEVICE *Device)
{
    KIRQL irql;
    if (!Device) return;
    KeAcquireSpinLock(&Device->Lock, &irql);
    QueueSnapshotLocked(Device);
    KeReleaseSpinLock(&Device->Lock, irql);
}

static NTSTATUS DisplayChanged(LPCGUID Setting, PVOID Value, ULONG Length, PVOID Context)
{
    if (IsEqualGUID(Setting, &DisplayGuid) && Length == sizeof(ULONG)) {
        LONG off = (*(PULONG)Value == 0) ? 1 : 0;
        T214_DEVICE *device = (T214_DEVICE *)Context;
        if (!device) {
            InterlockedExchange(&PendingDisplayOff, off);
            InterlockedExchange(&DisplayOffState, off);
            UpdateAdmissionBits(ADMIT_DISPLAY_OFF, off ? ADMIT_DISPLAY_OFF : 0u);
            return STATUS_SUCCESS;
        }
        {
            KIRQL irql;
            KeAcquireSpinLock(&device->Lock, &irql);
            if (!device->Stopping) {
                InterlockedExchange(&PendingDisplayOff, off);
                device->Generation++;
                if (!off) {
                    /* Veto power-down immediately when display wakes; defer arming until Snapshot refreshes EnableSc7. */
                    InterlockedExchange(&DisplayOffState, 0);
                    UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
                }
                QueueSnapshotLocked(device);
            }
            KeReleaseSpinLock(&device->Lock, irql);
        }
    }
    return STATUS_SUCCESS;
}

VOID Snapshot(PDEVICE_OBJECT Object, PVOID Context)
{
    T214_DEVICE *device = Context;
    ULONG attempts, returns, i, generation;
    LONG sc7, off;
    KIRQL irql;
    UNREFERENCED_PARAMETER(Object);

    for (;;) {
        KeAcquireSpinLock(&device->Lock, &irql);
        if (device->Stopping) {
            InterlockedExchange(&PendingDisplayOff, 0);
            InterlockedExchange(&DisplayOffState, 0);
            UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
            device->Dirty = FALSE;
            device->Queued = FALSE;
            KeSetEvent(&device->Idle, IO_NO_INCREMENT, FALSE);
            KeReleaseSpinLock(&device->Lock, irql);
            return;
        }
        device->Dirty = FALSE;
        generation = device->Generation;
        KeReleaseSpinLock(&device->Lock, irql);

        /* Registry I/O starts only after PnP start, never during early PEP load. */
        (VOID)RtlCreateRegistryKey(RTL_REGISTRY_ABSOLUTE, Parameters);
        sc7 = ReadEnableSc7FromRegistry();

        KeAcquireSpinLock(&device->Lock, &irql);
        if (device->Stopping) {
            InterlockedExchange(&PendingDisplayOff, 0);
            InterlockedExchange(&DisplayOffState, 0);
            UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
            device->Dirty = FALSE;
            device->Queued = FALSE;
            KeSetEvent(&device->Idle, IO_NO_INCREMENT, FALSE);
            KeReleaseSpinLock(&device->Lock, irql);
            return;
        }
        if (device->Generation != generation) {
            /* A newer display transition arrived during registry refresh; re-evaluate before committing. */
            KeReleaseSpinLock(&device->Lock, irql);
            continue;
        }
        off = InterlockedCompareExchange(&PendingDisplayOff, 0, 0);
        InterlockedExchange(&Sc7Enabled, sc7);
        InterlockedExchange(&DisplayOffState, off);
        UpdateAdmissionBits(ADMIT_SC7_ENABLED | ADMIT_DISPLAY_OFF,
                            (sc7 ? ADMIT_SC7_ENABLED : 0u) | (off ? ADMIT_DISPLAY_OFF : 0u));
        KeReleaseSpinLock(&device->Lock, irql);

        attempts = 0;
        returns = 0;
        Trace(L"RegisterStatus", (ULONG)RegisterStatus);
        Trace(L"DriverVersion", 3);
        Trace(L"EnableSc7Active", (ULONG)InterlockedCompareExchange(&Sc7Enabled, 0, 0));
        for (i = 0; i < CPU_COUNT; i++) {
            attempts += (ULONG)InterlockedCompareExchange(&Cpus[i].Attempts, 0, 0);
            returns += (ULONG)InterlockedCompareExchange(&Cpus[i].Returns, 0, 0);
            if (Cpus[i].Enumerated) {
                Trace(L"LastHaltStatus", (ULONG)InterlockedCompareExchange(&Cpus[i].LastStatus, 0, 0));
            }
        }
        Trace(L"HaltAttempts", attempts);
        Trace(L"HaltReturns", returns);
        Trace(L"Cc6Attempts", (ULONG)InterlockedCompareExchange(&Cc6Attempts, 0, 0));
        Trace(L"Sc7Attempts", (ULONG)InterlockedCompareExchange(&Sc7Attempts, 0, 0));
        Trace(L"Sc7Returns", (ULONG)InterlockedCompareExchange(&Sc7Returns, 0, 0));
        Trace(L"Sc7Failures", (ULONG)InterlockedCompareExchange(&Sc7Failures, 0, 0));
        Trace(L"Sc7Downgrades", (ULONG)InterlockedCompareExchange(&Sc7Downgrades, 0, 0));
        Trace(L"LastSc7Status", (ULONG)InterlockedCompareExchange(&LastSc7Status, 0, 0));
        Trace(L"Sc7BlockingMask", ComputeSc7BlockingMask());
        Trace(L"ProcessorCount", OnlineCount());
        Trace(L"DisplayOff", (ULONG)InterlockedCompareExchange(&DisplayOffState, 0, 0));

        KeAcquireSpinLock(&device->Lock, &irql);
        if (device->Stopping || !device->Dirty) {
            if (device->Stopping) {
                InterlockedExchange(&PendingDisplayOff, 0);
                InterlockedExchange(&DisplayOffState, 0);
                UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
            }
            device->Dirty = FALSE;
            device->Queued = FALSE;
            KeSetEvent(&device->Idle, IO_NO_INCREMENT, FALSE);
            KeReleaseSpinLock(&device->Lock, irql);
            return;
        }
        KeReleaseSpinLock(&device->Lock, irql);
    }
}

static VOID StopNotifications(T214_DEVICE *Device)
{
    KIRQL irql;
    PVOID callback;
    KeAcquireSpinLock(&Device->Lock, &irql);
    Device->Stopping = TRUE;
    Device->Dirty = FALSE;
    Device->Generation++;
    InterlockedExchange(&PendingDisplayOff, 0);
    InterlockedExchange(&DisplayOffState, 0);
    UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
    KeReleaseSpinLock(&Device->Lock, irql);
    callback = Device->DisplayCallback;
    if (callback) {
        Device->DisplayCallback = NULL;
        (VOID)PoUnregisterPowerSettingCallback(callback);
    }
    (VOID)KeWaitForSingleObject(&Device->Idle, Executive, KernelMode, FALSE, NULL);
    /* Synchronize with Snapshot's final spinlock release before Remove can free the FDO. */
    KeAcquireSpinLock(&Device->Lock, &irql);
    InterlockedExchange(&PendingDisplayOff, 0);
    InterlockedExchange(&DisplayOffState, 0);
    UpdateAdmissionBits(ADMIT_DISPLAY_OFF, 0u);
    KeReleaseSpinLock(&Device->Lock, irql);
}

static NTSTATUS StartNotifications(PDEVICE_OBJECT Object)
{
    T214_DEVICE *device = Object->DeviceExtension;
    KIRQL irql;
    NTSTATUS status;
    if (device->DisplayCallback) return STATUS_SUCCESS;
    KeAcquireSpinLock(&device->Lock, &irql);
    device->Stopping = FALSE;
    device->Dirty = FALSE;
    KeReleaseSpinLock(&device->Lock, irql);
    status = PoRegisterPowerSettingCallback(Object, &DisplayGuid, DisplayChanged,
                                            device, &device->DisplayCallback);
    if (NT_SUCCESS(status)) {
        QueueSnapshot(device);
    } else {
        StopNotifications(device);
    }
    return status;
}

static NTSTATUS Complete(PIRP Irp, NTSTATUS Status)
{
    Irp->IoStatus.Status = Status;
    IoCompleteRequest(Irp, IO_NO_INCREMENT);
    return Status;
}

static NTSTATUS ForwardComplete(PDEVICE_OBJECT Object, PIRP Irp, PVOID Context)
{
    UNREFERENCED_PARAMETER(Object);
    UNREFERENCED_PARAMETER(Irp);
    KeSetEvent((PKEVENT)Context, IO_NO_INCREMENT, FALSE);
    return STATUS_MORE_PROCESSING_REQUIRED;
}

static NTSTATUS ForwardAndWait(T214_DEVICE *Device, PIRP Irp)
{
    KEVENT event;
    NTSTATUS status;
    KeInitializeEvent(&event, NotificationEvent, FALSE);
    IoCopyCurrentIrpStackLocationToNext(Irp);
    IoSetCompletionRoutine(Irp, ForwardComplete, &event, TRUE, TRUE, TRUE);
    status = IoCallDriver(Device->Lower, Irp);
    if (status == STATUS_PENDING) {
        (VOID)KeWaitForSingleObject(&event, Executive, KernelMode, FALSE, NULL);
    }
    return Irp->IoStatus.Status;
}

NTSTATUS DispatchForward(PDEVICE_OBJECT Object, PIRP Irp)
{
    T214_DEVICE *device = Object->DeviceExtension;
    IoSkipCurrentIrpStackLocation(Irp);
    return IoCallDriver(device->Lower, Irp);
}

static NTSTATUS SystemPowerResumeComplete(PDEVICE_OBJECT Object, PIRP Irp, PVOID Context)
{
    UNREFERENCED_PARAMETER(Object);
    UNREFERENCED_PARAMETER(Context);
    if (Irp->PendingReturned) {
        IoMarkIrpPending(Irp);
    }
    if (NT_SUCCESS(Irp->IoStatus.Status)) {
        UpdateAdmissionBits(ADMIT_POWER_IRP_TRANSITION, 0u);
    }
    PoStartNextPowerIrp(Irp);
    return STATUS_SUCCESS;
}

NTSTATUS DispatchPower(PDEVICE_OBJECT Object, PIRP Irp)
{
    T214_DEVICE *device = Object->DeviceExtension;
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(Irp);

    switch (stack->MinorFunction) {
    case IRP_MN_SET_POWER:
        Irp->IoStatus.Status = STATUS_SUCCESS;
        if (stack->Parameters.Power.Type == SystemPowerState) {
            SYSTEM_POWER_STATE target = stack->Parameters.Power.State.SystemState;
            if (target > PowerSystemWorking) {
                UpdateAdmissionBits(0u, ADMIT_POWER_IRP_TRANSITION);
            } else if (target == PowerSystemWorking) {
                IoCopyCurrentIrpStackLocationToNext(Irp);
                IoSetCompletionRoutine(Irp, SystemPowerResumeComplete, device, TRUE, TRUE, TRUE);
                return PoCallDriver(device->Lower, Irp);
            }
        }
        break;
    case IRP_MN_QUERY_POWER:
        Irp->IoStatus.Status = STATUS_SUCCESS;
        break;
    default:
        break;
    }

    PoStartNextPowerIrp(Irp);
    IoSkipCurrentIrpStackLocation(Irp);
    return PoCallDriver(device->Lower, Irp);
}

NTSTATUS DispatchPnp(PDEVICE_OBJECT Object, PIRP Irp)
{
    T214_DEVICE *device = Object->DeviceExtension;
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(Irp);
    NTSTATUS status;
    switch (stack->MinorFunction) {
    case IRP_MN_START_DEVICE:
        status = ForwardAndWait(device, Irp);
        if (NT_SUCCESS(status)) status = StartNotifications(Object);
        return Complete(Irp, status);
    case IRP_MN_QUERY_CAPABILITIES:
        status = ForwardAndWait(device, Irp);
        if (NT_SUCCESS(status)) {
            PDEVICE_CAPABILITIES cap = stack->Parameters.DeviceCapabilities.Capabilities;
            cap->EjectSupported = FALSE;
            cap->Removable = FALSE;
            cap->SilentInstall = TRUE;
        }
        return Complete(Irp, status);
    case IRP_MN_QUERY_REMOVE_DEVICE:
    case IRP_MN_QUERY_STOP_DEVICE:
        return Complete(Irp, STATUS_UNSUCCESSFUL);
    case IRP_MN_STOP_DEVICE:
    case IRP_MN_SURPRISE_REMOVAL:
        StopNotifications(device);
        Irp->IoStatus.Status = STATUS_SUCCESS;
        return DispatchForward(Object, Irp);
    case IRP_MN_REMOVE_DEVICE: {
        /* Start failure can still cause removal despite query-remove refusal. */
        PDEVICE_OBJECT lower = device->Lower;
        StopNotifications(device);
        Irp->IoStatus.Status = STATUS_SUCCESS;
        IoSkipCurrentIrpStackLocation(Irp);
        status = IoCallDriver(lower, Irp);
        IoDetachDevice(lower);
        IoFreeWorkItem(device->Work);
        AttachedDevice = NULL;
        IoDeleteDevice(Object);
        return status;
    }
    default:
        return DispatchForward(Object, Irp);
    }
}

NTSTATUS AddDevice(PDRIVER_OBJECT Driver, PDEVICE_OBJECT PhysicalDevice)
{
    PDEVICE_OBJECT object;
    NTSTATUS status;
    T214_DEVICE *device;

    if (AttachedDevice) return STATUS_DEVICE_BUSY;
    status = IoCreateDevice(Driver, sizeof(T214_DEVICE), NULL, FILE_DEVICE_UNKNOWN,
                            FILE_DEVICE_SECURE_OPEN, FALSE, &object);
    if (!NT_SUCCESS(status)) return status;
    device = object->DeviceExtension;
    KeInitializeSpinLock(&device->Lock);
    KeInitializeEvent(&device->Idle, NotificationEvent, TRUE);
    device->Stopping = TRUE;
    device->Work = IoAllocateWorkItem(object);
    if (!device->Work) {
        IoDeleteDevice(object);
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    status = IoAttachDeviceToDeviceStackSafe(object, PhysicalDevice, &device->Lower);
    if (!NT_SUCCESS(status)) {
        IoFreeWorkItem(device->Work);
        IoDeleteDevice(object);
        return status;
    }
    AttachedDevice = device;
    object->Flags &= ~(DO_DEVICE_INITIALIZING | DO_POWER_PAGABLE);
    return STATUS_SUCCESS;
}

NTSTATUS DriverEntry(PDRIVER_OBJECT Object, PUNICODE_STRING Path)
{
    PEP_INFORMATION info;
    PHYSICAL_ADDRESS pa;
    PULONG hid;
    ULONG id, i;
    NTSTATUS status;

    RtlZeroMemory(&info, sizeof(info));
    if (Path->Length + sizeof(L"\\Parameters") > sizeof(Parameters)) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlCopyMemory(Parameters, Path->Buffer, Path->Length);
    RtlCopyMemory((PUCHAR)Parameters + Path->Length, L"\\Parameters", sizeof(L"\\Parameters"));

    pa.QuadPart = 0x70000804;
    hid = MmMapIoSpaceEx(pa, sizeof(ULONG), PAGE_READWRITE | PAGE_NOCACHE);
    if (!hid) return STATUS_INSUFFICIENT_RESOURCES;
    id = READ_REGISTER_ULONG(hid);
    MmUnmapIoSpace(hid, sizeof(ULONG));
    if ((id & 0xfff0u) != 0x2120u) {
        return STATUS_NOT_SUPPORTED;
    }

    pa.QuadPart = 0x60006000;
    Car = MmMapIoSpaceEx(pa, 0x100, PAGE_READWRITE | PAGE_NOCACHE);
    if (!Car) return STATUS_INSUFFICIENT_RESOURCES;

    for (i = 0; i <= IRP_MJ_MAXIMUM_FUNCTION; i++) {
        Object->MajorFunction[i] = DispatchForward;
    }
    Object->MajorFunction[IRP_MJ_PNP] = DispatchPnp;
    Object->MajorFunction[IRP_MJ_POWER] = DispatchPower;
    Object->DriverExtension->AddDevice = AddDevice;
    /* No unload callback: PoFx retains processor/device callbacks for this boot. */
    KeInitializeSpinLock(&PeripheralLock);
    for (i = 0; i < CPU_COUNT; i++) {
        Cpus[i].Tag = T214HandleCpu;
        Cpus[i].Index = i;
        RtlInitUnicodeString(&CpuNames[i], CpuPaths[i]);
    }
    for (i = 0; i < PERIPHERAL_COUNT; i++) {
        RtlInitUnicodeString(&Peripherals[i].Name, Peripherals[i].Path);
    }
    {
        KIRQL irql;
        KeAcquireSpinLock(&PeripheralLock, &irql);
        (VOID)RecomputeSc7BlockingMaskLocked();
        KeReleaseSpinLock(&PeripheralLock, irql);
    }
    RefreshEnableSc7FromRegistry();

    info.Version = PEP_INFORMATION_VERSION;
    info.Size = sizeof(info);
    info.AcceptDeviceNotification = DeviceNotify;
    info.AcceptProcessorNotification = ProcessorNotify;
    Kernel.Version = PEP_KERNEL_INFORMATION_V3;
    Kernel.Size = sizeof(Kernel);
    status = PoFxRegisterPlugin(&info, &Kernel);
    RegisterStatus = status;
    if (!NT_SUCCESS(status)) {
        MmUnmapIoSpace(Car, 0x100);
        Car = NULL;
    }
    return status;
}
