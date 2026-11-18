/* SPDX-License-Identifier: GPL-2.0-only
 * WDF and I2C stand-ins for the production button poll and RTC learning callbacks.
 * The runner includes the actual driver functions from the selected source tree.
 */

#include "board_registers.inc"
#include "button_state.h"
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
typedef void VOID;
typedef int32_t NTSTATUS, LONG;
typedef uint32_t ULONG;
typedef int KIRQL;
typedef unsigned char UCHAR, BOOLEAN;
typedef void *WDFIOTARGET;
#define TRUE 1
#define FALSE 0
#define MAXULONG UINT32_MAX
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS ((NTSTATUS)0)
#define DATA_TIMEOUT ((NTSTATUS)(uint32_t)0xc00000b5)
#define WDF_REL_TIMEOUT_IN_MS(x) (x)
typedef struct power_device
{
    struct swpwr_button_state Button;
    BOOLEAN PendingPress, PendingDisplayOff, Stopping;
    LONG DisplayOff, TimerLock, Polls, Presses;
    ULONG Failures;
    void *I2c, *Vhf, *Timer;
} power_device, *PSWPWR_DEVICE, *WDFDEVICE;
typedef struct
{
    WDFDEVICE Device;
} timer, *WDFTIMER;
#define SwPwrGetDevice(d) (d)
#define SwPwrGetTimer(t) (t)
static void KeAcquireSpinLock(LONG *lock, KIRQL *irql)
{
    (void)lock;
    *irql = 0;
}
static void KeReleaseSpinLock(LONG *lock, KIRQL irql)
{
    (void)lock;
    (void)irql;
}
static void WdfTimerStart(void *t, int delay)
{
    (void)t;
    (void)delay;
}
static void WdfTimerStop(void *t, int wait)
{
    (void)t;
    (void)wait;
}
static LONG InterlockedCompareExchange(LONG *p, LONG n, LONG expected)
{
    LONG old = *p;
    if (old == expected)
        *p = n;
    return old;
}
static LONG InterlockedIncrement(LONG *p)
{
    return ++*p;
}
static NTSTATUS irq_status, level_status;
static UCHAR irq_bits, level_bits;
static int irq_reads, level_reads;
static ULONG boot_irq, boot_status, boot_level_status, last_level;
static UCHAR reports[32];
static size_t report_count;
static NTSTATUS SwPwrReadReg(void *target, UCHAR which, UCHAR *out)
{
    (void)target;
    if (which == MAX77620_ONOFFIRQ)
    {
        irq_reads++;
        if (NT_SUCCESS(irq_status))
        {
            *out = irq_bits;
            irq_bits = 0;
        }
        return irq_status;
    }
    if (which == MAX77620_ONOFFSTAT)
    {
        level_reads++;
        if (NT_SUCCESS(level_status))
            *out = level_bits;
        return level_status;
    }
    return DATA_TIMEOUT;
}
static void SwPwrTrace(const wchar_t *key, ULONG value)
{
    if (!wcscmp(key, L"OnOffIrqAtStart"))
        boot_irq = value;
    else if (!wcscmp(key, L"BootIrqReadStatus"))
        boot_status = value;
    else if (!wcscmp(key, L"FirstReadStatus"))
        boot_level_status = value;
    else if (!wcscmp(key, L"LastOnOffStat"))
        last_level = value;
}
static void SwPwrReport(PSWPWR_DEVICE d, UCHAR buttons)
{
    (void)d;
    if (report_count < sizeof(reports))
        reports[report_count++] = buttons;
}

