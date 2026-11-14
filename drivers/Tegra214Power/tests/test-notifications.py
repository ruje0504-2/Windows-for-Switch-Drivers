#!/usr/bin/env python3
"""Exercise Tegra214Power PnP/workitem lifecycle, EnableSc7-before-DisplayOff ordering and stop/remove drain."""
from pathlib import Path
import os
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra214pep.c").read_text()
body = source[source.index("#define CPU_COUNT"):]

header = r'''
#define _DARWIN_C_SOURCE 1
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
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
typedef struct { USHORT Data2, Data3; ULONG Data1; UCHAR Data4[8]; } GUID_FAKE;
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
#define IRP_MJ_PNP 0x1b
#define IRP_MN_START_DEVICE 0x00
#define IRP_MN_QUERY_REMOVE_DEVICE 0x01
#define IRP_MN_REMOVE_DEVICE 0x02
#define IRP_MN_QUERY_STOP_DEVICE 0x05
#define IRP_MN_STOP_DEVICE 0x04
#define IRP_MN_QUERY_CAPABILITIES 0x09
#define IRP_MN_SURPRISE_REMOVAL 0x17
#define _ARM64_BARRIER_SY 15

typedef enum { PowerDeviceUnspecified = 0, PowerDeviceD0 = 1, PowerDeviceD3 = 4 } DEVICE_POWER_STATE;
typedef enum { PepDeviceNotAccepted = 0, PepDeviceAccepted = 1 } PEP_DEVICE_ACCEPTANCE_TYPE;
#define PEP_INFORMATION_VERSION 3
#define PEP_KERNEL_INFORMATION_V3 3
#define PEP_UNSPECIFIED_MINIMUM_STATE 0xFFFFFFFFu
#define PEP_PLATFORM_IDLE_STATE_NONE 0xFFFFFFFFu
#define PEP_IDLE_VETO_NONE 0u
#define PROCESSOR_HALT_VIA_PSCI_CPU_SUSPEND 0x20u
#define PROCESSOR_HALT_CACHE_FLUSH_OVERRIDE 0x01u

enum {
    PEP_DPM_PREPARE_DEVICE = 1, PEP_DPM_ABANDON_DEVICE, PEP_DPM_REGISTER_DEVICE,
    PEP_DPM_UNREGISTER_DEVICE, PEP_DPM_DEVICE_POWER_STATE, PEP_DPM_WORK,
    PEP_DPM_POWER_CONTROL_REQUEST, PEP_DPM_POWER_CONTROL_COMPLETE, PEP_DPM_SYSTEM_LATENCY_UPDATE,
    PEP_DPM_DEVICE_STARTED, PEP_DPM_NOTIFY_COMPONENT_IDLE_STATE, PEP_DPM_REGISTER_CRASHDUMP_DEVICE,
    PEP_DPM_DEVICE_IDLE_CONSTRAINTS, PEP_DPM_COMPONENT_IDLE_CONSTRAINTS
};
enum {
    PEP_NOTIFY_PPM_QUERY_CAPABILITIES = 1, PEP_NOTIFY_PPM_IDLE_EXECUTE, PEP_NOTIFY_PPM_IDLE_COMPLETE,
    PEP_NOTIFY_PPM_IS_PROCESSOR_HALTED, PEP_NOTIFY_PPM_INITIATE_WAKE, PEP_NOTIFY_PPM_QUERY_VETO_REASONS,
    PEP_NOTIFY_PPM_QUERY_VETO_REASON, PEP_NOTIFY_PPM_QUERY_PLATFORM_STATES, PEP_NOTIFY_PPM_QUERY_IDLE_STATES_V2,
    PEP_NOTIFY_PPM_QUERY_COORDINATED_STATES, PEP_NOTIFY_PPM_QUERY_COORDINATED_DEPENDENCY,
    PEP_NOTIFY_PPM_TEST_IDLE_STATE, PEP_NOTIFY_PPM_LPI_SUPPORTED, PEP_NOTIFY_PPM_ENTER_SYSTEM_STATE,
    PEP_NOTIFY_PPM_RESUME_FROM_SYSTEM_STATE
};

typedef struct { ULONG IdleStateCount; } PEP_COMPONENT_V2, *PPEP_COMPONENT_V2;
typedef struct { ULONG ComponentCount; PPEP_COMPONENT_V2 Components[1]; } PEP_DEVICE_REGISTER_V2, *PPEP_DEVICE_REGISTER_V2;
typedef struct { PCUNICODE_STRING DeviceId; BOOLEAN DeviceAccepted; } PEP_PREPARE_DEVICE, *PPEP_PREPARE_DEVICE, PEP_ABANDON_DEVICE, *PPEP_ABANDON_DEVICE;
typedef struct { PCUNICODE_STRING DeviceId; POHANDLE KernelHandle; PPEP_DEVICE_REGISTER_V2 Register; PEPHANDLE DeviceHandle; PEP_DEVICE_ACCEPTANCE_TYPE DeviceAccepted; } PEP_REGISTER_DEVICE, *PPEP_REGISTER_DEVICE;
typedef struct { PEPHANDLE DeviceHandle; } PEP_UNREGISTER_DEVICE, *PPEP_UNREGISTER_DEVICE, PEP_DEVICE_STARTED, *PPEP_DEVICE_STARTED;
typedef struct { PEPHANDLE DeviceHandle; DEVICE_POWER_STATE PowerState; BOOLEAN Complete; } PEP_DEVICE_POWER_STATE, *PPEP_DEVICE_POWER_STATE;
typedef struct { PEPHANDLE DeviceHandle; ULONG Component, IdleState; BOOLEAN DriverNotified, Completed; } PEP_NOTIFY_COMPONENT_IDLE_STATE, *PPEP_NOTIFY_COMPONENT_IDLE_STATE;
typedef struct { PEPHANDLE DeviceHandle; ULONG PlatformStateCount; DEVICE_POWER_STATE *MinimumDStates; } PEP_DEVICE_PLATFORM_CONSTRAINTS, *PPEP_DEVICE_PLATFORM_CONSTRAINTS;
typedef struct { PEPHANDLE DeviceHandle; ULONG Component, PlatformStateCount; ULONG *MinimumFStates; } PEP_COMPONENT_PLATFORM_CONSTRAINTS, *PPEP_COMPONENT_PLATFORM_CONSTRAINTS;
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
typedef BOOLEAN (*PEPCALLBACKNOTIFYDPM)(ULONG, PVOID);
typedef BOOLEAN (*PEPCALLBACKNOTIFYPPMS)(PEPHANDLE, ULONG, PVOID);
typedef struct { ULONG Version, Size; PEPCALLBACKNOTIFYDPM AcceptDeviceNotification; PEPCALLBACKNOTIFYPPMS AcceptProcessorNotification; } PEP_INFORMATION, *PPEP_INFORMATION;
typedef NTSTATUS (*PPROCESSOR_HALT_ROUTINE)(ULONG Flags, PVOID Parameter, PVOID HaltFn);
typedef struct { ULONG Version, Size; PPROCESSOR_HALT_ROUTINE ProcessorHalt; } PEP_KERNEL_INFORMATION, *PPEP_KERNEL_INFORMATION;
typedef struct { ULONG Flags; PWSTR Name; PVOID EntryContext; ULONG DefaultType; } RTL_QUERY_REGISTRY_TABLE;
typedef struct { BOOLEAN EjectSupported, Removable, SilentInstall; } DEVICE_CAPABILITIES, *PDEVICE_CAPABILITIES;
typedef struct { UCHAR MinorFunction; union { struct { PDEVICE_CAPABILITIES Capabilities; } DeviceCapabilities; } Parameters; } IO_STACK_LOCATION, *PIO_STACK_LOCATION;
typedef struct _IRP { struct { NTSTATUS Status; } IoStatus; IO_STACK_LOCATION Stack; } IRP, *PIRP;
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
static bool spin_locked = false;
static KIRQL cur_irql = PASSIVE_LEVEL;
static IO_WORKITEM_ROUTINE *queued_fn = NULL;
static PVOID queued_ctx = NULL;
static PDEVICE_OBJECT queued_obj = NULL;
static NTSTATUS (*display_cb)(LPCGUID, PVOID, ULONG, PVOID) = NULL;
static PVOID display_ctx = NULL;
static unsigned reg_writes = 0, unreg_calls = 0, work_freed = 0, dev_deleted = 0;
static ULONG traced_enable_sc7 = 99, traced_display_off = 99;

static void __dmb(int b) { (void)b; }
static void __wfi(void) {}
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
static NTSTATUS RtlCreateRegistryKey(ULONG rel, PWSTR path) {
    (void)rel; (void)path;
    assert(!spin_locked && cur_irql == PASSIVE_LEVEL);
    return 0;
}
static NTSTATUS RtlWriteRegistryValue(ULONG rel, PCWSTR path, PCWSTR name, ULONG type, PVOID data, ULONG len) {
    (void)rel; (void)path; (void)type; (void)len;
    assert(!spin_locked && cur_irql == PASSIVE_LEVEL);
    reg_writes++;
    if (!wcscmp(name, L"EnableSc7Active")) traced_enable_sc7 = *(ULONG *)data;
    if (!wcscmp(name, L"DisplayOff")) traced_display_off = *(ULONG *)data;
    return 0;
}
static NTSTATUS RtlQueryRegistryValues(ULONG rel, PCWSTR path, RTL_QUERY_REGISTRY_TABLE *t, PVOID c, PVOID e) {
    (void)rel; (void)path; (void)c; (void)e;
    assert(!spin_locked && cur_irql == PASSIVE_LEVEL);
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
#define READ_REGISTER_ULONG(p) (*(p))

static NTSTATUS MockHalt(ULONG f, PVOID p, PVOID fn) { (void)f; (void)p; (void)fn; return 0; }
static NTSTATUS PoFxRegisterPlugin(PPEP_INFORMATION info, PPEP_KERNEL_INFORMATION kern) {
    (void)info; kern->ProcessorHalt = MockHalt; return 0;
}

static void KeInitializeSpinLock(KSPIN_LOCK *l) { *l = 0; }
static void KeAcquireSpinLock(KSPIN_LOCK *l, KIRQL *i) { (void)l; assert(!spin_locked); spin_locked = true; *i = cur_irql; cur_irql = DISPATCH_LEVEL; }
static void KeReleaseSpinLock(KSPIN_LOCK *l, KIRQL i) { (void)l; assert(spin_locked); spin_locked = false; cur_irql = i; }
static void KeInitializeEvent(PKEVENT e, int t, BOOLEAN s) { (void)t; e->signaled = s; }
static void KeClearEvent(PKEVENT e) { e->signaled = false; }
static void KeSetEvent(PKEVENT e, int inc, BOOLEAN w) { (void)inc; (void)w; e->signaled = true; }
static void pump_work(void) {
    if (queued_fn) {
        IO_WORKITEM_ROUTINE *fn = queued_fn;
        PDEVICE_OBJECT obj = queued_obj;
        PVOID ctx = queued_ctx;
        queued_fn = NULL;
        fn(obj, ctx);
    }
}
static NTSTATUS KeWaitForSingleObject(PVOID o, int r, int m, BOOLEAN a, PVOID t) {
    (void)r; (void)m; (void)a; (void)t;
    PKEVENT ev = o;
    if (!ev->signaled) pump_work();
    assert(ev->signaled);
    return 0;
}
static void IoQueueWorkItem(PIO_WORKITEM w, IO_WORKITEM_ROUTINE *r, int q, PVOID c) {
    (void)q;
    assert(spin_locked && queued_fn == NULL);
    queued_obj = (PDEVICE_OBJECT)w;
    queued_fn = r;
    queued_ctx = c;
}
static PIO_WORKITEM IoAllocateWorkItem(PDEVICE_OBJECT o) { return (PIO_WORKITEM)o; }
static void IoFreeWorkItem(PIO_WORKITEM w) { (void)w; work_freed++; }
static NTSTATUS PoRegisterPowerSettingCallback(PDEVICE_OBJECT o, LPCGUID g, NTSTATUS (*cb)(LPCGUID, PVOID, ULONG, PVOID), PVOID ctx, PVOID *h) {
    (void)o;
    display_cb = cb; display_ctx = ctx; *h = (PVOID)1;
    ULONG on = 1;
    cb(g, &on, 4, ctx);
    return 0;
}
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID h) { assert(h == (PVOID)1); display_cb = NULL; unreg_calls++; return 0; }
static void IoCompleteRequest(PIRP i, int p) { (void)i; (void)p; }
static void IoCopyCurrentIrpStackLocationToNext(PIRP i) { (void)i; }
static void IoSkipCurrentIrpStackLocation(PIRP i) { (void)i; }
static PIO_STACK_LOCATION IoGetCurrentIrpStackLocation(PIRP i) { return &i->Stack; }
static void IoSetCompletionRoutine(PIRP i, NTSTATUS (*r)(PDEVICE_OBJECT, PIRP, PVOID), PVOID c, BOOLEAN s, BOOLEAN e, BOOLEAN cn) {
    (void)s; (void)e; (void)cn;
    i->IoStatus.Status = STATUS_SUCCESS;
    (void)r(NULL, i, c);
}
static NTSTATUS IoCallDriver(PDEVICE_OBJECT o, PIRP i) { (void)o; return i->IoStatus.Status; }
static void IoDetachDevice(PDEVICE_OBJECT o) { (void)o; }
static void IoDeleteDevice(PDEVICE_OBJECT o) { free(o->DeviceExtension); free(o); dev_deleted++; }
static NTSTATUS IoCreateDevice(PDRIVER_OBJECT d, ULONG ext, PUNICODE_STRING n, ULONG t, ULONG ch, BOOLEAN ex, PDEVICE_OBJECT *out) {
    (void)d; (void)n; (void)t; (void)ch; (void)ex;
    PDEVICE_OBJECT obj = calloc(1, sizeof(*obj));
    obj->DeviceExtension = calloc(1, ext);
    obj->Flags = DO_DEVICE_INITIALIZING | DO_POWER_PAGABLE;
    *out = obj;
    return STATUS_SUCCESS;
}
static NTSTATUS IoAttachDeviceToDeviceStackSafe(PDEVICE_OBJECT s, PDEVICE_OBJECT t, PDEVICE_OBJECT *l) {
    (void)s; *l = t; return STATUS_SUCCESS;
}
'''

