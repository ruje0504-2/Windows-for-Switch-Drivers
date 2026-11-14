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

defines = "\n".join(re.findall(r"^#define (?:MAX77620_|ONOFFSTAT_|ONOFFIRQ_|PMC_|POLL_MS|REPORT_ID_)[^\n]+", source, re.M))

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

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_IO_TIMEOUT -1
#define STATUS_DEVICE_NOT_READY -2
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
    WDFIOTARGET I2c;
    WDFTIMER Timer;
    WDFINTERRUPT Interrupt;
    BOOLEAN IrqConfigured;
    UCHAR OriginalTopMask, OriginalGlobalMask, OriginalOnOffMask;
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

static PSWPWR_DEVICE SwPwrGetDevice(WDFDEVICE d) { return (PSWPWR_DEVICE)d; }
static WDFDEVICE WdfInterruptGetDevice(WDFINTERRUPT i) { return (WDFDEVICE)i; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }
static LONG InterlockedCompareExchange(volatile LONG *p, LONG v, LONG c) { LONG o = *p; if (o == c) *p = v; return o; }
static void KeAcquireSpinLock(KSPIN_LOCK *l, KIRQL *irql) { (void)l; *irql = 0; }
static void KeReleaseSpinLock(KSPIN_LOCK *l, KIRQL irql) { (void)l; (void)irql; }

static UCHAR pmic[256];
static ULONG pmc_cntrl;
static bool fail_i2c_read = false, fail_i2c_write = false;
static unsigned fail_write_after = 0, write_count = 0;
static POWER_ACTION sys_action = PowerActionNone;
static bool in_d0_or_isr = false;
static unsigned workitem_queued = 0, timer_starts = 0, timer_stops = 0;
static UCHAR emitted_reports[16];
static unsigned emitted_count = 0;
static ULONG traced_wake = 0, traced_power = 0, traced_ready = 99;