typedef int64_t LONGLONG;
typedef struct
{
    int64_t QuadPart;
} LARGE_INTEGER;
typedef struct
{
    LONG Learns;
} rtc_device, *PSWRTC_DEVICE;
typedef struct
{
    rtc_device *Device;
} rtc_work, *WDFWORKITEM;
#define SwRtcGetDevice(d) (d)
#define SwRtcGetWork(w) (w)
static int64_t rtc_ticks, system_ticks, saved_offset;
static unsigned offset_writes, format_writes;
static NTSTATUS rtc_result;
static NTSTATUS SwRtcReadTime(PSWRTC_DEVICE dev, LARGE_INTEGER *v)
{
    (void)dev;
    if (NT_SUCCESS(rtc_result))
        v->QuadPart = rtc_ticks;
    return rtc_result;
}
static void KeQuerySystemTime(LARGE_INTEGER *v)
{
    v->QuadPart = system_ticks;
}
static void SwRtcTrace(const wchar_t *s, ULONG v)
{
    if (!wcscmp(s, L"OffsetFormat") && v == OFFSET_FORMAT)
        format_writes++;
}
static void SwRtcTrace64(const wchar_t *s, int64_t t)
{
    if (!wcscmp(s, L"OffsetSeconds"))
    {
        saved_offset = t;
        offset_writes++;
    }
}

#include "board_callbacks.inc"

