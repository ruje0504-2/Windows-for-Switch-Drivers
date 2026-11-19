#!/usr/bin/env python3
"""Exercise SwitchPowerButton passive PMU_EXT wake IRQ, PMC polarity, SC7 resume and I2C fault masking."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "src/switchpwrbtn.c").read_text()

def function(name):
    m = re.search(r"^(?:static\s+)?(?:VOID|UCHAR|BOOLEAN|NTSTATUS)\s+" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
    assert m, f"missing {name}"
    end = source.index("{", m.start()) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[m.start():end] + "\n"

defines = "\n".join(re.findall(r"^#define (?:MAX77620_|ONOFFSTAT_|ONOFFIRQ_|PMC_|POLL_MS|RECOVERY_MS|SWPWR_|REPORT_ID_)[^\n]+", source, re.M))

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
''' + f'#include "{root / "src/button_state.h"}"\n' + r'''

typedef uint32_t ULONG, *PULONG;
typedef uint8_t UCHAR, *PUCHAR, BOOLEAN;
typedef int32_t LONG, NTSTATUS;
typedef uintptr_t ULONG_PTR;
typedef unsigned KIRQL, WDF_POWER_DEVICE_STATE;
typedef void VOID, *PVOID, *WDFIOTARGET, *WDFTIMER, *WDFINTERRUPT, *VHFHANDLE, *WDFDEVICE, *WDFOBJECT;
typedef const int *LPCGUID;
typedef union { int64_t QuadPart; struct { uint32_t LowPart; int32_t HighPart; }; } LARGE_INTEGER;
typedef unsigned KSPIN_LOCK;
typedef struct { PUCHAR reportBuffer; ULONG reportBufferLen; UCHAR reportId; } HID_XFER_PACKET;
typedef enum {
    PowerActionNone = 0,
    PowerActionSleep,
    PowerActionHibernate,
    PowerActionShutdown,
    PowerActionShutdownReset,
    PowerActionShutdownOff
} POWER_ACTION;
typedef enum {
    WdfDeviceFailedNone = 0,
    WdfDeviceFailedAttemptRestart = 1,
    WdfDeviceFailedNoRestart = 2
} WDF_DEVICE_FAILED_ACTION;
typedef struct _CM_PARTIAL_RESOURCE_DESCRIPTOR {
    UCHAR Type;
    union {
        struct { LARGE_INTEGER Start; ULONG Length; } Memory;
        struct { UCHAR Class; UCHAR Type; ULONG IdLowPart; ULONG IdHighPart; } Connection;
        struct { ULONG Vector; } Interrupt;
    } u;
} CM_PARTIAL_RESOURCE_DESCRIPTOR, *PCM_PARTIAL_RESOURCE_DESCRIPTOR;
typedef struct _FAKE_RESLIST {
    ULONG Count;
    CM_PARTIAL_RESOURCE_DESCRIPTOR Descs[8];
} FAKE_RESLIST, *WDFCMRESLIST;
typedef BOOLEAN (*PFN_WDF_INTERRUPT_ISR)(WDFINTERRUPT, ULONG);
typedef VOID (*PFN_WDF_INTERRUPT_DPC)(WDFINTERRUPT, WDFOBJECT);
typedef VOID (*PFN_WDF_INTERRUPT_WORKITEM)(WDFINTERRUPT, WDFOBJECT);
typedef struct _WDF_INTERRUPT_CONFIG {
    PFN_WDF_INTERRUPT_ISR EvtInterruptIsr;
    PFN_WDF_INTERRUPT_DPC EvtInterruptDpc;
    PFN_WDF_INTERRUPT_WORKITEM EvtInterruptWorkItem;
    BOOLEAN PassiveHandling;
    BOOLEAN CanWakeDevice;
    BOOLEAN AutomaticSerialization;
    PCM_PARTIAL_RESOURCE_DESCRIPTOR InterruptRaw;
    PCM_PARTIAL_RESOURCE_DESCRIPTOR InterruptTranslated;
} WDF_INTERRUPT_CONFIG;
typedef struct _WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS {
    int IdleCaps;
    int DxState;
    ULONG IdleTimeout;
    int IdleTimeoutType;
    int ExcludeD3Cold;
} WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS;
typedef struct _WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS {
    int DxState;
    int UserControlOfWakeSettings;
    int Enabled;
} WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS;
typedef struct _SWPWR_TIMER {
    WDFDEVICE Device;
} SWPWR_TIMER, *PSWPWR_TIMER;

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_IO_TIMEOUT -1
#define STATUS_DEVICE_NOT_READY -2
#define STATUS_INSUFFICIENT_RESOURCES -3
#define STATUS_DEVICE_CONFIGURATION_ERROR -4
#define STATUS_POWER_STATE_INVALID -5
#define CmResourceTypeInterrupt 2
#define CmResourceTypeMemory 3
#define CmResourceTypeConnection 134
#define CM_RESOURCE_CONNECTION_CLASS_SERIAL 2
#define CM_RESOURCE_CONNECTION_TYPE_SERIAL_I2C 1
#define PAGE_READWRITE 0x04
#define PAGE_NOCACHE 0x200
#define WDF_NO_OBJECT_ATTRIBUTES NULL
#define PowerDeviceD3 4
#define IdleCanWakeFromS0 1
#define SystemManagedIdleTimeoutWithHint 2
#define WdfTrue 1
#define WakeDoNotAllowUserControl 2
#define WdfPowerDeviceD3 3
#define WdfPowerDeviceD3Final 5
#define WDF_REL_TIMEOUT_IN_MS(x) (x)
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define IsEqualGUID(a, b) ((a) == (b))
static const int SwPwrDisplayGuid = 1;
''' + defines + r'''

typedef struct _SWPWR_DEVICE {
    LARGE_INTEGER I2cId;
    PULONG Pmc;
    BOOLEAN PmcPolaritySaved;
    ULONG OriginalPmcPolarity;
    WDFIOTARGET I2c;
    WDFTIMER Timer;
    WDFINTERRUPT Interrupt;
    BOOLEAN InD0;
    BOOLEAN S0IdleAssigned;
    BOOLEAN IdleStopped;
    BOOLEAN IrqConfigured;
    BOOLEAN OriginalMasksSaved;
    BOOLEAN IrqRecoveryPending;
    BOOLEAN RecoveryTimerArmed;
    BOOLEAN IrqFailed;
    BOOLEAN DeviceFailReported;
    UCHAR OriginalTopMask, OriginalGlobalMask, OriginalOnOffMask;
    UCHAR DeferredOnOffIrq;
    ULONG IsrFaults;
    ULONG RecoveryAttempts;
    VHFHANDLE Vhf;
    volatile BOOLEAN Stopping;
    struct swpwr_button_state Button;
    KSPIN_LOCK TimerLock;
    PVOID DisplayCallback;
    volatile LONG DisplayOff;
    UCHAR Report[2];
    ULONG Failures;
    LONG Polls;
    LONG Presses;
    LONG Reports;
    LONG WakeReports, PowerReports;
    volatile LONG LastReportStatus, LastIrqStatus, LastOnOffIrq;
} SWPWR_DEVICE, *PSWPWR_DEVICE;

static UCHAR pmic[256];
static ULONG pmc_cntrl;
static bool pmc_mapped = false;
static bool fail_i2c_read = false, fail_i2c_write = false;
static int fail_read_reg = -1;
static unsigned fail_write_after = 0, write_count = 0, read_count = 0;
static POWER_ACTION sys_action = PowerActionNone;
static bool in_d0_or_isr = false, in_isr = false;
static int irq_lock_depth = 0, timer_lock_depth = 0, idle_ref_count = 0;
static bool timer_pending = false;
static unsigned last_timer_ms = 0;
static unsigned workitem_queued = 0, timer_starts = 0, timer_stops = 0;
static UCHAR emitted_reports[16];
static unsigned emitted_count = 0;
static ULONG traced_wake = 0, traced_power = 0, traced_ready = 99;
static NTSTATUS irq_create_status = STATUS_SUCCESS, s0_idle_status = STATUS_SUCCESS, sx_wake_status = STATUS_SUCCESS;
static unsigned irq_create_count = 0, i2c_closes = 0, object_deletes = 0, vhf_deletes = 0, disp_unregs = 0;
static unsigned fail_device_calls = 0;
static WDF_DEVICE_FAILED_ACTION last_fail_action = WdfDeviceFailedNone;
static LARGE_INTEGER last_opened_i2c_id;

static PSWPWR_DEVICE SwPwrGetDevice(WDFDEVICE d) { return (PSWPWR_DEVICE)d; }
static PSWPWR_TIMER SwPwrGetTimer(WDFTIMER t) { return (PSWPWR_TIMER)t; }
static WDFDEVICE WdfInterruptGetDevice(WDFINTERRUPT i) { return (WDFDEVICE)i; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }
static LONG InterlockedCompareExchange(volatile LONG *p, LONG v, LONG c) { LONG o = *p; if (o == c) *p = v; return o; }
static void KeAcquireSpinLock(KSPIN_LOCK *l, KIRQL *irql) {
    (void)l; *irql = 0;
    assert(timer_lock_depth == 0);
    timer_lock_depth++;
}
static void KeReleaseSpinLock(KSPIN_LOCK *l, KIRQL irql) {
    (void)l; (void)irql;
    assert(timer_lock_depth == 1);
    timer_lock_depth--;
}
static void WdfInterruptAcquireLock(WDFINTERRUPT i) {
    assert(i != NULL && !in_isr && timer_lock_depth == 0 && irq_lock_depth == 0);
    irq_lock_depth++;
}
static void WdfInterruptReleaseLock(WDFINTERRUPT i) {
    assert(i != NULL && !in_isr && timer_lock_depth == 0 && irq_lock_depth == 1);
    irq_lock_depth--;
}
static NTSTATUS WdfDeviceStopIdle(WDFDEVICE d, BOOLEAN wait) {
    (void)d;
    assert(!wait && timer_lock_depth == 0);
    idle_ref_count++;
    return STATUS_SUCCESS;
}
static void WdfDeviceResumeIdle(WDFDEVICE d) {
    (void)d;
    assert(timer_lock_depth == 0 && idle_ref_count > 0);
    idle_ref_count--;
}
static void WdfDeviceSetFailed(WDFDEVICE d, WDF_DEVICE_FAILED_ACTION action) {
    (void)d;
    assert(!in_d0_or_isr && irq_lock_depth == 0 && timer_lock_depth == 0);
    fail_device_calls++;
    last_fail_action = action;
}

static void SwPwrTrace(const wchar_t *name, ULONG val) {
    assert(!in_d0_or_isr && irq_lock_depth == 0 && timer_lock_depth == 0);
    if (!wcscmp(name, L"WakeReports")) traced_wake = val;
    if (!wcscmp(name, L"PowerReports")) traced_power = val;
    if (!wcscmp(name, L"WakeIrqReady")) traced_ready = val;
}
static NTSTATUS SwPwrOpenTarget(WDFDEVICE d, LARGE_INTEGER id, WDFIOTARGET *t) {
    (void)d; last_opened_i2c_id = id; *t = (WDFIOTARGET)1; return STATUS_SUCCESS;
}
static NTSTATUS SwPwrReadReg(WDFIOTARGET t, UCHAR reg, PUCHAR val) {
    assert(t != NULL);
    read_count++;
    if (fail_i2c_read || (fail_read_reg >= 0 && reg == (UCHAR)fail_read_reg)) return STATUS_IO_TIMEOUT;
    *val = pmic[reg];
    if (reg == MAX77620_ONOFFIRQ) pmic[reg] = 0; /* clear-on-read hardware semantics! */
    return STATUS_SUCCESS;
}
static NTSTATUS SwPwrWriteReg(WDFIOTARGET t, UCHAR reg, UCHAR val) {
    assert(t != NULL);
    if (fail_i2c_write) return STATUS_IO_TIMEOUT;
    if (fail_write_after && ++write_count >= fail_write_after) return STATUS_IO_TIMEOUT;
    pmic[reg] = val;
    return STATUS_SUCCESS;
}
static NTSTATUS VhfReadReportSubmit(VHFHANDLE v, HID_XFER_PACKET *p) {
    assert(v != NULL && p->reportBufferLen == 2 && p->reportBuffer[0] == REPORT_ID_SYSTEM);
    assert(emitted_count < sizeof(emitted_reports));
    emitted_reports[emitted_count++] = p->reportBuffer[1];
    return STATUS_SUCCESS;
}
static BOOLEAN WdfInterruptQueueWorkItemForIsr(WDFINTERRUPT i) { (void)i; workitem_queued++; return TRUE; }
static POWER_ACTION WdfDeviceGetSystemPowerAction(WDFDEVICE d) { (void)d; return sys_action; }
static void WdfTimerStart(WDFTIMER t, unsigned ms) {
    assert(t != NULL && ms > 0);
    timer_starts++;
    timer_pending = true;
    last_timer_ms = ms;
}
static BOOLEAN WdfTimerStop(WDFTIMER t, BOOLEAN wait) {
    assert(t != NULL);
    if (wait) assert(irq_lock_depth == 0 && timer_lock_depth == 0);
    timer_stops++;
    timer_pending = false;
    return TRUE;
}
static ULONG WdfCmResourceListGetCount(WDFCMRESLIST l) { return l ? l->Count : 0; }
static PCM_PARTIAL_RESOURCE_DESCRIPTOR WdfCmResourceListGetDescriptor(WDFCMRESLIST l, ULONG i) {
    return (l && i < l->Count) ? &l->Descs[i] : NULL;
}
static PVOID MmMapIoSpaceEx(LARGE_INTEGER addr, size_t len, ULONG protect) {
    (void)protect;
    assert(addr.QuadPart == 0x7000e400 && len == sizeof(ULONG) && !pmc_mapped);
    pmc_mapped = true;
    return &pmc_cntrl;
}
static void MmUnmapIoSpace(PVOID base, size_t len) {
    assert(base == &pmc_cntrl && len == sizeof(ULONG) && pmc_mapped);
    pmc_mapped = false;
}
static void WDF_INTERRUPT_CONFIG_INIT(WDF_INTERRUPT_CONFIG *c, PFN_WDF_INTERRUPT_ISR isr, PFN_WDF_INTERRUPT_DPC dpc) {
    memset(c, 0, sizeof(*c)); c->EvtInterruptIsr = isr; c->EvtInterruptDpc = dpc;
}
static NTSTATUS WdfInterruptCreate(WDFDEVICE d, WDF_INTERRUPT_CONFIG *cfg, PVOID attr, WDFINTERRUPT *out) {
    (void)attr;
    assert(cfg->PassiveHandling && cfg->CanWakeDevice && !cfg->AutomaticSerialization);
    assert(cfg->EvtInterruptIsr != NULL && cfg->EvtInterruptWorkItem != NULL);
    assert(cfg->InterruptRaw != NULL && cfg->InterruptTranslated != NULL);
    if (!NT_SUCCESS(irq_create_status)) return irq_create_status;
    irq_create_count++;
    *out = (WDFINTERRUPT)d;
    return STATUS_SUCCESS;
}
static void WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS_INIT(WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS *s, int caps) {
    memset(s, 0, sizeof(*s)); s->IdleCaps = caps;
}
static NTSTATUS WdfDeviceAssignS0IdleSettings(WDFDEVICE d, WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS *s) {
    (void)d; (void)s; return s0_idle_status;
}
static void WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS_INIT(WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS *s) {
    memset(s, 0, sizeof(*s));
}
static NTSTATUS WdfDeviceAssignSxWakeSettings(WDFDEVICE d, WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS *s) {
    (void)d; (void)s; return sx_wake_status;
}
static void WdfIoTargetClose(WDFIOTARGET t) {
    assert(irq_lock_depth == 0 && timer_lock_depth == 0 && t != NULL);
    i2c_closes++;
}
static void WdfObjectDelete(WDFOBJECT o) {
    assert(irq_lock_depth == 0 && timer_lock_depth == 0 && o == (WDFOBJECT)1);
    object_deletes++;
}
static void VhfDelete(VHFHANDLE v, BOOLEAN wait) {
    assert(irq_lock_depth == 0 && timer_lock_depth == 0 && v != NULL && wait);
    vhf_deletes++;
}
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID handle) {
    assert(handle != NULL);
    disp_unregs++;
    return STATUS_SUCCESS;
}
#define READ_REGISTER_ULONG(p) (*(p))
#define WRITE_REGISTER_ULONG(p, v) (*(p) = (v))
'''

funcs = "".join(function(n) for n in [
    "SwPwrDisplayChanged",
    "SwPwrPressReport",
    "SwPwrFlushReportTraces",
    "SwPwrReport",
    "SwPwrLockIrq",
    "SwPwrUnlockIrq",
    "SwPwrSetPmcPolarity",
    "SwPwrReleaseIdleRefLocked",
    "SwPwrScheduleRecoveryLocked",
    "SwPwrSetIrq",
    "SwPwrMaskOnOffRoute",
    "SwPwrRecoverIrqLocked",
    "SwPwrEvtD0Entry",
    "SwPwrEvtD0Exit",
    "SwPwrRunIrqRecovery",
    "SwPwrEvtTimer",
    "SwPwrEvtIsr",
    "SwPwrEvtInterruptWorkItem",
    "SwPwrEvtPrepareHardware",
    "SwPwrEvtReleaseHardware",
    "SwPwrEvtSuspend",
    "SwPwrEvtRestart",
    "SwPwrEvtSelfManagedIoCleanup",
])

tests = r'''
static SWPWR_TIMER timer_ctx;

static BOOLEAN call_isr(PSWPWR_DEVICE d) {
    BOOLEAN handled;
    assert(!in_d0_or_isr && !in_isr && irq_lock_depth == 0 && timer_lock_depth == 0);
    in_d0_or_isr = true;
    in_isr = true;
    irq_lock_depth = 1;
    handled = SwPwrEvtIsr(d->Interrupt ? d->Interrupt : (WDFINTERRUPT)d, 0);
    assert(irq_lock_depth == 1 && timer_lock_depth == 0);
    irq_lock_depth = 0;
    in_isr = false;
    in_d0_or_isr = false;
    return handled;
}

static void call_timer(PSWPWR_DEVICE d) {
    assert(!in_d0_or_isr && !in_isr && irq_lock_depth == 0 && timer_lock_depth == 0);
    timer_pending = false; /* one-shot WDFTIMER expires before callback runs */
    SwPwrEvtTimer(d->Timer);
    assert(irq_lock_depth == 0 && timer_lock_depth == 0);
}

static void init_reslist(FAKE_RESLIST *list, bool with_pmc, ULONG i2c_low, bool with_irq) {
    memset(list, 0, sizeof(*list));
    if (with_pmc) {
        CM_PARTIAL_RESOURCE_DESCRIPTOR *d = &list->Descs[list->Count++];
        d->Type = CmResourceTypeMemory;
        d->u.Memory.Start.QuadPart = 0x7000e400;
        d->u.Memory.Length = 4;
    }
    if (i2c_low != 0) {
        CM_PARTIAL_RESOURCE_DESCRIPTOR *d = &list->Descs[list->Count++];
        d->Type = CmResourceTypeConnection;
        d->u.Connection.Class = CM_RESOURCE_CONNECTION_CLASS_SERIAL;
        d->u.Connection.Type = CM_RESOURCE_CONNECTION_TYPE_SERIAL_I2C;
        d->u.Connection.IdLowPart = i2c_low;
        d->u.Connection.IdHighPart = 0;
    }
    if (with_irq) {
        CM_PARTIAL_RESOURCE_DESCRIPTOR *d = &list->Descs[list->Count++];
        d->Type = CmResourceTypeInterrupt;
        d->u.Interrupt.Vector = 118;
    }
}

static void reset_env(PSWPWR_DEVICE d, bool irq_mode) {
    assert(irq_lock_depth == 0 && timer_lock_depth == 0 && idle_ref_count == 0);
    memset(d, 0, sizeof(*d));
    memset(pmic, 0, sizeof(pmic));
    pmc_cntrl = 0x00000100u; /* unrelated PMC bits set, bit 17 initially 0 */
    pmc_mapped = false;
    d->Pmc = &pmc_cntrl;
    d->I2c = (WDFIOTARGET)1;
    d->Vhf = (VHFHANDLE)1;
    timer_ctx.Device = d;
    d->Timer = &timer_ctx;
    d->Interrupt = irq_mode ? (WDFINTERRUPT)d : NULL;
    d->S0IdleAssigned = irq_mode ? TRUE : FALSE;
    d->Stopping = TRUE;
    /* Unrelated PMIC interrupt masks must remain untouched! */
    pmic[MAX77620_IRQTOPM]   = 0xFDu; /* bit 1 ONOFF unmasked (0), others 1 */
    pmic[MAX77620_INTENLBT]  = 0xAAu | 0x01u;
    pmic[MAX77620_ONOFFIRQM] = 0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F;
    pmic[MAX77620_ONOFFIRQ]  = ONOFFIRQ_EN0_R; /* stale boot edge to clear on first arm */
    fail_i2c_read = fail_i2c_write = false;
    fail_read_reg = -1;
    fail_write_after = write_count = read_count = 0;
    irq_create_status = s0_idle_status = sx_wake_status = STATUS_SUCCESS;
    irq_create_count = i2c_closes = object_deletes = vhf_deletes = disp_unregs = 0;
    fail_device_calls = 0;
    last_fail_action = WdfDeviceFailedNone;
    last_opened_i2c_id.QuadPart = 0;
    sys_action = PowerActionNone;
    in_d0_or_isr = in_isr = false;
    timer_pending = false;
    last_timer_ms = 0;
    workitem_queued = timer_starts = timer_stops = emitted_count = 0;
    traced_wake = traced_power = 0; traced_ready = 99;
}

int main(void) {
    SWPWR_DEVICE d;

    /* 1. Initial D0Entry + Restart in passive-IRQ profile:
     *    - Sets PMC_INTR_POLARITY (bit 17) preserving neighbor bits
     *    - Clears stale boot ONOFFIRQ while top-masked
     *    - Unmasks press (EN0_R=0) and masks release (EN0_F=1) so release cannot wake SC7!
     *    - Does NOT start the 50 ms polling timer! */
    reset_env(&d, true);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(pmc_cntrl == (0x00000100u | PMC_INTR_POLARITY));
    assert(d.IrqConfigured && pmic[MAX77620_ONOFFIRQ] == 0);
    assert(pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_F));
    assert((pmic[MAX77620_INTENLBT] & 0x01u) == 0 && (pmic[MAX77620_INTENLBT] & 0xFEu) == 0xAAu);
    assert((pmic[MAX77620_IRQTOPM] & 0x02u) == 0 && (pmic[MAX77620_IRQTOPM] & 0xFDu) == 0xFDu);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 0);

    /* 2. Press with display ON -> SWPWR_POWER + 0 pulse; shared non-EN0 IRQs return FALSE. */
    ULONG disp_on = 1, disp_off = 0;
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_on, 4, &d) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = 0;
    assert(call_isr(&d) == FALSE && emitted_count == 0 && workitem_queued == 0);
    pmic[MAX77620_ONOFFIRQ] = 0x40u;
    assert(call_isr(&d) == FALSE && emitted_count == 0 && workitem_queued == 0 && pmic[MAX77620_ONOFFIRQ] == 0);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == TRUE);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_POWER && emitted_reports[1] == 0);
    assert(workitem_queued == 1 && d.PowerReports == 1 && d.WakeReports == 0);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(traced_power == 1 && traced_ready == 1 && !timer_pending && idle_ref_count == 0);

    /* 3. Repeated S0-idle D3 / SC7 sleep & wake cycles:
     *    - D0Exit(D3, Sleep) keeps PMIC wake armed!
     *    - SC7 wipes PMC_CNTRL bit 17 and latches ONOFFIRQ_EN0_R on button press!
     *    - D0Entry restores PMC_INTR_POLARITY WITHOUT clearing latched ONOFFIRQ!
     *    - Passive ISR consumes ONOFFIRQ_EN0_R while DisplayOff==1 and emits SWPWR_WAKE + 0! */
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_off, 4, &d) == STATUS_SUCCESS);
    for (unsigned cycle = 0; cycle < 3; ++cycle) {
        sys_action = PowerActionSleep;
        assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
        in_d0_or_isr = true;
        assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
        in_d0_or_isr = false;
        assert(d.IrqConfigured && (pmic[MAX77620_IRQTOPM] & 0x02u) == 0);

        /* SC7 resets PMC_CNTRL polarity and user presses Power to wake! */
        pmc_cntrl = 0;
        pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
        emitted_count = 0;

        in_d0_or_isr = true;
        assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
        in_d0_or_isr = false;
        assert((pmc_cntrl & PMC_INTR_POLARITY) != 0);
        assert(pmic[MAX77620_ONOFFIRQ] == ONOFFIRQ_EN0_R); /* NOT lost before ISR! */
        assert(call_isr(&d) == TRUE);
        assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 0);

        assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);
        assert(d.WakeReports == (LONG)(cycle + 1));
    }

    /* 4. Transient ONOFFIRQ read failure with Original*Mask==0 explicitly masks ONOFF,
     *    returns FALSE for shared level IRQ, holds S0-idle ref, recovers after bus resumes,
     *    delivers the latched wake press, keeps next S0-idle wake working, and preserves originals. */
    reset_env(&d, true);
    pmic[MAX77620_IRQTOPM] = 0x00u;
    pmic[MAX77620_INTENLBT] = 0x00u;
    pmic[MAX77620_ONOFFIRQM] = 0x00u;
    pmic[MAX77620_ONOFFIRQ] = 0;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_off, 4, &d) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    fail_read_reg = MAX77620_ONOFFIRQ;
    assert(call_isr(&d) == FALSE);
    assert((pmic[MAX77620_IRQTOPM] & 0x02u) == 0x02u);
    assert((pmic[MAX77620_ONOFFIRQM] & (ONOFFIRQ_EN0_F | ONOFFIRQ_EN0_R)) == (ONOFFIRQ_EN0_F | ONOFFIRQ_EN0_R));
    assert(!d.IrqConfigured && d.IrqRecoveryPending && d.OriginalMasksSaved);
    assert(d.OriginalTopMask == 0x00u && d.OriginalGlobalMask == 0x00u && d.OriginalOnOffMask == 0x00u);
    assert(pmic[MAX77620_ONOFFIRQ] == ONOFFIRQ_EN0_R && emitted_count == 0);
    {
        unsigned reads_before = read_count;
        assert(call_isr(&d) == FALSE && read_count == reads_before);
    }
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && last_timer_ms == RECOVERY_MS && idle_ref_count == 1 && fail_device_calls == 0);
    call_timer(&d);
    assert(!d.IrqConfigured && d.IrqRecoveryPending && d.RecoveryAttempts == 1);
    assert(timer_pending && last_timer_ms == RECOVERY_MS * 2u && idle_ref_count == 1 && fail_device_calls == 0);
    fail_read_reg = -1;
    call_timer(&d);
    assert(d.IrqConfigured && !d.IrqRecoveryPending && !d.IrqFailed && d.RecoveryAttempts == 0);
    assert(!timer_pending && idle_ref_count == 0 && pmic[MAX77620_ONOFFIRQ] == 0);
    assert(pmic[MAX77620_IRQTOPM] == 0x00u && pmic[MAX77620_ONOFFIRQM] == ONOFFIRQ_EN0_F);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);
    sys_action = PowerActionNone;
    emitted_count = 0;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && d.IrqConfigured);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
    assert(call_isr(&d) == TRUE && emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    sys_action = PowerActionShutdown;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.IrqConfigured && !d.OriginalMasksSaved);
    assert(pmic[MAX77620_IRQTOPM] == 0x00u && pmic[MAX77620_ONOFFIRQM] == 0x00u && pmic[MAX77620_INTENLBT] == 0x00u);

    /* 5. Transient total I2C failure recovers via ISR or timer; persistent failure hits finite bound
     *    with no hot loop, completes D0 teardown even while I2C is dead, and restarts cleanly. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_off, 4, &d) == STATUS_SUCCESS);
    fail_i2c_read = true;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && d.IrqConfigured && d.IrqRecoveryPending && !d.IrqFailed && d.IsrFaults == 1);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && idle_ref_count == 1 && fail_device_calls == 0);
    fail_i2c_read = false;
    assert(call_isr(&d) == TRUE && !d.IrqRecoveryPending && d.IsrFaults == 0);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(idle_ref_count == 0);
    call_timer(&d);
    assert(!timer_pending && idle_ref_count == 0);

    emitted_count = 0;
    fail_i2c_read = true;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && d.IrqRecoveryPending && !d.IrqFailed && d.IsrFaults == 1);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && idle_ref_count == 1);
    fail_i2c_read = false;
    call_timer(&d);
    assert(d.IrqConfigured && !d.IrqRecoveryPending && !timer_pending && idle_ref_count == 0);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);

    fail_i2c_read = true;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    for (unsigned i = 0; i < SWPWR_MAX_ISR_FAULTS; ++i) {
        assert(call_isr(&d) == FALSE);
    }
    assert(d.IrqFailed && !d.IrqRecoveryPending && d.IsrFaults == SWPWR_MAX_ISR_FAULTS);
    {
        unsigned reads_before = read_count;
        assert(call_isr(&d) == FALSE && read_count == reads_before);
    }
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(fail_device_calls == 1 && last_fail_action == WdfDeviceFailedAttemptRestart && !timer_pending && idle_ref_count == 0);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(fail_device_calls == 1);
    /* D0Exit(D3Final) must still complete cleanly even while I2C remains dead. */
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(!d.InD0 && !d.IrqConfigured && !d.OriginalMasksSaved && !d.IrqFailed && d.LastIrqStatus == STATUS_IO_TIMEOUT);
    fail_i2c_read = false;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && d.IrqConfigured && !d.IrqFailed && !d.DeviceFailReported);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    emitted_count = 0;
    assert(call_isr(&d) == TRUE && emitted_count == 2);

    /* 6. Persistent failure across recovery timer retries terminates at finite bound and fails device once. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    fail_read_reg = MAX77620_ONOFFIRQ;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && !d.IrqConfigured && d.IrqRecoveryPending);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && idle_ref_count == 1);
    fail_i2c_read = true;
    for (unsigned attempt = 1; attempt <= SWPWR_MAX_RECOVERY_ATTEMPTS; ++attempt) {
        assert(timer_pending);
        call_timer(&d);
        if (attempt < SWPWR_MAX_RECOVERY_ATTEMPTS) {
            assert(timer_pending && last_timer_ms == RECOVERY_MS * (attempt + 1u));
            assert(!d.IrqFailed && fail_device_calls == 0 && idle_ref_count == 1);
        } else {
            assert(!timer_pending && d.IrqFailed && !d.IrqRecoveryPending);
            assert(fail_device_calls == 1 && last_fail_action == WdfDeviceFailedAttemptRestart && idle_ref_count == 0);
        }
    }

    /* 7. Partial write failure during recovery after clearing ONOFFIRQ preserves deferred press and saved masks. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_on, 4, &d) == STATUS_SUCCESS);
    fail_read_reg = MAX77620_ONOFFIRQ;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && !d.IrqConfigured && d.IrqRecoveryPending);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    fail_read_reg = -1;
    fail_write_after = 2;
    write_count = 0;
    call_timer(&d);
    assert(pmic[MAX77620_ONOFFIRQ] == 0 && d.DeferredOnOffIrq == ONOFFIRQ_EN0_R);
    assert((pmic[MAX77620_IRQTOPM] & 0x02u) != 0 && !d.IrqConfigured && d.IrqRecoveryPending && timer_pending && emitted_count == 0);
    fail_write_after = 0;
    call_timer(&d);
    assert(d.IrqConfigured && !d.IrqRecoveryPending && d.DeferredOnOffIrq == 0 && !timer_pending && idle_ref_count == 0);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_POWER && emitted_reports[1] == 0);
    sys_action = PowerActionShutdown;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(pmic[MAX77620_IRQTOPM] == 0xFDu && pmic[MAX77620_INTENLBT] == (0xAAu | 0x01u));
    assert(pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F));

    /* 8. Synchronization across D0Entry/Restart, Suspend/Cleanup teardown, and D0Exit pending recovery. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_off, 4, &d) == STATUS_SUCCESS);
    sys_action = PowerActionSleep;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
    fail_read_reg = MAX77620_ONOFFIRQ;
    assert(call_isr(&d) == FALSE && !d.IrqConfigured && d.IrqRecoveryPending);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(!timer_pending && idle_ref_count == 0); /* Stopping==TRUE until Restart */
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_pending && idle_ref_count == 1);
    fail_read_reg = -1;
    call_timer(&d);
    assert(d.IrqConfigured && !d.IrqRecoveryPending && !timer_pending && idle_ref_count == 0);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);

    /* Teardown during active recovery stops timer, restores masks, and guards late callbacks. */
    fail_read_reg = MAX77620_ONOFFIRQ;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && d.IrqRecoveryPending);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && idle_ref_count == 1);
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS && !timer_pending && idle_ref_count == 0);
    {
        unsigned r = read_count;
        call_timer(&d);
        assert(read_count == r && !timer_pending && idle_ref_count == 0);
    }
    fail_read_reg = -1;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.InD0);
    assert(pmic[MAX77620_IRQTOPM] == 0xFDu && pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F));
    d.DisplayCallback = (PVOID)1;
    SwPwrEvtSelfManagedIoCleanup(&d);
    assert(d.I2c == NULL && d.Vhf == NULL && d.DisplayCallback == NULL);
    assert(i2c_closes == 1 && object_deletes == 1 && vhf_deletes == 1 && disp_unregs == 1);
    {
        unsigned r = read_count;
        SwPwrEvtInterruptWorkItem(&d, NULL);
        assert(call_isr(&d) == FALSE && read_count == r && !timer_pending && idle_ref_count == 0 && fail_device_calls == 0);
    }

    /* Entering Sleep D3 with pending recovery synchronously re-arms once when bus is back. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    fail_read_reg = MAX77620_ONOFFIRQ;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    assert(call_isr(&d) == FALSE && !d.IrqConfigured && d.IrqRecoveryPending);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(timer_pending && idle_ref_count == 1);
    fail_read_reg = -1;
    sys_action = PowerActionSleep;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS && !timer_pending && idle_ref_count == 0);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(d.IrqConfigured && !d.IrqRecoveryPending && !d.InD0);
    assert((pmic[MAX77620_IRQTOPM] & 0x02u) == 0 && pmic[MAX77620_ONOFFIRQ] == 0 && emitted_count == 0);

    /* 9. Mid-configuration write fault in SwPwrSetIrq(TRUE) protects saved original masks across retry! */
    reset_env(&d, true);
    fail_write_after = 3;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_IO_TIMEOUT && !d.IrqConfigured && !d.InD0 && d.OriginalMasksSaved);
    assert(pmic[MAX77620_IRQTOPM] == 0xFFu && d.OriginalTopMask == 0xFDu);
    fail_write_after = 0;
    write_count = 0;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && d.IrqConfigured && d.InD0);
    sys_action = PowerActionShutdown;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.IrqConfigured && !d.OriginalMasksSaved);
    assert(pmic[MAX77620_IRQTOPM] == 0xFDu && pmic[MAX77620_INTENLBT] == (0xAAu | 0x01u));
    assert(pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F));

    /* 10. Shutdown/D3Final and S4 Hibernate (D3 + PMIC power-off reset) disarm and re-arm cleanly. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && d.IrqConfigured);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    sys_action = PowerActionHibernate;
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && !d.IrqConfigured && !d.OriginalMasksSaved);
    assert(pmic[MAX77620_IRQTOPM] == 0xFDu && pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F));
    pmc_cntrl = 0;
    pmic[MAX77620_IRQTOPM] = 0xFFu;
    pmic[MAX77620_INTENLBT] = 0xFFu;
    pmic[MAX77620_ONOFFIRQM] = 0xFFu;
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    sys_action = PowerActionNone;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && d.IrqConfigured && d.OriginalMasksSaved);
    assert((pmc_cntrl & PMC_INTR_POLARITY) != 0 && pmic[MAX77620_ONOFFIRQ] == 0);
    assert((pmic[MAX77620_IRQTOPM] & 0x02u) == 0 && (pmic[MAX77620_ONOFFIRQM] & ONOFFIRQ_EN0_R) == 0);
    assert((pmic[MAX77620_ONOFFIRQM] & ONOFFIRQ_EN0_F) != 0);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 0);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    emitted_count = 0;
    assert(call_isr(&d) == TRUE && emitted_count == 2);

    /* 11. PrepareHardware failure leaves PMC_CNTRL untouched; ReleaseHardware cleans up for PnP rebalance. */
    FAKE_RESLIST raw, trans;
    reset_env(&d, false);
    d.Pmc = NULL; d.I2c = NULL;
    init_reslist(&raw, true, 0, true);
    init_reslist(&trans, true, 0, true);
    assert(SwPwrEvtPrepareHardware(&d, &raw, &trans) == STATUS_DEVICE_CONFIGURATION_ERROR);
    assert(pmc_cntrl == 0x00000100u && !d.PmcPolaritySaved && pmc_mapped);
    assert(SwPwrEvtReleaseHardware(&d, &trans) == STATUS_SUCCESS);
    assert(d.Pmc == NULL && !pmc_mapped && d.Interrupt == NULL && d.I2cId.QuadPart == 0 && pmc_cntrl == 0x00000100u && object_deletes == 0);

    reset_env(&d, false);
    d.Pmc = NULL; d.I2c = NULL;
    irq_create_status = STATUS_INSUFFICIENT_RESOURCES;
    init_reslist(&raw, true, 0x1111u, true);
    init_reslist(&trans, true, 0x1111u, true);
    assert(SwPwrEvtPrepareHardware(&d, &raw, &trans) == STATUS_INSUFFICIENT_RESOURCES);
    assert(pmc_cntrl == 0x00000100u && !d.PmcPolaritySaved && pmc_mapped && d.Interrupt == NULL);
    assert(SwPwrEvtReleaseHardware(&d, &trans) == STATUS_SUCCESS && d.Pmc == NULL && !pmc_mapped && pmc_cntrl == 0x00000100u);

    reset_env(&d, false);
    d.Pmc = NULL; d.I2c = NULL;
    init_reslist(&raw, true, 0x1111u, true);
    init_reslist(&trans, true, 0x1111u, true);
    assert(SwPwrEvtPrepareHardware(&d, &raw, &trans) == STATUS_SUCCESS);
    assert(d.Interrupt != NULL && d.S0IdleAssigned && d.I2cId.LowPart == 0x1111u && irq_create_count == 1);
    assert(pmc_cntrl == (0x00000100u | PMC_INTR_POLARITY) && d.PmcPolaritySaved && d.OriginalPmcPolarity == 0);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(d.IrqConfigured && last_opened_i2c_id.LowPart == 0x1111u);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(SwPwrEvtReleaseHardware(&d, &trans) == STATUS_SUCCESS);
    assert(d.Pmc == NULL && !pmc_mapped && d.I2c == NULL && d.Interrupt == NULL && d.I2cId.QuadPart == 0 && !d.S0IdleAssigned);
    assert(pmc_cntrl == 0x00000100u && !d.PmcPolaritySaved && i2c_closes == 1 && object_deletes == 1);
    init_reslist(&raw, true, 0x2222u, true);
    init_reslist(&trans, true, 0x2222u, true);
    assert(SwPwrEvtPrepareHardware(&d, &raw, &trans) == STATUS_SUCCESS);
    assert(d.Interrupt != NULL && d.S0IdleAssigned && d.I2cId.LowPart == 0x2222u && irq_create_count == 2 && pmc_mapped);
    assert(pmc_cntrl == (0x00000100u | PMC_INTR_POLARITY));
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(d.IrqConfigured && last_opened_i2c_id.LowPart == 0x2222u);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    emitted_count = 0;
    assert(call_isr(&d) == TRUE && emitted_count == 2);
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
    in_d0_or_isr = true;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    in_d0_or_isr = false;
    assert(SwPwrEvtReleaseHardware(&d, &trans) == STATUS_SUCCESS && !pmc_mapped && pmc_cntrl == 0x00000100u);

    /* 12. Legacy firmware without Interrupt resource keeps 50 ms polling fallback intact. */
    reset_env(&d, false);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.IrqConfigured && pmc_cntrl == 0x00000100u);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 1 && timer_pending && last_timer_ms == POLL_MS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    pmic[MAX77620_ONOFFSTAT] = 0;
    call_timer(&d);
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_POWER && emitted_reports[1] == 0);
    assert(timer_starts == 2 && timer_pending && last_timer_ms == POLL_MS);
    assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS && !timer_pending);
    assert(irq_lock_depth == 0 && timer_lock_depth == 0 && idle_ref_count == 0);

    puts("PASS: SwitchPowerButton PMU_EXT wake IRQ, fault recovery, shared IRQ ownership, S4/PnP lifecycle, and polling fallback");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="swpwr-wake-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(prefix + funcs + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
