/* Host ASan/UBSan test of SwitchFan MAX77812 SPB serialization (PmicLock),
 * M1 voltage/enable safety bounds, and Switchroot mode transactions/readbacks. DFLL MMIO is a model, not hardware acceptance.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include <time.h>
#include "../src/t214/performance.h"

typedef uint32_t ULONG;
typedef uint8_t UCHAR;
typedef uint8_t *PUCHAR;
typedef uintptr_t ULONG_PTR;
typedef int32_t LONG;
typedef int32_t NTSTATUS;
typedef uint64_t ULONGLONG;
typedef int64_t LONGLONG;
typedef bool BOOLEAN;
typedef unsigned KIRQL;
typedef void VOID;
typedef void *PVOID;
typedef ULONG *PULONG;
typedef void *WDFDEVICE;
typedef void *WDFREQUEST;
typedef void *WDFWAITLOCK;
typedef union { int64_t QuadPart; struct { uint32_t LowPart; int32_t HighPart; }; } LARGE_INTEGER;
typedef struct { void *Buffer; size_t Size; } WDF_MEMORY_DESCRIPTOR;
typedef struct { int64_t Timeout; } WDF_REQUEST_SEND_OPTIONS;

#define TRUE true
#define FALSE false
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS                    0
#define STATUS_ACCESS_DENIED             -1
#define STATUS_INVALID_PARAMETER         -2
#define STATUS_NOT_SUPPORTED             -3
#define STATUS_DEVICE_NOT_READY          -4
#define STATUS_DEVICE_CONFIGURATION_ERROR -5
#define STATUS_INVALID_DEVICE_STATE      -6
#define STATUS_DEVICE_DATA_ERROR         -7
#define STATUS_DEVICE_PROTOCOL_ERROR     -8
#define STATUS_IO_TIMEOUT                -9
#define STATUS_DEVICE_HARDWARE_ERROR     -10
#define RTL_NUMBER_OF(a) (sizeof(a) / sizeof((a)[0]))
#define WDF_REQUEST_SEND_OPTION_TIMEOUT 1
#define WDF_REL_TIMEOUT_IN_MS(n) (n)
#define WDF_MEMORY_DESCRIPTOR_INIT_BUFFER(m, b, s) (*(m) = (WDF_MEMORY_DESCRIPTOR){ (b), (s) })
#define WDF_REQUEST_SEND_OPTIONS_INIT(o, f) ((void)(f), memset((o), 0, sizeof(*(o))))
#define RtlZeroMemory(p, n) memset((p), 0, (n))
#define KernelMode 0
#define UserMode   1
#define DISPATCH_LEVEL 2

static bool pmic_locked = false;
static bool bl_locked = false;
static unsigned pmic_lock_acquires = 0;
static int requestor_mode = KernelMode;
static bool fail_pmic_write = false;
static unsigned pmic_read_calls = 0;
static unsigned fail_pmic_read_at = 0;
static unsigned corrupt_pmic_read_at = 0;
static UCHAR corrupt_pmic_value = 0;
static bool fail_sensor_config = false;
static bool fail_sensor_read = false;
static UCHAR sensor_remote_temp = 41;
static UCHAR sensor_local_temp = 38;
static unsigned sensor_config_calls = 0;
static unsigned sensor_read_calls = 0;
static ULONGLONG mock_time = 10000000ULL;
static unsigned timer_starts = 0;
static unsigned last_timer_ms = 0;
static UCHAR pmic_regs[256];
static ULONG car_regs[64];
static char order_log[128];
static unsigned order_len = 0;
static pthread_mutex_t cpu_guard = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t step_guard = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t step_changed = PTHREAD_COND_INITIALIZER;
static bool pause_cpu_down, down_waiting, temperature_waiting, allow_down;
static _Thread_local bool cpu_locked, temperature_thread;

#include "../src/t214/dfll_windows_types.h"

typedef struct {
    UCHAR *Car;
    UCHAR *Pwm;
    LITE_PERFORMANCE Config;
    void *Pmic;
    void *Sensor;
    void *Timer;
    WDFWAITLOCK PmicLock;
    WDFWAITLOCK BacklightLock;
    LARGE_INTEGER PmicIds[2];
    ULONG ConnectionCount, BootPllx, BootCpuKhz;
    UCHAR BootVout;
    BOOLEAN IsLite, Stopping, BacklightReady;
    BOOLEAN CpuConfigured, CpuApplied, CpuThrottled, CpuOverclockEnabled;
    BOOLEAN BaseCpuOverclockEnabled, GpuBridgeSeen, TempValid;
    BOOLEAN ScreenOffIdle, SensorNeedsReinit, GpuRailOn;
    ULONG DisplayState, Duty, LastTemp;
    ULONG PowerMode, PolicySeq;
    volatile LONG GpuDvfsPolicy;
    ULONG GpuReportedKhz, GpuReportedUv, GpuStatusWord;
    SWFAN_DFLL Dfll;
    ULONGLONG GpuSampleTime;
    ULONGLONG GpuAbiTime;
    ULONGLONG TempSampleTime;
    ULONG SocTempC, BoardTempC;
} SWFAN_DEVICE, *PSWFAN_DEVICE;

struct mock_request {
    void *in;
    size_t in_len;
    void *out;
    size_t out_len;
};

#define SWFAN_HAVE_PMIC_LOCK 1
#define SWFAN_HAVE_CPU_LOCK 1
static VOID SwFanCpuLock(PSWFAN_DEVICE Dev)
{
    (void)Dev;
    assert(!cpu_locked);
    if (temperature_thread) {
        pthread_mutex_lock(&step_guard);
        temperature_waiting = true;
        pthread_cond_broadcast(&step_changed);
        pthread_mutex_unlock(&step_guard);
    }
    pthread_mutex_lock(&cpu_guard);
    cpu_locked = true;
}
static VOID SwFanCpuUnlock(PSWFAN_DEVICE Dev)
{
    (void)Dev;
    assert(cpu_locked);
    cpu_locked = false;
    pthread_mutex_unlock(&cpu_guard);
}
static void WdfWaitLockAcquire(WDFWAITLOCK l, void *t)
{
    (void)t;
    if (l == (WDFWAITLOCK)1) {
        assert(!pmic_locked);
        pmic_locked = true;
        ++pmic_lock_acquires;
    } else if (l == (WDFWAITLOCK)2) {
        assert(!bl_locked);
        bl_locked = true;
    }
}
static void WdfWaitLockRelease(WDFWAITLOCK l)
{
    if (l == (WDFWAITLOCK)1) {
        assert(pmic_locked);
        pmic_locked = false;
    } else if (l == (WDFWAITLOCK)2) {
        assert(bl_locked);
        bl_locked = false;
    }
}
static VOID SwFanPmicLock(PSWFAN_DEVICE Dev)
{ if (Dev->PmicLock != NULL) WdfWaitLockAcquire(Dev->PmicLock, NULL); }
static VOID SwFanPmicUnlock(PSWFAN_DEVICE Dev)
{ if (Dev->PmicLock != NULL) WdfWaitLockRelease(Dev->PmicLock); }

static PSWFAN_DEVICE SwFanGetDevice(void *d) { return d; }
static void SwFanTrace(const void *n, ULONG v) { (void)n; (void)v; }
static VOID SwFanSetDuty(PSWFAN_DEVICE Dev, ULONG Duty) { Dev->Duty = Duty; }
static NTSTATUS SwFanOpenTarget(void *d, LARGE_INTEGER i, void **t)
{ (void)d; (void)i; *t = (void *)1; return STATUS_SUCCESS; }
static void WdfObjectDelete(void *p) { (void)p; }
static void WdfTimerStart(void *t, unsigned ms)
{ assert(t != NULL && !pmic_locked); ++timer_starts; last_timer_ms = ms; }
static int WdfRequestGetRequestorMode(WDFREQUEST r) { (void)r; return requestor_mode; }
static NTSTATUS WdfRequestRetrieveInputBuffer(WDFREQUEST r, size_t min_len, PVOID *buf, size_t *len)
{
    struct mock_request *mr = r;
    if (mr->in_len < min_len) return STATUS_INVALID_PARAMETER;
    *buf = mr->in;
    if (len) *len = mr->in_len;
    return STATUS_SUCCESS;
}
static NTSTATUS WdfRequestRetrieveOutputBuffer(WDFREQUEST r, size_t min_len, PVOID *buf, size_t *len)
{
    struct mock_request *mr = r;
    if (mr->out_len < min_len) return STATUS_INVALID_PARAMETER;
    *buf = mr->out;
    if (len) *len = mr->out_len;
    return STATUS_SUCCESS;
}
static NTSTATUS SwFanReadReg(void *t, UCHAR reg, UCHAR *v)
{
    assert(t != NULL);
    if (t == (void *)2) {
        assert(!pmic_locked);
        ++sensor_read_calls;
        if (fail_sensor_read) return STATUS_IO_TIMEOUT;
        *v = (reg == 1) ? sensor_remote_temp : sensor_local_temp;
        return STATUS_SUCCESS;
    }
    assert(pmic_locked);
    ++pmic_read_calls;
    if (fail_pmic_read_at && pmic_read_calls == fail_pmic_read_at) {
        fail_pmic_read_at = 0;
        return STATUS_IO_TIMEOUT;
    }
    if (corrupt_pmic_read_at && pmic_read_calls == corrupt_pmic_read_at) {
        corrupt_pmic_read_at = 0;
        *v = corrupt_pmic_value;
        return STATUS_SUCCESS;
    }
    *v = pmic_regs[reg];
    return STATUS_SUCCESS;
}
static NTSTATUS WdfIoTargetSendWriteSynchronously(void *t, void *r,
    WDF_MEMORY_DESCRIPTOR *m, void *o, WDF_REQUEST_SEND_OPTIONS *s, ULONG_PTR *n)
{
    UCHAR *b = m->Buffer;
    (void)r; (void)o; (void)s;
    assert(t != NULL && pmic_locked && m->Size == 2);
    if (fail_pmic_write) return STATUS_IO_TIMEOUT;
    if (b[0] == 0x26 && b[1] < 174u) {
        pthread_mutex_lock(&step_guard);
        if (pause_cpu_down) {
            assert(cpu_locked);
            down_waiting = true;
            pthread_cond_broadcast(&step_changed);
            while (!allow_down) pthread_cond_wait(&step_changed, &step_guard);
        }
        pthread_mutex_unlock(&step_guard);
    }
    pmic_regs[b[0]] = b[1];
    if (order_len < sizeof(order_log)) order_log[order_len++] = 'V';
    *n = 2;
    return STATUS_SUCCESS;
}
static void KeDelayExecutionThread(int m, bool a, LARGE_INTEGER *d) { (void)m; (void)a; (void)d; }
static void KeRaiseIrql(unsigned i, KIRQL *o) { (void)i; *o = 0; }
static void KeLowerIrql(KIRQL i) { (void)i; }
static void KeStallExecutionProcessor(unsigned n) { (void)n; }
static ULONGLONG KeQueryInterruptTime(void) { return mock_time; }
#define KeMemoryBarrier() ((void)0)
static LONG InterlockedExchange(volatile LONG *dst, LONG val)
{ LONG old = *dst; *dst = val; return old; }
static LONG InterlockedCompareExchange(volatile LONG *dst, LONG exch, LONG comp)
{ LONG cur = *dst; if (cur == comp) *dst = exch; return cur; }

static ULONG dfll_regs[0x300 / 4], sources[2], reset_dvco, bank[3], resets[2], gates[2], i2c_source;
static bool dfll_bad_monitor;
static void dfll_settle(void)
{
    ULONG cfg = dfll_regs[SW_DFLL_OUTPUT_CFG / 4];
    ULONG force = dfll_regs[SW_DFLL_FORCE / 4];
    int idx;
    if (!(cfg & SW_DFLL_I2C_ENABLE)) return;
    if (force & 0x40u) idx = force & 63u;
    else if (dfll_regs[0] == 2) {
        int f = (dfll_regs[SW_DFLL_FREQ_REQ / 4] >> 16) & 4095;
        if (f & 2048) f -= 4096;
        idx = ((cfg >> 24) & 63) + f / 64;
        if (idx < (int)((cfg >> 8) & 63)) idx = (cfg >> 8) & 63;
        if (idx > (int)((cfg >> 16) & 63)) idx = (cfg >> 16) & 63;
    } else return;
    assert(idx >= 0 && idx < 33);
    pmic_regs[0x26] = (UCHAR)dfll_regs[SW_DFLL_LUT / 4 + idx];
    dfll_regs[SW_DFLL_I2C_STS / 4] = (ULONG)idx << 1;
}
static ULONG regread(ULONG *p)
{
    if (p == &dfll_regs[SW_DFLL_MON_DATA / 4])
        return (1u << 16) | (dfll_bad_monitor ? 1u : (dfll_regs[SW_DFLL_FREQ_REQ / 4] & 127u) * 2u);
    return *p;
}
#define READ_REGISTER_ULONG(p) regread(p)
static void regwrite(ULONG *p, ULONG v)
{
    assert(cpu_locked);
    if (order_len < sizeof(order_log)) order_log[order_len++] = 'C';
    *p = v;
    if (p == car_regs + 0xe0 / 4 && (v & (1u << 30))) *p |= 1u << 27;
    if (p == resets) { bank[0] |= v; memset(dfll_regs,0,sizeof(dfll_regs)); }
    if (p == resets + 1) bank[0] &= ~v;
    if (p == gates) bank[2] |= v;
    if (p == gates + 1) bank[2] &= ~v;
    dfll_settle();
}
#define WRITE_REGISTER_ULONG(p, v) regwrite((p), (v))
#define SWFAN_HAVE_DFLL 1
static ULONG SwFanDfllReadKhz(PSWFAN_DEVICE);
static NTSTATUS SwFanDfllStopLocked(PSWFAN_DEVICE);
static NTSTATUS SwFanDvfsUpdateLocked(PSWFAN_DEVICE, ULONG, BOOLEAN);
#define TMP451_LOCAL  0x00
#define TMP451_REMOTE 0x01
static NTSTATUS SwFanConfigureSensor(void *t)
{
    assert(t == (void *)2 && !pmic_locked);
    ++sensor_config_calls;
    return fail_sensor_config ? STATUS_IO_TIMEOUT : STATUS_SUCCESS;
}
#define SWFAN_HAVE_TEMP_REFRESH 1
static NTSTATUS SwFanRefreshTemp(PSWFAN_DEVICE Dev, PULONG TempOut)
{
    UCHAR Local = 0, Remote = 0;
    BOOLEAN NeedReinit = FALSE;
    NTSTATUS Status = STATUS_SUCCESS;

    if (Dev->Sensor == NULL) return STATUS_DEVICE_NOT_READY;
    if (Dev->BacklightLock != NULL) WdfWaitLockAcquire(Dev->BacklightLock, NULL);
    if (Dev->Stopping) {
        if (Dev->BacklightLock != NULL) WdfWaitLockRelease(Dev->BacklightLock);
        return STATUS_DEVICE_NOT_READY;
    }
    NeedReinit = Dev->IsLite && (Dev->SensorNeedsReinit || Dev->ScreenOffIdle);
    if (NeedReinit) Dev->SensorNeedsReinit = FALSE;
    if (Dev->BacklightLock != NULL) WdfWaitLockRelease(Dev->BacklightLock);

    if (NeedReinit) {
        Status = SwFanConfigureSensor(Dev->Sensor);
        if (!NT_SUCCESS(Status)) {
            if (Dev->BacklightLock != NULL) WdfWaitLockAcquire(Dev->BacklightLock, NULL);
            Dev->SensorNeedsReinit = TRUE;
            if (Dev->BacklightLock != NULL) WdfWaitLockRelease(Dev->BacklightLock);
        }
    }
    if (NT_SUCCESS(Status)) Status = SwFanReadReg(Dev->Sensor, TMP451_REMOTE, &Remote);
    if (NT_SUCCESS(Status)) Status = SwFanReadReg(Dev->Sensor, TMP451_LOCAL, &Local);
    if (NT_SUCCESS(Status)) {
        ULONG Temp = (Remote > Local) ? Remote : Local;
        SwFanCpuLock(Dev);
        Dev->LastTemp = Temp;
        Dev->TempValid = TRUE;
        Dev->SocTempC = Remote;
        Dev->BoardTempC = Local;
        Dev->TempSampleTime = KeQueryInterruptTime();
        SwFanCpuUnlock(Dev);
        *TempOut = Temp;
        return STATUS_SUCCESS;
    }
    SwFanCpuLock(Dev);
    Dev->TempValid = FALSE;
    Dev->TempSampleTime = 0;
    SwFanCpuUnlock(Dev);
    return Status;
}
#include "../src/t214/clock.h"
#include "../src/t214/dfll_windows.h"
#include "../src/t214/gpu_pmic.h"

static void wait_step(bool *flag)
{
    struct timespec until;
    assert(clock_gettime(CLOCK_REALTIME, &until) == 0);
    until.tv_sec += 5;
    while (!*flag) assert(pthread_cond_timedwait(&step_changed, &step_guard, &until) == 0);
}

static void *eco_thread(void *p)
{
    assert(SwCpuApply(p, FALSE) == STATUS_SUCCESS);
    return NULL;
}

static void *thermal_thread(void *p)
{
    temperature_thread = true;
    SwCpuTemperature(p, 40u, TRUE);
    return NULL;
}

static void test_concurrent_cpu_policy(PSWFAN_DEVICE d)
{
    pthread_t eco, thermal;
    assert(SwFanSetPowerMode(d, SwFanPowerModeDefaultFixed) == STATUS_SUCCESS);
    assert(d->CpuApplied && d->CpuOverclockEnabled);
    pause_cpu_down = true;
    down_waiting = temperature_waiting = allow_down = false;
    assert(pthread_create(&eco, NULL, eco_thread, d) == 0);
    pthread_mutex_lock(&step_guard);
    wait_step(&down_waiting);
    assert(pthread_create(&thermal, NULL, thermal_thread, d) == 0);
    wait_step(&temperature_waiting);
    /* Suspend is paused between F-down and V-down. The temperature callback must
     * not enter the CPU transaction until the complete voltage-down finishes. */
    assert(pthread_mutex_trylock(&cpu_guard) != 0);
    assert(SwCpuKhz(d) == d->BootCpuKhz);
    allow_down = true;
    pthread_cond_broadcast(&step_changed);
    pthread_mutex_unlock(&step_guard);
    assert(pthread_join(eco, NULL) == 0);
    assert(pthread_join(thermal, NULL) == 0);
    pause_cpu_down = false;
    assert(d->CpuApplied && d->CpuOverclockEnabled);
    assert(SwCpuKhz(d) == CPU_TARGET_KHZ && pmic_regs[CPU_M4_VOUT] == CPU_OC_VOUT);
}

