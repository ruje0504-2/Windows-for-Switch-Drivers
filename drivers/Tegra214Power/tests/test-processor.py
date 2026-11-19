#!/usr/bin/env python3
"""Exercise real Tegra214Power PoFx processor/platform/DPM state machine, extended PSCI IDs and SC7 gates."""
from pathlib import Path
import os
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra214pep.c").read_text()
# Strip WDK headers so we compile the exact tegra214pep.c translation unit against strict C11 mocks.
body = source[source.index("#define CPU_COUNT"):]

header = r'''
#define _DARWIN_C_SOURCE 1
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

typedef void VOID, *PVOID, *PEPHANDLE, *POHANDLE, *PIO_WORKITEM;
typedef struct _DEVICE_OBJECT *PDEVICE_OBJECT;
typedef struct _DRIVER_OBJECT *PDRIVER_OBJECT;
typedef uint8_t UCHAR, *PUCHAR, BOOLEAN, KIRQL;
typedef uint16_t USHORT;
typedef wchar_t WCHAR, *PWSTR;
typedef const WCHAR *PCWSTR;
typedef uint32_t ULONG, *PULONG;
typedef int32_t LONG, NTSTATUS;
typedef uintptr_t ULONG_PTR;
typedef struct { USHORT Length, MaximumLength; PWSTR Buffer; } UNICODE_STRING, *PUNICODE_STRING;
typedef const UNICODE_STRING *PCUNICODE_STRING;
typedef union { int64_t QuadPart; } PHYSICAL_ADDRESS;
typedef struct { ULONG Data1; USHORT Data2, Data3; UCHAR Data4[8]; } GUID;
typedef const GUID *LPCGUID;
typedef unsigned KSPIN_LOCK;
typedef struct { bool signaled; } KEVENT, *PKEVENT;

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_UNSUCCESSFUL ((NTSTATUS)0xC0000001)
#define STATUS_NOT_SUPPORTED ((NTSTATUS)0xC00000BB)
#define STATUS_INVALID_PARAMETER ((NTSTATUS)0xC000000D)
#define STATUS_INSUFFICIENT_RESOURCES ((NTSTATUS)0xC000009A)
#define STATUS_DEVICE_BUSY ((NTSTATUS)0x80000011)
#define STATUS_PENDING 0x00000103
#define STATUS_MORE_PROCESSING_REQUIRED ((NTSTATUS)0xC0000016)
#define RTL_NUMBER_OF(a) (sizeof(a) / sizeof((a)[0]))
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define PASSIVE_LEVEL 0
#define DISPATCH_LEVEL 2
#define RTL_REGISTRY_ABSOLUTE 0
#define REG_DWORD 4
#define REG_NONE 0
#define RTL_QUERY_REGISTRY_DIRECT 0x20
#define RTL_QUERY_REGISTRY_TYPECHECK 0x100
#define RTL_QUERY_REGISTRY_TYPECHECK_SHIFT 24
#define PAGE_READWRITE 4
#define PAGE_NOCACHE 0x200
#define DelayedWorkQueue 1
#define NotificationEvent 0
#define Executive 0
#define KernelMode 0
#define IO_NO_INCREMENT 0
#define FILE_DEVICE_UNKNOWN 0x22
#define FILE_DEVICE_SECURE_OPEN 0x100
#define DO_DEVICE_INITIALIZING 0x80
#define DO_POWER_PAGABLE 0x2000
#define IRP_MJ_MAXIMUM_FUNCTION 0x1b
#define IRP_MJ_POWER 0x16
#define IRP_MJ_PNP 0x1b
#define IRP_MN_START_DEVICE 0x00
#define IRP_MN_QUERY_REMOVE_DEVICE 0x01
#define IRP_MN_REMOVE_DEVICE 0x02
#define IRP_MN_QUERY_STOP_DEVICE 0x05
#define IRP_MN_STOP_DEVICE 0x04
#define IRP_MN_QUERY_CAPABILITIES 0x09
#define IRP_MN_SURPRISE_REMOVAL 0x17
#define IRP_MN_WAIT_WAKE 0x00
#define IRP_MN_POWER_SEQUENCE 0x01
#define IRP_MN_SET_POWER 0x02
#define IRP_MN_QUERY_POWER 0x03
#define _ARM64_BARRIER_SY 15

typedef enum {
    PowerDeviceUnspecified = 0,
    PowerDeviceD0 = 1,
    PowerDeviceD1 = 2,
    PowerDeviceD2 = 3,
    PowerDeviceD3 = 4
} DEVICE_POWER_STATE;

typedef enum {
    PowerSystemUnspecified = 0,
    PowerSystemWorking = 1,
    PowerSystemSleeping1 = 2,
    PowerSystemSleeping2 = 3,
    PowerSystemSleeping3 = 4,
    PowerSystemHibernate = 5,
    PowerSystemShutdown = 6
} SYSTEM_POWER_STATE;

typedef enum { SystemPowerState = 0, DevicePowerState = 1 } POWER_STATE_TYPE;
typedef union { SYSTEM_POWER_STATE SystemState; DEVICE_POWER_STATE DeviceState; } POWER_STATE;

typedef enum { PepDeviceNotAccepted = 0, PepDeviceAccepted = 1 } PEP_DEVICE_ACCEPTANCE_TYPE;

#define PEP_INFORMATION_VERSION 3
#define PEP_KERNEL_INFORMATION_V3 3
#define PEP_UNSPECIFIED_MINIMUM_STATE 0xFFFFFFFFu
#define PEP_PLATFORM_IDLE_STATE_NONE 0xFFFFFFFFu
#define PEP_IDLE_VETO_NONE 0u
#define PROCESSOR_HALT_VIA_PSCI_CPU_SUSPEND 0x20u
#define PROCESSOR_HALT_CACHE_FLUSH_OVERRIDE 0x01u

enum {
    PEP_DPM_PREPARE_DEVICE = 1,
    PEP_DPM_ABANDON_DEVICE,
    PEP_DPM_REGISTER_DEVICE,
    PEP_DPM_UNREGISTER_DEVICE,
    PEP_DPM_DEVICE_POWER_STATE,
    PEP_DPM_WORK,
    PEP_DPM_POWER_CONTROL_REQUEST,
    PEP_DPM_POWER_CONTROL_COMPLETE,
    PEP_DPM_SYSTEM_LATENCY_UPDATE,
    PEP_DPM_DEVICE_STARTED,
    PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE,
    PEP_DPM_REGISTER_CRASHDUMP_DEVICE,
    PEP_DPM_DEVICE_IDLE_CONSTRAINTS,
    PEP_DPM_COMPONENT_IDLE_CONSTRAINTS
};

enum {
    PEP_NOTIFY_PPM_QUERY_CAPABILITIES = 1,
    PEP_NOTIFY_PPM_IDLE_EXECUTE,
    PEP_NOTIFY_PPM_IDLE_COMPLETE,
    PEP_NOTIFY_PPM_IS_PROCESSOR_HALTED,
    PEP_NOTIFY_PPM_INITIATE_WAKE,
    PEP_NOTIFY_PPM_QUERY_VETO_REASONS,
    PEP_NOTIFY_PPM_QUERY_VETO_REASON,
    PEP_NOTIFY_PPM_QUERY_PLATFORM_STATES,
    PEP_NOTIFY_PPM_QUERY_IDLE_STATES_V2,
    PEP_NOTIFY_PPM_QUERY_COORDINATED_STATES,
    PEP_NOTIFY_PPM_QUERY_COORDINATED_DEPENDENCY,
    PEP_NOTIFY_PPM_TEST_IDLE_STATE,
    PEP_NOTIFY_PPM_LPI_SUPPORTED,
    PEP_NOTIFY_PPM_ENTER_SYSTEM_STATE,
    PEP_NOTIFY_PPM_RESUME_FROM_SYSTEM_STATE
};

typedef struct { ULONG IdleStateCount; } PEP_COMPONENT_V2, *PPEP_COMPONENT_V2;
typedef struct { ULONG ComponentCount; PPEP_COMPONENT_V2 Components[1]; } PEP_DEVICE_REGISTER_V2, *PPEP_DEVICE_REGISTER_V2;
typedef struct { PCUNICODE_STRING DeviceId; BOOLEAN DeviceAccepted; } PEP_PREPARE_DEVICE, *PPEP_PREPARE_DEVICE, PEP_ABANDON_DEVICE, *PPEP_ABANDON_DEVICE;
typedef struct { PCUNICODE_STRING DeviceId; POHANDLE KernelHandle; PPEP_DEVICE_REGISTER_V2 Register; PEPHANDLE DeviceHandle; PEP_DEVICE_ACCEPTANCE_TYPE DeviceAccepted; } PEP_REGISTER_DEVICE, *PPEP_REGISTER_DEVICE;
typedef struct { PEPHANDLE DeviceHandle; } PEP_UNREGISTER_DEVICE, *PPEP_UNREGISTER_DEVICE, PEP_DEVICE_STARTED, *PPEP_DEVICE_STARTED;
typedef struct { PEPHANDLE DeviceHandle; DEVICE_POWER_STATE PowerState; BOOLEAN Complete; } PEP_DEVICE_POWER_STATE, *PPEP_DEVICE_POWER_STATE;
typedef struct { PEPHANDLE DeviceHandle; ULONG Component; ULONG IdleState; BOOLEAN DriverNotified; BOOLEAN Completed; } PEP_NOTIFY_COMPONENT_IDLE_STATE, *PPEP_NOTIFY_COMPONENT_IDLE_STATE;
typedef struct { PEPHANDLE DeviceHandle; ULONG PlatformStateCount; DEVICE_POWER_STATE *MinimumDStates; } PEP_DEVICE_PLATFORM_CONSTRAINTS, *PPEP_DEVICE_PLATFORM_CONSTRAINTS;
typedef struct { PEPHANDLE DeviceHandle; ULONG Component; ULONG PlatformStateCount; ULONG *MinimumFStates; } PEP_COMPONENT_PLATFORM_CONSTRAINTS, *PPEP_COMPONENT_PLATFORM_CONSTRAINTS;
typedef struct { BOOLEAN NeedWork; } PEP_WORK, *PPEP_WORK;
typedef struct { ULONG BytesReturned; NTSTATUS Status; } PEP_POWER_CONTROL_REQUEST, *PPEP_POWER_CONTROL_REQUEST;
typedef struct { PEPHANDLE DeviceHandle; PVOID DeviceContext; } PEP_CRASHDUMP_INFORMATION, *PPEP_CRASHDUMP_INFORMATION;
typedef BOOLEAN PEPCALLBACKPOWERONCRASHDUMPDEVICE(PPEP_CRASHDUMP_INFORMATION);
typedef PEPCALLBACKPOWERONCRASHDUMPDEVICE *PPEPCALLBACKPOWERONCRASHDUMPDEVICE;
typedef struct { PEPHANDLE DeviceHandle; PPEPCALLBACKPOWERONCRASHDUMPDEVICE PowerOnDumpDeviceCallback; } PEP_REGISTER_CRASHDUMP_DEVICE, *PPEP_REGISTER_CRASHDUMP_DEVICE;

typedef struct { ULONG FeedbackCounterCount, IdleStateCount, DiscretePerformanceStateCount; BOOLEAN PerformanceStatesSupported, ParkingSupported; } PEP_PPM_QUERY_CAPABILITIES, *PPEP_PPM_QUERY_CAPABILITIES;
typedef struct { BOOLEAN Interruptible, WakesSpuriously, CacheCoherent, ThreadContextRetained, PlatformOnly; ULONG Latency, BreakEvenDuration; } PEP_PROCESSOR_IDLE_STATE_V2, *PPEP_PROCESSOR_IDLE_STATE_V2;
typedef struct { ULONG Count; PEP_PROCESSOR_IDLE_STATE_V2 IdleStates[1]; } PEP_PPM_QUERY_IDLE_STATES_V2, *PPEP_PPM_QUERY_IDLE_STATES_V2;
typedef struct { ULONG PlatformStateCount; } PEP_PPM_QUERY_PLATFORM_STATES, *PPEP_PPM_QUERY_PLATFORM_STATES;
typedef struct { ULONG Latency, BreakEvenDuration, DependencyCount, MaximumDependencySize; } PEP_COORDINATED_IDLE_STATE, *PPEP_COORDINATED_IDLE_STATE;
typedef struct { ULONG Count; PEP_COORDINATED_IDLE_STATE States[1]; } PEP_PPM_QUERY_COORDINATED_STATES, *PPEP_PPM_QUERY_COORDINATED_STATES;
typedef struct { ULONG ExpectedStateIndex; BOOLEAN LooseDependency, InitiatingState, DependentState; } PEP_COORDINATED_DEPENDENCY_OPTION, *PPEP_COORDINATED_DEPENDENCY_OPTION;
typedef struct { ULONG StateIndex, DependencyIndex, DependencySize, DependencySizeUsed; POHANDLE TargetProcessor; PEP_COORDINATED_DEPENDENCY_OPTION Options[1]; } PEP_PPM_QUERY_COORDINATED_DEPENDENCY, *PPEP_PPM_QUERY_COORDINATED_DEPENDENCY;
typedef struct { ULONG ProcessorState, PlatformState, VetoReason; } PEP_PPM_TEST_IDLE_STATE, *PPEP_PPM_TEST_IDLE_STATE;
typedef struct { ULONG ProcessorState, PlatformState; NTSTATUS Status; } PEP_PPM_IDLE_EXECUTE_V2, *PPEP_PPM_IDLE_EXECUTE_V2;
typedef struct { BOOLEAN OverrideLpiStates; } PEP_PPM_LPI_SUPPORTED, *PPEP_PPM_LPI_SUPPORTED;
typedef struct { BOOLEAN Halted; } PEP_PPM_IS_PROCESSOR_HALTED, *PPEP_PPM_IS_PROCESSOR_HALTED;
typedef struct { BOOLEAN NeedInterruptForCompletion; } PEP_PPM_INITIATE_WAKE, *PPEP_PPM_INITIATE_WAKE;
typedef struct { ULONG VetoReasonCount; } PEP_PPM_QUERY_VETO_REASONS, *PPEP_PPM_QUERY_VETO_REASONS;
typedef struct { ULONG VetoReason; USHORT NameSize; PWSTR Name; } PEP_PPM_QUERY_VETO_REASON, *PPEP_PPM_QUERY_VETO_REASON;
typedef struct { SYSTEM_POWER_STATE TargetState; } PEP_PPM_ENTER_SYSTEM_STATE, *PPEP_PPM_ENTER_SYSTEM_STATE, PEP_PPM_RESUME_FROM_SYSTEM_STATE, *PPEP_PPM_RESUME_FROM_SYSTEM_STATE;

typedef BOOLEAN (*PEPCALLBACKNOTIFYDPM)(ULONG, PVOID);
typedef BOOLEAN (*PEPCALLBACKNOTIFYPPMS)(PEPHANDLE, ULONG, PVOID);
typedef struct { ULONG Version, Size; PEPCALLBACKNOTIFYDPM AcceptDeviceNotification; PEPCALLBACKNOTIFYPPMS AcceptProcessorNotification; } PEP_INFORMATION, *PPEP_INFORMATION;
typedef NTSTATUS (*PPROCESSOR_HALT_ROUTINE)(ULONG Flags, PVOID Parameter, PVOID HaltFn);
typedef struct { ULONG Version, Size; PPROCESSOR_HALT_ROUTINE ProcessorHalt; } PEP_KERNEL_INFORMATION, *PPEP_KERNEL_INFORMATION;

typedef struct { ULONG Flags; PWSTR Name; PVOID EntryContext; ULONG DefaultType; } RTL_QUERY_REGISTRY_TABLE;
typedef struct { BOOLEAN EjectSupported, Removable, SilentInstall; } DEVICE_CAPABILITIES, *PDEVICE_CAPABILITIES;
typedef struct {
    UCHAR MinorFunction;
    union {
        struct { PDEVICE_CAPABILITIES Capabilities; } DeviceCapabilities;
        struct { ULONG SystemContext; POWER_STATE_TYPE Type; POWER_STATE State; } Power;
    } Parameters;
} IO_STACK_LOCATION, *PIO_STACK_LOCATION;
typedef struct _IRP { struct { NTSTATUS Status; } IoStatus; BOOLEAN PendingReturned; IO_STACK_LOCATION Stack; } IRP, *PIRP;
struct _DEVICE_OBJECT { PVOID DeviceExtension; ULONG Flags; };
struct _DRIVER_EXTENSION { NTSTATUS (*AddDevice)(PDRIVER_OBJECT, PDEVICE_OBJECT); };
struct _DRIVER_OBJECT { NTSTATUS (*MajorFunction[28])(PDEVICE_OBJECT, PIRP); struct _DRIVER_EXTENSION *DriverExtension; };

typedef NTSTATUS DRIVER_INITIALIZE(PDRIVER_OBJECT, PUNICODE_STRING);
typedef NTSTATUS DRIVER_ADD_DEVICE(PDRIVER_OBJECT, PDEVICE_OBJECT);
typedef NTSTATUS DRIVER_DISPATCH(PDEVICE_OBJECT, PIRP);
typedef VOID IO_WORKITEM_ROUTINE(PDEVICE_OBJECT, PVOID);

static ULONG hidrev_reg = 0x00002120u;
static ULONG car_words[64];
static ULONG reg_enable_sc7 = 0;
static KIRQL cur_irql = PASSIVE_LEVEL;
static PEP_INFORMATION registered_pep;
static ULONG last_psci_flags = 0, last_psci_state = 0;
static unsigned psci_calls = 0, wfi_calls = 0;
static NTSTATUS next_psci_status = STATUS_SUCCESS;
static void (*psci_mid_hook)(void) = NULL;

static void __dmb(int b) { (void)b; }
static void __wfi(void) { wfi_calls++; }
static KIRQL KeGetCurrentIrql(void) { return cur_irql; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }
static LONG InterlockedCompareExchange(volatile LONG *p, LONG v, LONG c) { LONG o = *p; if (o == c) *p = v; return o; }
static void RtlZeroMemory(void *p, size_t n) { memset(p, 0, n); }
static void RtlCopyMemory(void *d, const void *s, size_t n) { memcpy(d, s, n); }
static void RtlInitUnicodeString(PUNICODE_STRING u, PCWSTR s) {
    u->Buffer = (PWSTR)s;
    u->Length = (USHORT)(wcslen(s) * sizeof(WCHAR));
    u->MaximumLength = u->Length + sizeof(WCHAR);
}
static BOOLEAN RtlEqualUnicodeString(PCUNICODE_STRING a, PCUNICODE_STRING b, BOOLEAN ci) {
    (void)ci;
    return a->Length == b->Length && wcsncasecmp(a->Buffer, b->Buffer, a->Length / sizeof(WCHAR)) == 0;
}
static BOOLEAN IsEqualGUID(LPCGUID a, LPCGUID b) { return memcmp(a, b, sizeof(GUID)) == 0; }
static NTSTATUS RtlCreateRegistryKey(ULONG rel, PWSTR path) { (void)rel; (void)path; assert(cur_irql == PASSIVE_LEVEL); return 0; }
static NTSTATUS RtlWriteRegistryValue(ULONG rel, PCWSTR path, PCWSTR name, ULONG type, PVOID data, ULONG len) {
    (void)rel; (void)path; (void)name; (void)type; (void)data; (void)len;
    assert(cur_irql == PASSIVE_LEVEL);
    return 0;
}
static NTSTATUS RtlQueryRegistryValues(ULONG rel, PCWSTR path, RTL_QUERY_REGISTRY_TABLE *t, PVOID c, PVOID e) {
    (void)rel; (void)path; (void)c; (void)e;
    assert(cur_irql == PASSIVE_LEVEL);
    *(ULONG *)t[0].EntryContext = reg_enable_sc7;
    return 0;
}
static PVOID MmMapIoSpaceEx(PHYSICAL_ADDRESS pa, size_t len, ULONG prot) {
    (void)len; (void)prot;
    if (pa.QuadPart == 0x70000804) return &hidrev_reg;
    if (pa.QuadPart == 0x60006000) return car_words;
    return NULL;
}
static void MmUnmapIoSpace(PVOID p, size_t len) { (void)p; (void)len; }
static void (*car_read_hook)(void) = NULL;
static ULONG read_reg_ulong(PULONG p) {
    ULONG v = *p;
    if (car_read_hook && p == &car_words[0xe0 / 4]) {
        void (*hook)(void) = car_read_hook;
        car_read_hook = NULL;
        hook();
    }
    return v;
}
#define READ_REGISTER_ULONG(p) read_reg_ulong(p)

static NTSTATUS MockProcessorHalt(ULONG flags, PVOID param, PVOID fn) {
    assert(fn == NULL && param != NULL);
    if (psci_mid_hook) psci_mid_hook();
    last_psci_flags = flags;
    last_psci_state = *(ULONG *)param;
    psci_calls++;
    return next_psci_status;
}
static NTSTATUS PoFxRegisterPlugin(PPEP_INFORMATION info, PPEP_KERNEL_INFORMATION kern) {
    registered_pep = *info;
    kern->ProcessorHalt = MockProcessorHalt;
    return STATUS_SUCCESS;
}

/* Unused PnP stubs for processor test */
static void KeInitializeSpinLock(KSPIN_LOCK *l) { *l = 0; }
static void KeAcquireSpinLock(KSPIN_LOCK *l, KIRQL *i) { assert(*l == 0); *l = 1; *i = cur_irql; cur_irql = DISPATCH_LEVEL; }
static void KeReleaseSpinLock(KSPIN_LOCK *l, KIRQL i) { assert(*l == 1); *l = 0; cur_irql = i; }
static void KeInitializeEvent(PKEVENT e, int t, BOOLEAN s) { (void)t; e->signaled = s; }
static void KeClearEvent(PKEVENT e) { e->signaled = false; }
static void KeSetEvent(PKEVENT e, int inc, BOOLEAN w) { (void)inc; (void)w; e->signaled = true; }
static NTSTATUS KeWaitForSingleObject(PVOID o, int r, int m, BOOLEAN a, PVOID t) { (void)o; (void)r; (void)m; (void)a; (void)t; return 0; }
static void IoQueueWorkItem(PIO_WORKITEM w, IO_WORKITEM_ROUTINE *r, int q, PVOID c) { (void)w; (void)r; (void)q; (void)c; }
static PIO_WORKITEM IoAllocateWorkItem(PDEVICE_OBJECT o) { return (PIO_WORKITEM)o; }
static void IoFreeWorkItem(PIO_WORKITEM w) { (void)w; }
static NTSTATUS PoRegisterPowerSettingCallback(PDEVICE_OBJECT o, LPCGUID g, PVOID cb, PVOID ctx, PVOID *h) { (void)o; (void)g; (void)cb; *h = ctx; return 0; }
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID h) { (void)h; return 0; }
static void IoCompleteRequest(PIRP i, int p) { (void)i; (void)p; }
static void IoCopyCurrentIrpStackLocationToNext(PIRP i) { (void)i; }
static void IoSkipCurrentIrpStackLocation(PIRP i) { (void)i; }
static PIO_STACK_LOCATION IoGetCurrentIrpStackLocation(PIRP i) { return &i->Stack; }
static void IoSetCompletionRoutine(PIRP i, PVOID r, PVOID c, BOOLEAN s, BOOLEAN e, BOOLEAN cn) { (void)i; (void)r; (void)c; (void)s; (void)e; (void)cn; }
static void IoMarkIrpPending(PIRP i) { (void)i; }
static void PoStartNextPowerIrp(PIRP i) { (void)i; }
static NTSTATUS IoCallDriver(PDEVICE_OBJECT o, PIRP i) { (void)o; return i->IoStatus.Status; }
static NTSTATUS PoCallDriver(PDEVICE_OBJECT o, PIRP i) { (void)o; return i->IoStatus.Status; }
static void IoDetachDevice(PDEVICE_OBJECT o) { (void)o; }
static void IoDeleteDevice(PDEVICE_OBJECT o) { (void)o; }
static NTSTATUS IoCreateDevice(PDRIVER_OBJECT d, ULONG ext, PUNICODE_STRING n, ULONG t, ULONG ch, BOOLEAN ex, PDEVICE_OBJECT *out) { (void)d; (void)ext; (void)n; (void)t; (void)ch; (void)ex; *out = NULL; return STATUS_UNSUCCESSFUL; }
static NTSTATUS IoAttachDeviceToDeviceStackSafe(PDEVICE_OBJECT s, PDEVICE_OBJECT t, PDEVICE_OBJECT *l) { (void)s; (void)t; *l = NULL; return 0; }
'''

