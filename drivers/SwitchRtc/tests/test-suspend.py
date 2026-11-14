#!/usr/bin/env python3
"""Exercise SwitchRtc SC7 wall-clock resync ordering and SetSystemTime callback detach/reattach."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/switchrtc.c").read_text()

def function(name):
    m = re.search(r"^(?:static\s+)?(?:VOID|UCHAR|BOOLEAN|NTSTATUS)\s+" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
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
typedef void VOID, *PVOID, *WDFIOTARGET, *WDFWORKITEM, *WDFDEVICE, *PCALLBACK_OBJECT;
typedef union { int64_t QuadPart; struct { uint32_t LowPart; int32_t HighPart; }; } LARGE_INTEGER, *PLARGE_INTEGER;
typedef struct { const wchar_t *Buffer; } UNICODE_STRING;
typedef struct { int unused; } OBJECT_ATTRIBUTES;
typedef struct { CSHORT Year, Month, Day, Hour, Minute, Second, Milliseconds, Weekday; } TIME_FIELDS;
typedef struct { int dummy; } SW_DISPLAY_IDLE;

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
    SW_DISPLAY_IDLE DisplayIdle;
    LONG Learns;
} SWRTC_DEVICE, *PSWRTC_DEVICE;

typedef struct { WDFDEVICE Device; } SWRTC_WORK, *PSWRTC_WORK;

static SWRTC_WORK work_ctx;
static PSWRTC_DEVICE SwRtcGetDevice(WDFDEVICE d) { return (PSWRTC_DEVICE)d; }
static PSWRTC_WORK SwRtcGetWork(WDFWORKITEM w) { (void)w; return &work_ctx; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }

static LONGLONG rtc_seconds = 1000000;
static LONGLONG sys_seconds = 1000000;
static LONGLONG saved_offset = 0;
static bool has_offset = false, fail_i2c = false;
static void (*registered_cb)(PVOID, PVOID, PVOID) = NULL;
static PVOID registered_ctx = NULL;
static unsigned work_queued = 0, work_flushed = 0, set_time_calls = 0;
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
    assert(t != NULL && reg == RTC_UPDATE0 && val == RTC_READ_UPDATE);
    return fail_i2c ? STATUS_IO_TIMEOUT : STATUS_SUCCESS;
}
static NTSTATUS SwRtcRead(WDFIOTARGET t, UCHAR reg, UCHAR *buf, ULONG len) {
    assert(t != NULL);
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
'''

funcs = "".join(function(n) for n in [
    "SwRtcBcd",
    "SwRtcReadTime",
    "SwRtcEvtLearnWork",
    "SwRtcTimeChanged",
    "SwRtcRestoreTime",
    "SwRtcRegisterCallback",
    "SwRtcEvtSuspend",
    "SwRtcEvtRestart",
])

tests = r'''
int main(void) {
    SWRTC_DEVICE d;
    memset(&d, 0, sizeof(d));
    d.I2c = (WDFIOTARGET)1;
    d.LearnWork = (WDFWORKITEM)1;
    work_ctx.Device = &d;

    /* 1. Initial time sync learns +3600s offset via \Callback\SetSystemTime. */
    rtc_seconds = 10 * 3600;
    sys_seconds = 11 * 3600;
    SwRtcRegisterCallback(&d);
    assert(registered_cb != NULL);
    registered_cb(registered_ctx, NULL, NULL);
    assert(work_queued == 1);
    SwRtcEvtLearnWork(d.LearnWork);
    work_queued = 0;
    assert(has_offset && saved_offset == 3600 && d.Learns == 1);

    /* 2. Repeated SC7 cycles where SoC timer stops while MAX77620 RTC advances!
     *    - SwRtcEvtSuspend unregisters callback AND flushes any queued LearnWork!
     *    - SC7 sleeps 900 seconds: RTC advances +900s, Windows sys_seconds stays frozen!
     *    - SwRtcEvtRestart calls SwRtcRestoreTime BEFORE re-registering callback,
     *      so ZwSetSystemTime updates sys_seconds WITHOUT self-triggering LearnWork! */
    for (unsigned cycle = 0; cycle < 4; ++cycle) {
        /* Queue a pending learn before suspend to prove Flush drains it cleanly */
        registered_cb(registered_ctx, NULL, NULL);
        assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
        assert(registered_cb == NULL && d.TimeCallback == NULL && d.TimeCallbackObject == NULL);
        assert(work_queued == 0 && work_flushed == cycle + 1);

        /* 900 seconds spent in SC7 deep sleep */
        rtc_seconds += 900;
        LONG learns_before = d.Learns;
        unsigned sets_before = set_time_calls;

        assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
        assert(set_time_calls == sets_before + 1);
        assert(last_restore_result == 3);
        assert(sys_seconds == rtc_seconds + 3600);
        /* Crucial invariant: ZwSetSystemTime during resume did NOT queue LearnWork! */
        assert(work_queued == 0 && d.Learns == learns_before);
        /* And callback is re-registered afterward for normal NTP updates! */
        assert(registered_cb != NULL && d.TimeCallback != NULL);
    }

    /* 3. Small drift (<= 2s) skips ZwSetSystemTime; I2C fault during resume does not corrupt offset. */
    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    rtc_seconds += 1;
    unsigned sets_before = set_time_calls;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && last_restore_result == 2);

    assert(SwRtcEvtSuspend(&d) == STATUS_SUCCESS);
    fail_i2c = true;
    rtc_seconds += 600;
    assert(SwRtcEvtRestart(&d) == STATUS_SUCCESS);
    assert(set_time_calls == sets_before && saved_offset == 3600);

    puts("PASS: SwitchRtc SC7 wall-clock resync, callback detach-before-restore ordering and I2C fault preservation");
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