static power_device dev;
static timer t;
static void reset(void)
{
    memset(&dev, 0, sizeof(dev));
    t.Device = &dev;
    dev.Timer = &t;
    dev.Vhf = &dev;
    dev.I2c = &dev;
    report_count = 0;
    irq_reads = level_reads = 0;
    irq_bits = level_bits = 0;
    irq_status = level_status = 0;
    boot_irq = 0xa5;
    boot_status = 0xabcdef00;
    boot_level_status = MAXULONG;
    last_level = 0x52;
}
static void tick(UCHAR ir, UCHAR lv, NTSTATUS rs, NTSTATUS ss, int off)
{
    irq_bits |= ir;
    level_bits = lv;
    irq_status = rs;
    level_status = ss;
    dev.DisplayOff = off;
    SwPwrEvtTimer(&t);
}
static bool pair(uint8_t kind)
{
    return report_count == 2 && reports[0] == kind && reports[1] == 0;
}
static unsigned total, good;
static void verdict(const char *group, const char *name, bool pass, int64_t observed,
                    int64_t expected)
{
    printf("%s,%s,%d,%" PRId64 ",%" PRId64 "\n", group, name, (int)pass, observed, expected);
    total++;
    good += (unsigned)pass;
}
static void button_tests(void)
{
    reset();
    tick(8, 4, 0, 0, 0);
    tick(0, 4, 0, 0, 1);
    tick(0, 0, 0, 0, 1);
    verdict("button", "held_no_duplicate", pair(SWPWR_POWER), report_count, 2);
    verdict("button", "two_transactions_per_valid_poll", irq_reads == 3 && level_reads == 3,
            irq_reads + level_reads, 6);
    reset();
    tick(8, 0, 0, 0, 1);
    tick(0, 0, 0, 0, 1);
    verdict("button", "short_wake", pair(SWPWR_WAKE), report_count, 2);
    reset();
    tick(8, 0, 0, DATA_TIMEOUT, 0);
    tick(0, 0, 0, 0, 0);
    tick(0, 0, 0, 0, 0);
    verdict("button", "level_read_failed_after_edge", pair(SWPWR_POWER), report_count, 2);
    reset();
    tick(8, 0, 0, DATA_TIMEOUT, 1);
    tick(0, 0, 0, 0, 0);
    verdict("button", "screen_woke_during_read_retry", pair(SWPWR_WAKE),
            report_count ? reports[0] : 0, SWPWR_WAKE);
    reset();
    tick(8, 0, 0, DATA_TIMEOUT, 1);
    tick(0, 4, DATA_TIMEOUT, 0, 0);
    tick(0, 4, 0, DATA_TIMEOUT, 0);
    tick(0, 4, 0, 0, 0);
    tick(0, 4, 0, 0, 1);
    tick(0, 0, 0, 0, 1);
    verdict("button", "multiple_failures_then_held", pair(SWPWR_WAKE),
            report_count ? reports[0] : 0, SWPWR_WAKE);
    reset();
    tick(0, 0, DATA_TIMEOUT, 0, 0);
    tick(0, 0, 0, 0, 1);
    verdict("button", "no_spurious_press_on_irq_failure",
            report_count == 0 && irq_reads == 2 && level_reads == 1, report_count, 0);
    reset();
    tick(8, 0, 0, DATA_TIMEOUT, 1);
    SwPwrEvtSuspend(&dev);
    irq_status = level_status = 0;
    level_bits = 0;
    NTSTATUS st = SwPwrEvtRestart(&dev);
    tick(0, 0, 0, 0, 0);
    verdict("button", "discard_pre_suspend_edge", st == 0 && report_count == 0, report_count, 0);
    reset();
    dev.Button.suppress_held = true;
    tick(8, 4, 0, DATA_TIMEOUT, 1);
    tick(0, 4, 0, 0, 1);
    tick(0, 0, 0, 0, 1);
    tick(8, 0, 0, 0, 0);
    verdict("button", "suppress_held_until_release", pair(SWPWR_POWER), report_count, 2);
    reset();
    dev.Polls = 199;
    tick(0, 0, DATA_TIMEOUT, 0, 0);
    verdict("button", "failed_periodic_sample_keeps_last_level", last_level == 0x52, last_level,
            0x52);
    reset();
    irq_bits = 0x40;
    level_bits = 2;
    st = initial_reads(&dev);
    verdict("button", "real_zero_status_valid_interrupt",
            st == 0 && boot_level_status == 0 && boot_irq == 0x40, boot_irq, 0x40);
    reset();
    irq_status = DATA_TIMEOUT;
    level_bits = 2;
    st = initial_reads(&dev);
    verdict("button", "unavailable_start_irq_recorded",
            st == 0 && boot_status == (ULONG)DATA_TIMEOUT && boot_irq == MAXULONG, boot_irq,
            MAXULONG);
}
static void time_tests(void)
{
    static const struct
    {
        const char *name;
        int64_t ticks, seconds;
    } inputs[] = {{"zero", 0, 0},
                  {"positive_under_half", 4999999, 0},
                  {"negative_under_half", -4999999, 0},
                  {"positive_half", 5000000, 1},
                  {"negative_half", -5000000, -1},
                  {"positive_one_second", 10000000, 1},
                  {"negative_one_second", -10000000, -1},
                  {"negative_one_and_half", -15000000, -2},
                  {"positive_one_and_half", 15000000, 2},
                  {"negative_whole_timezone", -28800LL * TICKS_PER_SECOND, -28800},
                  {"positive_whole_timezone", 28800LL * TICKS_PER_SECOND, 28800},
                  {"negative_day_fraction", -86400LL * TICKS_PER_SECOND - 6000001, -86401},
                  {"negative_below_whole", -30000001, -3},
                  {"negative_above_whole", -29999999, -3}};
    rtc_device rd = {0};
    rtc_work work = {&rd};
    for (size_t i = 0; i < sizeof(inputs) / sizeof(inputs[0]); i++)
    {
        rtc_ticks = 134395056000000000LL;
        system_ticks = rtc_ticks + inputs[i].ticks;
        rtc_result = 0;
        offset_writes = format_writes = 0;
        saved_offset = 77;
        SwRtcEvtLearnWork(&work);
        verdict("rtc", inputs[i].name,
                saved_offset == inputs[i].seconds && offset_writes == 1 && format_writes == 1,
                saved_offset, inputs[i].seconds);
    }
    rtc_result = DATA_TIMEOUT;
    offset_writes = format_writes = 0;
    saved_offset = 77;
    SwRtcEvtLearnWork(&work);
    verdict("rtc", "read_failure_preserves_offset",
            offset_writes == 0 && format_writes == 0 && saved_offset == 77, saved_offset, 77);
}
int main(void)
{
    button_tests();
    time_tests();
    printf("total,%u,matched,%u\n", total, good);
    return good == total ? 0 : 1;
}