static void SwPwrTrace(const wchar_t *name, ULONG val) {
    assert(!in_d0_or_isr);
    if (!wcscmp(name, L"WakeReports")) traced_wake = val;
    if (!wcscmp(name, L"PowerReports")) traced_power = val;
    if (!wcscmp(name, L"WakeIrqReady")) traced_ready = val;
}
static NTSTATUS SwPwrOpenTarget(WDFDEVICE d, LARGE_INTEGER id, WDFIOTARGET *t) {
    (void)d; (void)id; *t = (WDFIOTARGET)1; return STATUS_SUCCESS;
}
static NTSTATUS SwPwrReadReg(WDFIOTARGET t, UCHAR reg, PUCHAR val) {
    assert(t != NULL);
    if (fail_i2c_read) return STATUS_IO_TIMEOUT;
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
static void WdfTimerStart(WDFTIMER t, unsigned ms) { (void)t; (void)ms; timer_starts++; }
static BOOLEAN WdfTimerStop(WDFTIMER t, BOOLEAN wait) { (void)t; (void)wait; timer_stops++; return TRUE; }
#define READ_REGISTER_ULONG(p) (*(p))
#define WRITE_REGISTER_ULONG(p, v) (*(p) = (v))
'''

funcs = "".join(function(n) for n in [
    "SwPwrDisplayChanged",
    "SwPwrPressReport",
    "SwPwrSetIrq",
    "SwPwrEvtD0Entry",
    "SwPwrEvtD0Exit",
    "SwPwrFlushReportTraces",
    "SwPwrReport",
    "SwPwrEvtIsr",
    "SwPwrEvtInterruptWorkItem",
    "SwPwrEvtSuspend",
    "SwPwrEvtRestart",
])

tests = r'''
static void reset_env(PSWPWR_DEVICE d, bool irq_mode) {
    memset(d, 0, sizeof(*d));
    memset(pmic, 0, sizeof(pmic));
    pmc_cntrl = 0x00000100u; /* unrelated PMC bits set, bit 17 initially 0 */
    d->Pmc = &pmc_cntrl;
    d->I2c = (WDFIOTARGET)1;
    d->Vhf = (VHFHANDLE)1;
    d->Timer = (WDFTIMER)1;
    d->Interrupt = irq_mode ? (WDFINTERRUPT)d : NULL;
    /* Unrelated PMIC interrupt masks must remain untouched! */
    pmic[MAX77620_IRQTOPM]   = 0xFDu; /* bit 1 ONOFF masked initially, others 1 */
    pmic[MAX77620_INTENLBT]  = 0xAAu | 0x01u;
    pmic[MAX77620_ONOFFIRQM] = 0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F;
    pmic[MAX77620_ONOFFIRQ]  = ONOFFIRQ_EN0_R; /* stale boot edge to clear on first arm */
    fail_i2c_read = fail_i2c_write = false;
    fail_write_after = write_count = 0;
    sys_action = PowerActionNone;
    in_d0_or_isr = false;
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

    /* 2. Press with display ON -> SWPWR_POWER + 0 pulse! */
    ULONG disp_on = 1, disp_off = 0;
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_on, 4, &d) == STATUS_SUCCESS);
    pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
    in_d0_or_isr = true;
    assert(SwPwrEvtIsr(&d, 0) == TRUE);
    in_d0_or_isr = false;
    assert(emitted_count == 2 && emitted_reports[0] == SWPWR_POWER && emitted_reports[1] == 0);
    assert(workitem_queued == 1 && d.PowerReports == 1 && d.WakeReports == 0);
    SwPwrEvtInterruptWorkItem(&d, NULL);
    assert(traced_power == 1 && traced_ready == 1);

    /* 3. Repeated S0-idle D3 / SC7 sleep & wake cycles:
     *    - D0Exit(D3, Sleep) keeps PMIC wake armed!
     *    - SC7 wipes PMC_CNTRL bit 17 and latches ONOFFIRQ_EN0_R on button press!
     *    - D0Entry restores PMC_INTR_POLARITY WITHOUT clearing latched ONOFFIRQ!
     *    - Passive ISR consumes ONOFFIRQ_EN0_R while DisplayOff==1 and emits SWPWR_WAKE + 0! */
    assert(SwPwrDisplayChanged(&SwPwrDisplayGuid, &disp_off, 4, &d) == STATUS_SUCCESS);
    for (unsigned cycle = 0; cycle < 3; ++cycle) {
        sys_action = PowerActionSleep;
        assert(SwPwrEvtSuspend(&d) == STATUS_SUCCESS);
        assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
        assert(d.IrqConfigured && (pmic[MAX77620_IRQTOPM] & 0x02u) == 0);

        /* SC7 resets PMC_CNTRL polarity and user presses Power to wake! */
        pmc_cntrl = 0;
        pmic[MAX77620_ONOFFIRQ] = ONOFFIRQ_EN0_R;
        emitted_count = 0;

        in_d0_or_isr = true;
        assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
        assert((pmc_cntrl & PMC_INTR_POLARITY) != 0);
        assert(pmic[MAX77620_ONOFFIRQ] == ONOFFIRQ_EN0_R); /* NOT lost before ISR! */
        assert(SwPwrEvtIsr(&d, 0) == TRUE);
        in_d0_or_isr = false;
        assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 0);

        assert(emitted_count == 2 && emitted_reports[0] == SWPWR_WAKE && emitted_reports[1] == 0);
        assert(d.WakeReports == (LONG)(cycle + 1));
    }

    /* 4. I2C read fault in ISR masks IRQ route to prevent IRQ storm, and next D0Entry re-arms! */
    fail_i2c_read = true;
    in_d0_or_isr = true;
    assert(SwPwrEvtIsr(&d, 0) == TRUE);
    in_d0_or_isr = false;
    /* Even if read fails during SwPwrSetIrq(FALSE) too, test when read fails only on ONOFFIRQ: */
    fail_i2c_read = false;
    assert(SwPwrSetIrq(&d, FALSE) == STATUS_SUCCESS && !d.IrqConfigured);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && d.IrqConfigured);

    /* 5. Mid-configuration write fault in SwPwrSetIrq(TRUE) rolls back original masks! */
    reset_env(&d, true);
    fail_write_after = 3;
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_IO_TIMEOUT && !d.IrqConfigured);

    /* 6. Shutdown / D3Final restores original PMIC masks cleanly. */
    reset_env(&d, true);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && d.IrqConfigured);
    sys_action = PowerActionShutdown;
    assert(SwPwrEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.IrqConfigured);
    assert(pmic[MAX77620_IRQTOPM] == 0xFDu);
    assert(pmic[MAX77620_ONOFFIRQM] == (0xF0u | ONOFFIRQ_EN0_R | ONOFFIRQ_EN0_F));

    /* 7. Legacy firmware without Interrupt resource keeps 50 ms polling fallback intact. */
    reset_env(&d, false);
    assert(SwPwrEvtD0Entry(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.IrqConfigured);
    assert(SwPwrEvtRestart(&d) == STATUS_SUCCESS && timer_starts == 1);

    puts("PASS: SwitchPowerButton PMU_EXT wake IRQ, PMC polarity, SC7 latched-wake preservation, rollback and polling fallback");
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