static NTSTATUS send_gpu_pmic(PSWFAN_DEVICE d, struct tg_gpu_pmic_request *op)
{
    struct mock_request req = { op, sizeof(*op), op, sizeof(*op) };
    ULONG_PTR info = 0;
    NTSTATUS st = SwFanGpuPmic(d, &req, &info);
    assert(!pmic_locked && !bl_locked && !cpu_locked);
    if (NT_SUCCESS(st)) assert(info == sizeof(*op));
    return st;
}

int main(void)
{
    SWFAN_DEVICE d = {0};
    SWFAN_POWER_STATUS st = {0};
    struct tg_gpu_pmic_request op;
    unsigned locks_before;

    memset(pmic_regs, 0, sizeof(pmic_regs));
    memset(car_regs, 0, sizeof(car_regs));
    pmic_regs[0x06] = 0x54; /* M3 DDR + M4 CPU enabled, M1 GPU initially off */
    pmic_regs[0x23] = 20;   /* pre-acquisition cold M1 code (<68 while off) */
    pmic_regs[0x26] = 110;  /* 800 mV boot CPU M4 */
    car_regs[0x20 / 4] = 0x20008888u;
    car_regs[0xe0 / 4] = (1u << 30) | (1u << 27) | (2u << 20) | (156u << 8) | 2u; /* 998.4 MHz */
    car_regs[0xa0 / 4] = (1u<<30) | (85u<<8) | 8u; /* 408 MHz PLLP */

    d.Dfll.Map[SwDfllMmio] = (UCHAR *)dfll_regs;
    d.Dfll.Map[SwDfllSources] = (UCHAR *)sources;
    d.Dfll.Map[SwDfllReset] = (UCHAR *)&reset_dvco;
    d.Dfll.Map[SwDfllBankStatus] = (UCHAR *)bank;
    d.Dfll.Map[SwDfllBankReset] = (UCHAR *)resets;
    d.Dfll.Map[SwDfllBankEnable] = (UCHAR *)gates;
    d.Dfll.Map[SwDfllI2cSource] = (UCHAR *)&i2c_source;
    i2c_source = 4; /* integer divide by 5 */
    d.Dfll.Handoff = (LITE_DFLL_HANDOFF){LITE_DFLL_HANDOFF_MAGIC, LITE_DFLL_HANDOFF_VERSION,
        sizeof(LITE_DFLL_HANDOFF), 0x2120, 0x83, 1630, 0, 0x33};
    d.PowerMode = SwFanPowerModeDefaultFixed;
    d.IsLite = TRUE;
    d.BacklightReady = TRUE;
    d.DisplayState = 1;
    d.Car = (UCHAR *)car_regs;
    d.Pwm = (UCHAR *)&car_regs[60];
    d.PmicLock = (WDFWAITLOCK)1;
    d.BacklightLock = (WDFWAITLOCK)2;
    d.Config.CpuKhz = LITE_CPU_KHZ;
    d.Config.RamActualKhz = 1600000u;
    d.Config.Status = LitePerfReady;
    d.Config.PmicAddress = 0x33;
    d.ConnectionCount = 3;
    d.Sensor = (void *)2;
    d.TempValid = TRUE;
    d.TempSampleTime = mock_time;
    d.LastTemp = 41;
    d.Duty = 51;
    d.CpuOverclockEnabled = TRUE;
    d.BaseCpuOverclockEnabled = TRUE;
    d.GpuDvfsPolicy = (LONG)TG_GPU_DVFS_POLICY_PACK(0, 460800u, 460800u, 0);

    SwCpuPrepare(&d);
    assert(d.CpuConfigured && d.BootCpuKhz == 998400u && !pmic_locked);
    SwCpuTemperature(&d, 41, TRUE);
    assert(d.CpuApplied && SwCpuKhz(&d) == 1497600u && pmic_regs[0x26] == 174);

    /* 1. UserMode callers are blocked from TG_GPU_IOCTL_PMIC. */
    requestor_mode = UserMode;
    op = (struct tg_gpu_pmic_request){ 0, TG_GPU_PMIC_QUERY_ADDRESS, 0, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_ACCESS_DENIED);
    requestor_mode = KernelMode;

    /* 2. M1 enable requires valid M1_VOUT >= 590 mV (code >= 68); RMW preserves 0x54.
     * Fresh cached temperature does not incur extra sensor I/O; off-rail VOUT write does not publish uV. */
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_INVALID_DEVICE_STATE);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 110 }; /* 800 mV while off */
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && pmic_regs[0x23] == 110 && d.GpuReportedUv == 0);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && pmic_regs[0x06] == 0x55 && d.GpuRailOn);
    assert(d.GpuReportedUv == 800000u && sensor_read_calls == 0);

    /* 3. Live M1 bounds: 68..160 (590..1050 mV) allowed; <68 rejected while M1 on! */
    locks_before = pmic_lock_acquires;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 68 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && pmic_regs[0x23] == 68 && d.GpuReportedUv == 590000u);
    assert(pmic_lock_acquires == locks_before + 1);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 67 };
    assert(send_gpu_pmic(&d, &op) == STATUS_INVALID_PARAMETER && pmic_regs[0x23] == 68);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 161 };
    assert(send_gpu_pmic(&d, &op) == STATUS_INVALID_PARAMETER && pmic_regs[0x23] == 68);
    /* CPU M4 (0x26) is read-only on GPU bridge; DDR M3 is completely rejected. */
    op = (struct tg_gpu_pmic_request){ 0x33, 0x26, 1, 110 };
    assert(send_gpu_pmic(&d, &op) == STATUS_INVALID_PARAMETER);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x25, 0, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_INVALID_PARAMETER);

    /* Disabling M1 clears rail telemetry and allows restoring cold pre-acquisition voltage (<68). */
    d.Timer = (void *)1;
    d.GpuReportedKhz = 844800u; d.GpuSampleTime = mock_time;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && pmic_regs[0x06] == 0x54 && !d.GpuRailOn);
    assert(d.GpuReportedUv == 0 && d.GpuReportedKhz == 0 && d.GpuSampleTime == 0);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 20 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && pmic_regs[0x23] == 20 && d.GpuReportedUv == 0);

    /* 4. Fresh temperature enforcement: stale cache triggers sensor refresh and blocks overtemp >= 80 C. */
    op = (struct tg_gpu_pmic_request){ 0x33, 0x23, 1, 110 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && d.GpuReportedUv == 0);
    mock_time += 70000000ULL; /* > 6 s stale */
    sensor_remote_temp = 82; sensor_local_temp = 40;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_NOT_READY && !d.GpuRailOn && pmic_regs[0x06] == 0x54 && d.LastTemp == 82);
    /* Post-D0Entry invalid temp refreshes from sensor on query/enable and fails safe on I2C error. */
    d.TempValid = FALSE; d.TempSampleTime = 0; fail_sensor_read = true;
    op = (struct tg_gpu_pmic_request){ 0, TG_GPU_PMIC_QUERY_TEMP, 0, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_NOT_READY);
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_NOT_READY && !d.GpuRailOn);
    fail_sensor_read = false; sensor_remote_temp = 44; sensor_local_temp = 39;
    op = (struct tg_gpu_pmic_request){ 0, TG_GPU_PMIC_QUERY_TEMP, 0, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && op.value == 44 && d.TempValid && d.TempSampleTime == mock_time);

    /* Re-enable at 800 mV while parked in ScreenOffIdle reinitializes TMP451 before enabling rail,
     * refuses enable on sensor config failure, and wakes cooling + 50 ms polling on success. */
    d.ScreenOffIdle = TRUE;
    d.SensorNeedsReinit = FALSE;
    d.Duty = 0;
    timer_starts = 0;
    fail_sensor_config = true;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_NOT_READY && !d.GpuRailOn && d.ScreenOffIdle && d.SensorNeedsReinit && !d.TempValid && timer_starts == 0);
    fail_sensor_config = false; sensor_remote_temp = 41;
    unsigned cfg_before = sensor_config_calls;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && d.GpuRailOn && d.GpuReportedUv == 800000u);
    assert(!d.ScreenOffIdle && !d.SensorNeedsReinit && d.TempValid && d.LastTemp == 41 && d.Duty == 76);
    assert(sensor_config_calls == cfg_before + 1 && timer_starts == 1 && last_timer_ms == 50);

    /* Disabling M1 remains allowed even if the sensor is failing; failed write does not publish rail-on. */
    d.TempValid = FALSE; d.TempSampleTime = 0; fail_sensor_read = true;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && !d.GpuRailOn && d.GpuReportedUv == 0);
    fail_sensor_read = false; fail_pmic_write = true;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_IO_TIMEOUT && !d.GpuRailOn);
    fail_pmic_write = false;

    /* Post-write readback failure or bit-mismatch on enable still publishes GpuRailOn and wakes polling;
     * readback failure on disable keeps GpuRailOn fail-safe TRUE until confirmed off. */
    d.ScreenOffIdle = TRUE; d.Duty = 0; timer_starts = 0;
    fail_pmic_read_at = pmic_read_calls + 3;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_IO_TIMEOUT && d.GpuRailOn && d.GpuReportedUv == 800000u);
    assert(!d.ScreenOffIdle && d.Duty == 76 && timer_starts == 1 && last_timer_ms == 50);
    fail_pmic_read_at = pmic_read_calls + 2;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_IO_TIMEOUT && d.GpuRailOn);
    pmic_regs[0x06] = 0x55;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 0 };
    assert(send_gpu_pmic(&d, &op) == STATUS_SUCCESS && !d.GpuRailOn && d.GpuReportedUv == 0);
    d.ScreenOffIdle = TRUE; timer_starts = 0;
    corrupt_pmic_read_at = pmic_read_calls + 3; corrupt_pmic_value = 0x57;
    op = (struct tg_gpu_pmic_request){ 0x33, 0x06, 1, 1 };
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_DATA_ERROR && d.GpuRailOn && !d.ScreenOffIdle && timer_starts == 1);
    d.Stopping = TRUE;
    assert(send_gpu_pmic(&d, &op) == STATUS_DEVICE_NOT_READY);
    d.Stopping = FALSE;
    d.Duty = 51;

    /* Profiles follow the captured nvpmodel requests, and use the DFLL
     * adapter/core, not the legacy two PLLX steps. */
    SwFanGetPowerStatus(&d, &st);
    assert(st.PowerMode == 7 && st.CpuActualKhz == 1497600u);
    assert(SwFanSetPowerMode(&d, SwFanPowerModeHandheld) == STATUS_NOT_SUPPORTED);
    op = (struct tg_gpu_pmic_request){0,TG_GPU_PMIC_QUERY_DVFS_POLICY,0,0};
    assert(send_gpu_pmic(&d,&op) == STATUS_SUCCESS);
    assert(SwFanSetPowerMode(&d, SwFanPowerModeHandheld) == STATUS_NOT_SUPPORTED);
    op=(struct tg_gpu_pmic_request){0,TG_GPU_PMIC_REPORT_GPU_KHZ,1,460800};
    assert(send_gpu_pmic(&d,&op)==STATUS_SUCCESS && !d.GpuSampleTime);
    op=(struct tg_gpu_pmic_request){0,TG_GPU_PMIC_REPORT_ABI,1,TG_GPU_LIVE_COUNTER_ABI};
    assert(send_gpu_pmic(&d,&op)==STATUS_SUCCESS);
    assert(SwFanDfllAvailable(&d));
    for (ULONG mode=0; mode < SwNvpCount; ++mode) {
        assert(SwFanSetPowerMode(&d,mode) == STATUS_SUCCESS);
        SwFanGetPowerStatus(&d,&st);
        assert(st.PowerMode == mode && st.CpuDfllActive && !d.CpuApplied);
        assert(st.CpuRequestedMaxKhz == sw_nvpmodel_profiles[mode].cpu_max_khz);
        assert(st.CpuActualKhz == (sw_nvpmodel_profiles[mode].cpu_overclock ? 2397000u : mode==1 ? 1581000u : 1963500u));
        assert(st.GpuPolicyMaxKhz == (mode==1 ? 537600u : mode==3 || mode==4 || mode==6 ? 1228800u : 844800u));
        assert(st.GpuPolicyMinKhz == (mode>=5 ? 460800u : 76800u));
    }
    assert(SwFanSetPowerMode(&d,7) == STATUS_SUCCESS);
    assert(!d.Dfll.Control.active && SwCpuKhz(&d)==1497600u && pmic_regs[0x26]==174);
    assert(!d.Dfll.ClocksOwned && !bank[2]);

    /* A missing/unknown handoff cannot silently turn on the new clock path. */
    d.Dfll.Handoff.CpuSpeedo=0;
    assert(SwFanSetPowerMode(&d,0)==STATUS_NOT_SUPPORTED && d.PowerMode==7);
    d.Dfll.Handoff.CpuSpeedo=1630;
    /* A monitor failure recovers to boot PLLX, latches the policy failure,
     * and a later temperature poll cannot automatically re-enter DFLL. */
    dfll_bad_monitor=true;
    assert(SwFanSetPowerMode(&d,0)==STATUS_DEVICE_HARDWARE_ERROR);
    assert(d.Dfll.FaultLatched && SwCpuKhz(&d)==d.BootCpuKhz && d.PowerMode==7);
    dfll_bad_monitor=false;
    SwCpuTemperature(&d,40,TRUE);
    assert(!d.Dfll.Control.active && SwCpuKhz(&d)==d.BootCpuKhz);
    assert(SwFanSetPowerMode(&d,7)==STATUS_SUCCESS && !d.Dfll.FaultLatched);
    test_concurrent_cpu_policy(&d);

    op=(struct tg_gpu_pmic_request){0,TG_GPU_PMIC_REPORT_GPU_KHZ,1,844800};
    assert(send_gpu_pmic(&d,&op)==STATUS_SUCCESS);
    SwFanGetPowerStatus(&d,&st);
    assert(st.GpuReportedKhz==844800 && st.GpuSampleAgeMs==0);
    /* Unsupported / invalid inputs rejected cleanly. */
    assert(SwFanSetPowerMode(&d, 8) == STATUS_INVALID_PARAMETER);
    d.IsLite = FALSE;
    assert(SwFanSetPowerMode(&d, 0) == STATUS_NOT_SUPPORTED);

    puts("PASS: SwitchFan PmicLock serialization, M1 Vmin/Vmax bounds and Switchroot 0..6 modes, DFLL ownership and recovery (MMIO model)");
    return 0;
}