tests = r'''
static PEPHANDLE reg_dev(PCWSTR path, POHANDLE kh, ULONG comp_count, const ULONG *idle_counts) {
    UNICODE_STRING id;
    RtlInitUnicodeString(&id, path);
    PEP_PREPARE_DEVICE prep = { &id, FALSE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_PREPARE_DEVICE, &prep) && prep.DeviceAccepted);

    PEP_COMPONENT_V2 comps_storage[9] = {0};
    struct { ULONG ComponentCount; PPEP_COMPONENT_V2 Components[9]; } reg_storage = { comp_count, {0} };
    for (ULONG i = 0; i < comp_count; ++i) {
        comps_storage[i].IdleStateCount = idle_counts[i];
        reg_storage.Components[i] = &comps_storage[i];
    }
    PEP_REGISTER_DEVICE reg = {
        &id, kh, comp_count ? (PPEP_DEVICE_REGISTER_V2)&reg_storage : NULL, NULL, PepDeviceNotAccepted
    };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_REGISTER_DEVICE, &reg));
    assert(reg.DeviceAccepted == PepDeviceAccepted && reg.DeviceHandle != NULL);
    return reg.DeviceHandle;
}

static void set_dstate(PEPHANDLE h, DEVICE_POWER_STATE s, BOOLEAN complete) {
    PEP_DEVICE_POWER_STATE p = { h, s, complete };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_DEVICE_POWER_STATE, &p));
}

static PEPHANDLE hook_dev = NULL;
static void hook_display_on_mid_car(void) {
    ULONG on = 1;
    DisplayChanged(&DisplayGuid, &on, 4, NULL);
}
static void hook_dev_wake_mid_car(void) {
    set_dstate(hook_dev, PowerDeviceD0, FALSE);
}
static void hook_sys_enter_mid_car(void) {
    PEP_PPM_ENTER_SYSTEM_STATE s = { PowerSystemHibernate };
    assert(registered_pep.AcceptProcessorNotification(NULL, PEP_NOTIFY_PPM_ENTER_SYSTEM_STATE, &s));
}

int main(void) {
    struct _DRIVER_EXTENSION ext = {0};
    struct _DRIVER_OBJECT drv = { .DriverExtension = &ext };
    UNICODE_STRING reg_path;
    RtlInitUnicodeString(&reg_path, L"\\Registry\\Machine\\System\\CurrentControlSet\\Services\\tegra214pep");

    /* 1. Reject Erista (0x2100); accept T210B01 Mariko (0x2120). */
    hidrev_reg = 0x00002100u;
    assert(DriverEntry(&drv, &reg_path) == STATUS_NOT_SUPPORTED);
    hidrev_reg = 0x00002120u;
    reg_enable_sc7 = 0;
    assert(DriverEntry(&drv, &reg_path) == STATUS_SUCCESS);

    /* 2. Enumerate 4 CPUs + verify 4 processor states & 2 coordinated states + deepest-first dependency options! */
    PEPHANDLE cpu_h[4];
    for (unsigned i = 0; i < 4; ++i) {
        cpu_h[i] = reg_dev(CpuPaths[i], (POHANDLE)(uintptr_t)(0x100 + i), 0, NULL);
        PEP_PPM_QUERY_CAPABILITIES cap = {0};
        assert(registered_pep.AcceptProcessorNotification(cpu_h[i], PEP_NOTIFY_PPM_QUERY_CAPABILITIES, &cap));
        assert(cap.IdleStateCount == 4 && !cap.PerformanceStatesSupported);
    }
    struct { ULONG Count; PEP_PROCESSOR_IDLE_STATE_V2 States[4]; } idle_q = { 4, {{0}} };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_IDLE_STATES_V2, &idle_q));
    assert(idle_q.States[IDLE_WFI].CacheCoherent && idle_q.States[IDLE_WFI].ThreadContextRetained);
    assert(!idle_q.States[IDLE_C7].ThreadContextRetained && !idle_q.States[IDLE_C7].PlatformOnly);
    assert(idle_q.States[IDLE_CC6].PlatformOnly && idle_q.States[IDLE_SC7].PlatformOnly);

    PEP_PPM_QUERY_PLATFORM_STATES plat_q = {0};
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_PLATFORM_STATES, &plat_q));
    assert(plat_q.PlatformStateCount == 2);

    struct { ULONG Count; PEP_COORDINATED_IDLE_STATE States[2]; } coord_q = { 2, {{0}} };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_COORDINATED_STATES, &coord_q));
    assert(coord_q.States[PLAT_STATE_CC6].DependencyCount == 4 && coord_q.States[PLAT_STATE_CC6].MaximumDependencySize == 2);
    assert(coord_q.States[PLAT_STATE_SC7].DependencyCount == 4 && coord_q.States[PLAT_STATE_SC7].MaximumDependencySize == 3);

    struct { ULONG StateIndex, DependencyIndex, DependencySize, DependencySizeUsed; POHANDLE Target; PEP_COORDINATED_DEPENDENCY_OPTION Opts[3]; } dep_q = {
        PLAT_STATE_SC7, 2, 3, 0, NULL, {{0}}
    };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_COORDINATED_DEPENDENCY, &dep_q));
    assert(dep_q.DependencySizeUsed == 3 && dep_q.Target == (POHANDLE)(uintptr_t)0x102);
    assert(dep_q.Opts[0].ExpectedStateIndex == IDLE_SC7 && dep_q.Opts[0].InitiatingState);
    assert(dep_q.Opts[1].ExpectedStateIndex == IDLE_CC6 && !dep_q.Opts[1].InitiatingState && dep_q.Opts[1].DependentState);
    assert(dep_q.Opts[2].ExpectedStateIndex == IDLE_C7  && !dep_q.Opts[2].InitiatingState && dep_q.Opts[2].DependentState);

    /* 3. Verify CAR PLLX Pdiv[] clock & display gates:
     *    1497.6 MHz (n=234, m=2, p=2 => Pdiv=3) vetoed; 998.4 MHz (n=156, m=2, p=2) allowed when display off! */
    car_words[0x20 / 4] = 0x20008888u;
    car_words[0xe0 / 4] = 0x48000000u | (2u << 20) | (234u << 8) | 2u; /* 1497.6 MHz */
    ULONG disp = 0;
    DisplayChanged(&DisplayGuid, &disp, 4, NULL);
    PEP_PPM_TEST_IDLE_STATE test_s = { IDLE_C7, PEP_PLATFORM_IDLE_STATE_NONE, 0 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_DISPLAY_OR_CLOCK);

    car_words[0xe0 / 4] = 0x48000000u | (2u << 20) | (156u << 8) | 2u; /* 998.4 MHz */
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == PEP_IDLE_VETO_NONE);

    /* 4. Execute C7 (0x40000007) and CC6 (0x40000010); verify EnableSc7=0 vetoes SC7! */
    PEP_PPM_IDLE_EXECUTE_V2 exec = { IDLE_C7, PEP_PLATFORM_IDLE_STATE_NONE, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000007u);
    assert(last_psci_flags == (PROCESSOR_HALT_VIA_PSCI_CPU_SUSPEND | PROCESSOR_HALT_CACHE_FLUSH_OVERRIDE));

    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_CC6, PLAT_STATE_CC6, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000010u && Cc6Attempts == 1);

    test_s = (PEP_PPM_TEST_IDLE_STATE){ IDLE_SC7, PLAT_STATE_SC7, 0 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SC7_DISABLED);

    /* 5. Enable SC7 -> vetoed by VETO_SC7_DEVICES_BUSY until all 5 required + registered optional devices reach D3 + deepest F-state! */
    reg_enable_sc7 = 1;
    RefreshEnableSc7FromRegistry();
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SC7_DEVICES_BUSY);
    assert(ComputeSc7BlockingMask() == 0x1Fu); /* SDM1, PWRB, I2C1, I2C3, I2C5 required */

    PEPHANDLE sdm1 = reg_dev(L"\\_SB.SDM1", (POHANDLE)1, 0, NULL);
    PEPHANDLE pwrb = reg_dev(L"\\_SB.PWRB", (POHANDLE)2, 0, NULL);
    PEPHANDLE i2c1 = reg_dev(L"\\_SB.I2C1", (POHANDLE)3, 0, NULL);
    PEPHANDLE i2c3 = reg_dev(L"\\_SB.I2C3", (POHANDLE)4, 0, NULL);
    PEPHANDLE i2c5 = reg_dev(L"\\_SB.I2C5", (POHANDLE)5, 0, NULL);
    const ULONG gpu_fcounts[2] = { 1, 3 }; /* component 1 has F0..F2 */
    PEPHANDLE gpu0 = reg_dev(L"\\_SB.GPU0", (POHANDLE)6, 2, gpu_fcounts);

    /* Check platform D-state and F-state constraints reported to PoFx! */
    DEVICE_POWER_STATE min_d[2] = {0};
    PEP_DEVICE_PLATFORM_CONSTRAINTS d_con = { gpu0, 2, min_d };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_DEVICE_IDLE_CONSTRAINTS, &d_con));
    assert(min_d[PLAT_STATE_CC6] == PowerDeviceUnspecified && min_d[PLAT_STATE_SC7] == PowerDeviceD3);

    ULONG min_f[2] = {0};
    PEP_COMPONENT_PLATFORM_CONSTRAINTS f_con = { gpu0, 1, 2, min_f };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_COMPONENT_IDLE_CONSTRAINTS, &f_con));
    assert(min_f[PLAT_STATE_CC6] == PEP_UNSPECIFIED_MINIMUM_STATE && min_f[PLAT_STATE_SC7] == 2u);

    /* Crashdump callback registration succeeds for SDM1. */
    PEP_REGISTER_CRASHDUMP_DEVICE cd = { sdm1, NULL };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_REGISTER_CRASHDUMP_DEVICE, &cd) && cd.PowerOnDumpDeviceCallback != NULL);
    PEP_CRASHDUMP_INFORMATION cd_info = { sdm1, NULL };
    assert(cd.PowerOnDumpDeviceCallback(&cd_info) == FALSE); /* registration is not D0 completion */
    set_dstate(sdm1, PowerDeviceD0, FALSE);
    assert(cd.PowerOnDumpDeviceCallback(&cd_info) == FALSE);
    set_dstate(sdm1, PowerDeviceD0, TRUE);
    assert(cd.PowerOnDumpDeviceCallback(&cd_info) == TRUE);
    set_dstate(sdm1, PowerDeviceD3, FALSE);
    assert(cd.PowerOnDumpDeviceCallback(&cd_info) == FALSE);
    PEP_CRASHDUMP_INFORMATION unknown_dump = { NULL, NULL };
    assert(cd.PowerOnDumpDeviceCallback(&unknown_dump) == FALSE);
    PEP_CRASHDUMP_INFORMATION component_dump = { gpu0, NULL };
    set_dstate(gpu0, PowerDeviceD0, TRUE);
    assert(cd.PowerOnDumpDeviceCallback(&component_dump) == FALSE);

    /* Transition all 5 required + GPU0 to D3 (in-progress Complete=FALSE must NOT clear blocking!). */
    set_dstate(sdm1, PowerDeviceD3, TRUE);
    assert(cd.PowerOnDumpDeviceCallback(&cd_info) == FALSE);
    set_dstate(pwrb, PowerDeviceD3, TRUE);
    set_dstate(i2c1, PowerDeviceD3, TRUE);
    set_dstate(i2c3, PowerDeviceD3, TRUE);
    set_dstate(i2c5, PowerDeviceD3, TRUE);
    set_dstate(gpu0, PowerDeviceD3, FALSE);
    assert(ComputeSc7BlockingMask() == (1u << 5));
    set_dstate(gpu0, PowerDeviceD3, TRUE);
    /* Still blocked because GPU0 component 1 is in F0 (< F2)! */
    assert(ComputeSc7BlockingMask() == (1u << 5));

    PEP_NOTIFY_COMPONENT_IDLE_STATE comp_idle = { gpu0, 1, 2, TRUE, FALSE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE, &comp_idle) && comp_idle.Completed);
    assert(ComputeSc7BlockingMask() == 0u);

    /* A ninth component cannot be silently discarded when admitting SC7. */
    ULONG many_idle_counts[9] = {1,1,1,1,1,1,1,1,2};
    PEPHANDLE tch0 = reg_dev(L"\\_SB.TCH0", (POHANDLE)0x9000, 9, many_idle_counts);
    set_dstate(tch0, PowerDeviceD3, TRUE);
    assert(ComputeSc7BlockingMask() == (1u << 6));
    PEP_UNREGISTER_DEVICE unreg_tch0 = { tch0 };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_UNREGISTER_DEVICE, &unreg_tch0));
    assert(ComputeSc7BlockingMask() == 0u);

    /* 6. Repeated SC7 cycles emit extended PSCI ID 0x4000001B! */
    for (unsigned cycle = 0; cycle < 3; ++cycle) {
        assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
        assert(test_s.VetoReason == PEP_IDLE_VETO_NONE);
        exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
        assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
        assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x4000001Bu);
        assert(Sc7Attempts == (LONG)(cycle + 1) && Sc7Returns == (LONG)(cycle + 1) && Sc7Failures == 0);
    }

    /* 7. Mid-transition peripheral wakeup (D0 start with Complete=FALSE) downgrades SC7 -> CC6 (0x40000010)! */
    set_dstate(pwrb, PowerDeviceD0, FALSE);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000010u && Sc7Downgrades == 1);

    /* 8. PSCI error return (-1 / STATUS_NOT_SUPPORTED) increments Sc7Failures and records LastSc7Status! */
    set_dstate(pwrb, PowerDeviceD3, TRUE);
    next_psci_status = STATUS_NOT_SUPPORTED;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_NOT_SUPPORTED && Sc7Failures == 1 && LastSc7Status == STATUS_NOT_SUPPORTED);

    /* 9. Busy-to-idle transitions: D1/incomplete D3, shallow F2->F1 wake, pre/post F2, optional peripheral + dump invariants. */
    next_psci_status = STATUS_SUCCESS;
    set_dstate(pwrb, PowerDeviceD1, FALSE);
    assert(ComputeSc7BlockingMask() == (1u << 1));
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SC7_DEVICES_BUSY);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000010u && Sc7Downgrades == 2);

    set_dstate(pwrb, PowerDeviceD3, FALSE);
    assert(ComputeSc7BlockingMask() == (1u << 1));
    set_dstate(pwrb, PowerDeviceD3, TRUE);
    assert(ComputeSc7BlockingMask() == 0u);
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == PEP_IDLE_VETO_NONE);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x4000001Bu);

    /* Component shallow wake F2->F1 with DriverNotified=FALSE blocks immediately; F1->F2 needs DriverNotified=TRUE. */
    comp_idle = (PEP_NOTIFY_COMPONENT_IDLE_STATE){ gpu0, 1, 1, FALSE, FALSE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE, &comp_idle) && comp_idle.Completed);
    assert(ComputeSc7BlockingMask() == (1u << 5));
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SC7_DEVICES_BUSY);
    comp_idle = (PEP_NOTIFY_COMPONENT_IDLE_STATE){ gpu0, 1, 2, FALSE, FALSE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE, &comp_idle) && comp_idle.Completed);
    assert(ComputeSc7BlockingMask() == (1u << 5));
    comp_idle = (PEP_NOTIFY_COMPONENT_IDLE_STATE){ gpu0, 1, 2, TRUE, FALSE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE, &comp_idle) && comp_idle.Completed);
    assert(ComputeSc7BlockingMask() == 0u);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x4000001Bu);

    /* Optional peripheral busy -> idle -> active -> unregistered + crashdump safety checks. */
    PEPHANDLE bat0 = reg_dev(L"\\_SB.BAT0", (POHANDLE)0x8000, 0, NULL);
    assert(ComputeSc7BlockingMask() == (1u << 8));
    set_dstate(bat0, PowerDeviceD3, FALSE);
    assert(ComputeSc7BlockingMask() == (1u << 8));
    set_dstate(bat0, PowerDeviceD3, TRUE);
    assert(ComputeSc7BlockingMask() == 0u);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x4000001Bu);
    set_dstate(bat0, PowerDeviceD0, TRUE);
    assert(ComputeSc7BlockingMask() == (1u << 8));
    PEP_CRASHDUMP_INFORMATION bat_dump = { bat0, NULL };
    assert(PowerOnCrashdumpDevice(&bat_dump) == TRUE);
    assert(PowerOnCrashdumpDevice(NULL) == FALSE);
    PEP_UNREGISTER_DEVICE unreg_bat0 = { bat0 };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_UNREGISTER_DEVICE, &unreg_bat0));
    assert(ComputeSc7BlockingMask() == 0u);
    assert(PowerOnCrashdumpDevice(&bat_dump) == FALSE);
    PEP_REGISTER_CRASHDUMP_DEVICE unreg_cd = { bat0, NULL };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_REGISTER_CRASHDUMP_DEVICE, &unreg_cd) == FALSE);
    PEP_DEVICE_POWER_STATE stale_p = { bat0, PowerDeviceD0, TRUE };
    assert(registered_pep.AcceptDeviceNotification(PEP_DPM_DEVICE_POWER_STATE, &stale_p) == FALSE);
    assert(PowerOnCrashdumpDevice(&bat_dump) == FALSE);

    /* 10. System transition vetoes (ENTER_SYSTEM_STATE/RESUME_FROM_SYSTEM_STATE) block C7/CC6/SC7 while allowing WFI. */
    PEP_PPM_QUERY_VETO_REASONS veto_cnt = {0};
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_VETO_REASONS, &veto_cnt));
    assert(veto_cnt.VetoReasonCount == VETO_COUNT);
    WCHAR veto_buf[64] = {0};
    PEP_PPM_QUERY_VETO_REASON veto_q = { VETO_SYSTEM_TRANSITION, 0, NULL };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_VETO_REASON, &veto_q));
    assert((size_t)veto_q.NameSize > sizeof(WCHAR) && (size_t)veto_q.NameSize <= sizeof(veto_buf));
    veto_q.Name = veto_buf;
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_QUERY_VETO_REASON, &veto_q));
    assert(veto_buf[0] != 0);

    PEP_PPM_ENTER_SYSTEM_STATE enter_sys = { PowerSystemShutdown };
    assert(registered_pep.AcceptProcessorNotification(NULL, PEP_NOTIFY_PPM_ENTER_SYSTEM_STATE, &enter_sys));
    PEP_PPM_TEST_IDLE_STATE wfi_s = { IDLE_WFI, PEP_PLATFORM_IDLE_STATE_NONE, 99 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &wfi_s));
    assert(wfi_s.VetoReason == PEP_IDLE_VETO_NONE);
    PEP_PPM_TEST_IDLE_STATE c7_s = { IDLE_C7, PEP_PLATFORM_IDLE_STATE_NONE, 0 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &c7_s));
    assert(c7_s.VetoReason == VETO_SYSTEM_TRANSITION);
    PEP_PPM_TEST_IDLE_STATE cc6_s = { IDLE_CC6, PLAT_STATE_CC6, 0 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &cc6_s));
    assert(cc6_s.VetoReason == VETO_SYSTEM_TRANSITION);
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SYSTEM_TRANSITION);

    unsigned psci_before = psci_calls, wfi_before = wfi_calls;
    LONG sc7_att_before = Sc7Attempts, cc6_att_before = Cc6Attempts, sc7_dg_before = Sc7Downgrades;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_C7, PEP_PLATFORM_IDLE_STATE_NONE, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec) && exec.Status == STATUS_SUCCESS);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_CC6, PLAT_STATE_CC6, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec) && exec.Status == STATUS_SUCCESS);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec) && exec.Status == STATUS_SUCCESS);
    assert(psci_calls == psci_before && wfi_calls == wfi_before + 3);
    assert(Sc7Attempts == sc7_att_before && Cc6Attempts == cc6_att_before && Sc7Downgrades == sc7_dg_before);

    PEP_PPM_RESUME_FROM_SYSTEM_STATE resume_sys = { PowerSystemWorking };
    assert(registered_pep.AcceptProcessorNotification(NULL, PEP_NOTIFY_PPM_RESUME_FROM_SYSTEM_STATE, &resume_sys));
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == PEP_IDLE_VETO_NONE);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x4000001Bu);

    /* 11. Final SC7 admission re-samples coherent state after CAR MMIO read (display wake, peripheral wake, system transition, opt-out). */
    psci_before = psci_calls; wfi_before = wfi_calls;
    car_read_hook = hook_display_on_mid_car;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec) && exec.Status == STATUS_SUCCESS);
    assert(car_read_hook == NULL && psci_calls == psci_before && wfi_calls == wfi_before + 1);
    disp = 0;
    DisplayChanged(&DisplayGuid, &disp, 4, NULL);

    hook_dev = i2c5;
    sc7_dg_before = Sc7Downgrades;
    car_read_hook = hook_dev_wake_mid_car;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(car_read_hook == NULL && exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000010u && Sc7Downgrades == sc7_dg_before + 1);
    set_dstate(i2c5, PowerDeviceD3, TRUE);

    psci_before = psci_calls; wfi_before = wfi_calls;
    car_read_hook = hook_sys_enter_mid_car;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec) && exec.Status == STATUS_SUCCESS);
    assert(car_read_hook == NULL && psci_calls == psci_before && wfi_calls == wfi_before + 1);
    assert(registered_pep.AcceptProcessorNotification(NULL, PEP_NOTIFY_PPM_RESUME_FROM_SYSTEM_STATE, &resume_sys));

    /* Preserved opt-in guarantee: EnableSc7=0 vetoes/downgrades SC7 while keeping C7/CC6 functional. */
    reg_enable_sc7 = 0;
    RefreshEnableSc7FromRegistry();
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_TEST_IDLE_STATE, &test_s));
    assert(test_s.VetoReason == VETO_SC7_DISABLED);
    sc7_dg_before = Sc7Downgrades;
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_SC7, PLAT_STATE_SC7, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000010u && Sc7Downgrades == sc7_dg_before + 1);
    exec = (PEP_PPM_IDLE_EXECUTE_V2){ IDLE_C7, PEP_PLATFORM_IDLE_STATE_NONE, -1 };
    assert(registered_pep.AcceptProcessorNotification(cpu_h[0], PEP_NOTIFY_PPM_IDLE_EXECUTE, &exec));
    assert(exec.Status == STATUS_SUCCESS && last_psci_state == 0x40000007u);

    puts("PASS: Tegra214Power extended PSCI IDs (C7/CC6/SC7), CAR PLLX Pdiv gate, D3/F-state busy-to-idle tracking, system transition vetoes, coherent final admission and PSCI fault accounting");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="tegra214pep-proc-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(header + body + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
