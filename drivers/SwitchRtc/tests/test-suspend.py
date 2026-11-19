#!/usr/bin/env python3
"""Exercise SwitchRtc boot restore, S0 idle preservation, confirmed resume resync, and cleanup."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/switchrtc.c").read_text()

def function(name):
    m = re.search(r"^(?:static\s+)?(?:VOID|UCHAR|BOOLEAN|LONGLONG|NTSTATUS)\s+" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
    assert m, f"missing {name}"
    end = source.index("{", m.start()) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[m.start():end] + "\n"

defines = "\n".join(re.findall(r"^#define (?:RTC_|TICKS_PER_SECOND|OFFSET_FORMAT)[^\n]+", source, re.M))

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

typedef uint32_t ULONG;
typedef uint8_t UCHAR, BOOLEAN;
typedef int16_t CSHORT;
typedef int32_t LONG, NTSTATUS;
typedef int64_t LONGLONG;
typedef uint64_t ULONGLONG;
typedef void VOID, *PVOID, *WDFIOTARGET, *WDFWORKITEM, *WDFDEVICE, *PCALLBACK_OBJECT;
typedef union { int64_t QuadPart; struct { uint32_t LowPart; int32_t HighPart; }; } LARGE_INTEGER, *PLARGE_INTEGER;
typedef struct { const wchar_t *Buffer; } UNICODE_STRING;
typedef struct { int unused; } OBJECT_ATTRIBUTES;
typedef struct { CSHORT Year, Month, Day, Hour, Minute, Second, Milliseconds, Weekday; } TIME_FIELDS;
typedef struct { int dummy; } SW_DISPLAY_IDLE;
typedef enum {
    PowerActionNone = 0,
    PowerActionReserved,
    PowerActionSleep,
    PowerActionHibernate,
    PowerActionShutdown,
    PowerActionShutdownReset,
    PowerActionShutdownOff,
    PowerActionWarmEject,
    PowerActionDisplayOff
} POWER_ACTION;

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_INVALID_PARAMETER -1
#define STATUS_IO_TIMEOUT -2
#define KernelMode 0
#define OBJ_CASE_INSENSITIVE 1
#define OBJ_KERNEL_HANDLE 2
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define RtlZeroMemory(p, n) memset((p), 0, (n))
#define RtlInitUnicodeString(u, s) ((u)->Buffer = (s))
#define InitializeObjectAttributes(a, n, f, r, s) ((void)(a), (void)(n), (void)(f), (void)(r), (void)(s))
''' + defines + r'''

typedef struct _SWRTC_DEVICE {
    LARGE_INTEGER I2cId;
    WDFIOTARGET I2c;
    WDFWORKITEM LearnWork;
    PCALLBACK_OBJECT TimeCallbackObject;
    PVOID TimeCallback;
    LONG Learns;
    LONG Suspended;
    LONG SuppressTimeCallback;
    LONG PendingLearn;
    BOOLEAN SuspendSnapshotValid;
    BOOLEAN SuspendRtcValid;
    BOOLEAN SuspendSystemSleep;
    LARGE_INTEGER SuspendRtc;
    LARGE_INTEGER SuspendSystemTime;
    ULONGLONG SuspendUnbiased;
    SW_DISPLAY_IDLE DisplayIdle;
} SWRTC_DEVICE, *PSWRTC_DEVICE;

typedef struct { WDFDEVICE Device; } SWRTC_WORK, *PSWRTC_WORK;

static SWRTC_WORK work_ctx;
static PSWRTC_DEVICE SwRtcGetDevice(WDFDEVICE d) { return (PSWRTC_DEVICE)d; }
static PSWRTC_WORK SwRtcGetWork(WDFWORKITEM w) { (void)w; return &work_ctx; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }
static LONG InterlockedCompareExchange(volatile LONG *p, LONG ex, LONG cmp) {
    LONG o = *p;
    if (o == cmp) *p = ex;
    return o;
}

static LONGLONG rtc_seconds = 1000000;
static LONGLONG sys_seconds = 1000000;
static ULONGLONG unbiased_seconds = 1000;
static LONGLONG saved_offset = 0;
static bool has_offset = false, fail_i2c = false, in_d3 = false;
static POWER_ACTION power_action = PowerActionNone;
static void (*registered_cb)(PVOID, PVOID, PVOID) = NULL;
static PVOID registered_ctx = NULL;
static unsigned work_queued = 0, work_flushed = 0, set_time_calls = 0;
static unsigned display_idle_stops = 0, i2c_closes = 0, obj_deletes = 0;
static ULONG last_restore_result = 0;

static void SwRtcTrace(const wchar_t *n, ULONG v) {
    if (!wcscmp(n, L"RestoreResult")) last_restore_result = v;
}
static void SwRtcTrace64(const wchar_t *n, LONGLONG v) {
    if (!wcscmp(n, L"OffsetSeconds")) { saved_offset = v; has_offset = true; }
}
static BOOLEAN SwRtcReadOffset(LONGLONG *out) {
    if (!has_offset) return FALSE;
    *out = saved_offset;
    return TRUE;
}
static NTSTATUS SwRtcWrite(WDFIOTARGET t, UCHAR reg, UCHAR val) {
    assert(t != NULL && !in_d3 && reg == RTC_UPDATE0 && val == RTC_READ_UPDATE);
    return fail_i2c ? STATUS_IO_TIMEOUT : STATUS_SUCCESS;
}
static NTSTATUS SwRtcRead(WDFIOTARGET t, UCHAR reg, UCHAR *buf, ULONG len) {
    assert(t != NULL && !in_d3);
    if (fail_i2c) return STATUS_IO_TIMEOUT;
    if (reg == RTC_CONTROL && len == 1) {
        *buf = RTC_24H; /* binary 24h mode */
        return STATUS_SUCCESS;
    }
    assert(reg == RTC_SEC && len == 7);
    LONGLONG s = rtc_seconds;
    buf[0] = (UCHAR)(s % 60); s /= 60;
    buf[1] = (UCHAR)(s % 60); s /= 60;
    buf[2] = (UCHAR)(s % 24); s /= 24;
    buf[3] = 1;
    buf[4] = 11;
    buf[5] = 26;
    buf[6] = (UCHAR)(1 + (s % 28));
    return STATUS_SUCCESS;
}
static BOOLEAN RtlTimeFieldsToTime(TIME_FIELDS *f, PLARGE_INTEGER out) {
    LONGLONG total = (((LONGLONG)(f->Day - 1) * 24 + f->Hour) * 60 + f->Minute) * 60 + f->Second;
    out->QuadPart = total * TICKS_PER_SECOND;
    return TRUE;
}
static void KeDelayExecutionThread(int m, BOOLEAN a, PLARGE_INTEGER d) { (void)m; (void)a; (void)d; }
static void KeQuerySystemTime(PLARGE_INTEGER now) { now->QuadPart = sys_seconds * TICKS_PER_SECOND; }
static NTSTATUS ZwSetSystemTime(PLARGE_INTEGER new_time, PLARGE_INTEGER old_time) {
    (void)old_time;
    sys_seconds = new_time->QuadPart / TICKS_PER_SECOND;
    set_time_calls++;
    /* Windows fires \Callback\SetSystemTime synchronously inside ZwSetSystemTime! */
    if (registered_cb != NULL) registered_cb(registered_ctx, NULL, NULL);
    return STATUS_SUCCESS;
}
static void WdfWorkItemEnqueue(WDFWORKITEM w) { (void)w; work_queued++; }
static void WdfWorkItemFlush(WDFWORKITEM w) {
    work_flushed++;
    if (work_queued) {
        work_queued = 0;
        void SwRtcEvtLearnWork(WDFWORKITEM);
        SwRtcEvtLearnWork(w);
    }
}
static NTSTATUS ExCreateCallback(PCALLBACK_OBJECT *obj, OBJECT_ATTRIBUTES *a, BOOLEAN c, BOOLEAN m) {
    (void)a; (void)c; (void)m; *obj = (PCALLBACK_OBJECT)1; return STATUS_SUCCESS;
}
static PVOID ExRegisterCallback(PCALLBACK_OBJECT obj, void (*fn)(PVOID, PVOID, PVOID), PVOID ctx) {
    assert(obj != NULL && registered_cb == NULL);
    registered_cb = fn; registered_ctx = ctx;
    return (PVOID)1;
}
static void ExUnregisterCallback(PVOID cb) { assert(cb == (PVOID)1 && registered_cb != NULL); registered_cb = NULL; }
static void ObDereferenceObject(PVOID o) { assert(o == (PCALLBACK_OBJECT)1); }
static ULONGLONG KeQueryUnbiasedInterruptTime(void) { return unbiased_seconds * (ULONGLONG)TICKS_PER_SECOND; }
static POWER_ACTION WdfDeviceGetSystemPowerAction(WDFDEVICE d) { (void)d; return power_action; }
static void SwDisplayIdleStop(SW_DISPLAY_IDLE *idle) { (void)idle; display_idle_stops++; }
static void WdfIoTargetClose(WDFIOTARGET t) { assert(t != NULL); i2c_closes++; }
static void WdfObjectDelete(PVOID o) { assert(o != NULL); obj_deletes++; }
'''

funcs = "".join(function(n) for n in [
    "SwRtcBcd",
    "SwRtcReadTime",
    "SwRtcRoundSeconds",
    "SwRtcIsSystemSleepAction",
    "SwRtcStoreOffset",
    "SwRtcEvtLearnWork",
    "SwRtcTimeChanged",
    "SwRtcApplyRtcTime",
    "SwRtcRestoreTime",
    "SwRtcRegisterCallback",
    "SwRtcUnregisterCallback",
    "SwRtcEvtSuspend",
    "SwRtcEvtRestart",
    "SwRtcEvtSelfManagedIoCleanup",
])

tests = r'''
int main(void) {
    SWRTC_DEVICE d;
    memset(&d, 0, sizeof(d));
    d.I2c = (WDFIOTARGET)1;
    d.LearnWork = (WDFWORKITEM)1;
    work_ctx.Device = &d;

    /* 1. Boot with no offset skips restore; initial sync learns +3600s; boot restore corrects stale UEFI time. */
    rtc_seconds = 10 * 3600;
    sys_seconds = 11 * 3600;
    unbiased_seconds = 1000;
    SwRtcRestoreTime(&d);
    assert(last_restore_result == 1 && set_time_calls == 0);

    SwRtcRegisterCallback(&d);
    assert(registered_cb != NULL);
    registered_cb(registered_ctx, NULL, NULL);
    assert(work_queued == 1);
    SwRtcEvtLearnWork(d.LearnWork);
    work_queued = 0;
    assert(has_offset && saved_offset == 3600 && d.Learns == 1);

    sys_seconds = 1000;
    LONG learns_before = d.Learns;
    SwRtcRestoreTime(&d);
    assert(set_time_calls == 1 && last_restore_result == 3 && sys_seconds == rtc_seconds + 3600);
    assert(work_queued == 0 && d.PendingLearn == 0 && d.Learns == learns_before);

    /* 2. Ordinary S0 screen-off idle (with 1s RTC quantization jitter) must NOT overwrite system time. */
    unsigned sets_before = set_time_calls;
    learns_before = d.Learns;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    assert(d.Suspended == 1 && registered_cb != NULL && d.SuspendSnapshotValid && d.SuspendRtcValid && !d.SuspendSystemSleep);
    in_d3 = true;
    rtc_seconds += 121;
    sys_seconds += 120;
    unbiased_seconds += 120;
    in_d3 = false;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 4);
    assert(sys_seconds == 11 * 3600 + 120 && saved_offset == 3600 && d.Learns == learns_before);
    assert(!d.Suspended && !d.SuspendSnapshotValid);

    /* 3. Ordinary S0 idle with NTP/user wall-clock update preserves the update, avoids I2C in D3, and learns offset.
     *    PowerActionDisplayOff must still be treated as S0 idle, not system sleep. */
    power_action = PowerActionDisplayOff;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS && !d.SuspendSystemSleep);
    in_d3 = true;
    rtc_seconds += 30;
    unbiased_seconds += 30;
    sys_seconds += 30 + 180;
    registered_cb(registered_ctx, NULL, NULL);
    assert(d.PendingLearn == 1 && work_queued == 0);
    /* Late LearnWork arriving while suspended must defer without touching I2C. */
    SwRtcEvtLearnWork(d.LearnWork);
    assert(d.PendingLearn == 1);
    in_d3 = false;
    sets_before = set_time_calls;
    learns_before = d.Learns;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 4);
    assert(sys_seconds == 11 * 3600 + 330 && saved_offset == sys_seconds - rtc_seconds);
    assert(d.Learns == learns_before + 1 && d.PendingLearn == 0 && !d.Suspended);
    power_action = PowerActionNone;

    /* Wall-clock shift during S0 idle without callback firing still preserves wall clock and updates offset. */
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    in_d3 = true;
    rtc_seconds += 20;
    unbiased_seconds += 20;
    sys_seconds += 20 - 50;
    in_d3 = false;
    sets_before = set_time_calls;
    learns_before = d.Learns;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 4);
    assert(saved_offset == sys_seconds - rtc_seconds && d.Learns == learns_before + 1);

    /* 4. Repeated confirmed low-power pause cycles (PowerActionNone, unbiased time frozen while RTC advances +900s). */
    saved_offset = 3600;
    sys_seconds = rtc_seconds + 3600;
    for (unsigned cycle = 0; cycle < 4; ++cycle) {
        /* Queue a pending learn before suspend to prove Flush drains it in D0 before entering D3. */
        registered_cb(registered_ctx, NULL, NULL);
        unsigned flushes_before = work_flushed;
        assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
        assert(d.Suspended == 1 && work_queued == 0 && work_flushed == flushes_before + 2);

        in_d3 = true;
        rtc_seconds += 900;
        in_d3 = false;
        learns_before = d.Learns;
        sets_before = set_time_calls;

        assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
        assert(set_time_calls == sets_before + 1 && last_restore_result == 3);
        assert(sys_seconds == rtc_seconds + 3600);
        /* ZwSetSystemTime during resume must not self-trigger LearnWork or PendingLearn. */
        assert(work_queued == 0 && d.PendingLearn == 0 && d.Learns == learns_before);
        assert(!d.Suspended && registered_cb != NULL && d.TimeCallback != NULL);
    }

    /* 5. Mixed S0 idle (60s + NTP +120s) followed by confirmed low-power pause (900s) preserves both updates. */
    LONGLONG expected_offset = saved_offset + 120;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    in_d3 = true;
    rtc_seconds += 60;
    unbiased_seconds += 60;
    sys_seconds += 60 + 120;
    registered_cb(registered_ctx, NULL, NULL);
    assert(d.PendingLearn == 1 && work_queued == 0);
    rtc_seconds += 900;
    in_d3 = false;
    sets_before = set_time_calls;
    learns_before = d.Learns;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before + 1 && last_restore_result == 3);
    assert(saved_offset == expected_offset && sys_seconds == rtc_seconds + expected_offset);
    assert(d.Learns == learns_before + 1 && work_queued == 0 && d.PendingLearn == 0);

    /* 6. Confirmed legacy system sleep (PowerActionSleep) restores from RTC even if HAL clobbered system time
     *    and skips ZwSetSystemTime when drift <= 2s. */
    power_action = PowerActionSleep;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS && d.SuspendSystemSleep);
    in_d3 = true;
    rtc_seconds += 10;
    sys_seconds = 500;
    registered_cb(registered_ctx, NULL, NULL);
    in_d3 = false;
    sets_before = set_time_calls;
    learns_before = d.Learns;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before + 1 && last_restore_result == 3);
    assert(saved_offset == expected_offset && sys_seconds == rtc_seconds + expected_offset);
    assert(d.Learns == learns_before && d.PendingLearn == 0);

    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    in_d3 = true;
    rtc_seconds += 1;
    in_d3 = false;
    sets_before = set_time_calls;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 2);
    power_action = PowerActionNone;

    /* 7. Generic D0 restart without a suspend snapshot or system sleep action does not assert SC7. */
    sys_seconds = rtc_seconds + expected_offset;
    LONGLONG expected_sys = sys_seconds + 300;
    sys_seconds = expected_sys;
    sets_before = set_time_calls;
    assert(!d.SuspendSnapshotValid && SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 4 && sys_seconds == expected_sys);

    /* 8. I2C fault during restart preserves offset; I2C fault during suspend still recovers across confirmed pause. */
    sys_seconds = rtc_seconds + expected_offset;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    in_d3 = true;
    rtc_seconds += 600;
    in_d3 = false;
    fail_i2c = true;
    sets_before = set_time_calls;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && saved_offset == expected_offset && !d.Suspended);
    fail_i2c = false;

    sys_seconds = rtc_seconds + expected_offset;
    fail_i2c = true;
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS && d.SuspendSnapshotValid && !d.SuspendRtcValid);
    fail_i2c = false;
    in_d3 = true;
    rtc_seconds += 600;
    in_d3 = false;
    sets_before = set_time_calls;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before + 1 && last_restore_result == 3 && sys_seconds == rtc_seconds + expected_offset);

    /* 9. Self-managed I/O cleanup unregisters callback, drains work, stops display idle, and closes I2C target. */
    SwRtcEvtSelfManagedIoCleanup(&d);
    assert(display_idle_stops == 1 && d.Suspended == 1);
    assert(registered_cb == NULL && d.TimeCallback == NULL && d.TimeCallbackObject == NULL);
    assert(d.I2c == NULL && i2c_closes == 1 && obj_deletes == 1);

    puts("PASS: SwitchRtc boot restore, S0 idle wall-clock preservation, confirmed resume resync, I2C fault handling and cleanup");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="swrtc-sc7-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(prefix + funcs + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