tests = r'''
int main(void) {
    struct _DRIVER_EXTENSION ext = {0};
    struct _DRIVER_OBJECT drv = { .DriverExtension = &ext };
    struct _DEVICE_OBJECT pdo = {0};
    UNICODE_STRING reg_path;
    RtlInitUnicodeString(&reg_path, L"\\Registry\\Machine\\System\\CurrentControlSet\\Services\\tegra214pep");

    assert(DriverEntry(&drv, &reg_path) == STATUS_SUCCESS);
    assert(reg_writes == 0); /* ZERO registry writes during early PEP load! */

    /* 1. AddDevice + single-instance guard + START_DEVICE */
    assert(ext.AddDevice(&drv, &pdo) == STATUS_SUCCESS && AttachedDevice != NULL);
    assert(ext.AddDevice(&drv, &pdo) == STATUS_DEVICE_BUSY);
    PDEVICE_OBJECT fdo = (PDEVICE_OBJECT)AttachedDevice->Work;
    assert((fdo->Flags & (DO_DEVICE_INITIALIZING | DO_POWER_PAGABLE)) == 0);

    IRP irp = { .IoStatus = { STATUS_SUCCESS }, .Stack = { .MinorFunction = IRP_MN_START_DEVICE } };
    assert(drv.MajorFunction[IRP_MJ_PNP](fdo, &irp) == STATUS_SUCCESS);
    pump_work();
    assert(reg_writes > 0 && traced_enable_sc7 == 0 && traced_display_off == 0);

    /* 2. User enables SC7 in registry (Set-Sc7Mode.ps1) and turns screen off:
     *    DisplayOffState stays 0 until Snapshot refreshes EnableSc7=1, then both become 1 atomically! */
    reg_enable_sc7 = 1;
    ULONG off = 0, on = 1;
    assert(display_cb(&DisplayGuid, &off, 4, display_ctx) == STATUS_SUCCESS);
    assert(DisplayOffState == 0); /* Not armed before EnableSc7 refresh! */
    pump_work();
    assert(Sc7Enabled == 1 && DisplayOffState == 1 && traced_enable_sc7 == 1 && traced_display_off == 1);

    /* 3. Screen wake vetoes DisplayOffState SYNCHRONOUSLY before Snapshot runs! */
    assert(display_cb(&DisplayGuid, &on, 4, display_ctx) == STATUS_SUCCESS);
    assert(DisplayOffState == 0);
    pump_work();
    assert(traced_display_off == 0);

    /* 4. QUERY_REMOVE and QUERY_STOP are refused; REMOVE_DEVICE drains queued Snapshot and frees FDO cleanly! */
    irp.Stack.MinorFunction = IRP_MN_QUERY_REMOVE_DEVICE;
    assert(drv.MajorFunction[IRP_MJ_PNP](fdo, &irp) == STATUS_UNSUCCESSFUL);
    irp.Stack.MinorFunction = IRP_MN_QUERY_STOP_DEVICE;
    assert(drv.MajorFunction[IRP_MJ_PNP](fdo, &irp) == STATUS_UNSUCCESSFUL);

    /* Queue an in-flight display-off workitem right before REMOVE_DEVICE to test drain race! */
    assert(display_cb(&DisplayGuid, &off, 4, display_ctx) == STATUS_SUCCESS);
    assert(queued_fn != NULL);
    irp.Stack.MinorFunction = IRP_MN_REMOVE_DEVICE;
    assert(drv.MajorFunction[IRP_MJ_PNP](fdo, &irp) == STATUS_SUCCESS);
    assert(queued_fn == NULL && unreg_calls == 1 && work_freed == 1 && dev_deleted == 1);
    assert(AttachedDevice == NULL && DisplayOffState == 0);

    puts("PASS: Tegra214Power early-boot registry silence, EnableSc7-before-DisplayOff ordering, synchronous wake veto and remove drain");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="tegra214pep-notif-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(header + body + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
