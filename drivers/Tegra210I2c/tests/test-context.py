#!/usr/bin/env python3
"""Exercise Tegra210I2c 6-bus SC7 CAR/packet-mode context save/restore and faults."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra210i2c.c").read_text()

def function(name):
    m = re.search(r"^(?:static )?(?:VOID|ULONG|BOOLEAN|NTSTATUS)\s+" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
    assert m, f"missing {name}"
    end = source.index("{", m.start()) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[m.start():end] + "\n"

defines = "\n".join(re.findall(r"^#define (?:I2C_|CNFG_|FIFO_|INT_|HDR0_|HDR_|CONFIG_)[^\n]+", source, re.M))
clock_table = source[source.index("typedef struct _TEGRA_I2C_CLOCK"):source.index("typedef struct _TEGRA_I2C_DEVICE")]

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint32_t ULONG, *PULONG;
typedef uint16_t USHORT;
typedef uint8_t UCHAR, *PUCHAR, BOOLEAN;
typedef int32_t LONG, NTSTATUS;
typedef void VOID, *PVOID, *PKEVENT, *HANDLE, *WDFDEVICE;
typedef unsigned WDF_POWER_DEVICE_STATE;

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_DEVICE_NOT_READY -1
#define STATUS_DEVICE_BUSY -2
#define STATUS_DEVICE_POWER_FAILURE -3
#define STATUS_IO_TIMEOUT -4
#define STATUS_INVALID_PARAMETER -5
#define STATUS_NO_SUCH_DEVICE -6
#define STATUS_IO_DEVICE_ERROR -7
#define WdfPowerDeviceD3Final 5
#define RTL_NUMBER_OF(a) (sizeof(a) / sizeof((a)[0]))
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define KeStallExecutionProcessor(x) ((void)(x))
''' + defines + "\n" + clock_table + r'''

typedef struct _TEGRA_I2C_DEVICE {
    volatile UCHAR *Base;
    ULONG Length;
    volatile UCHAR *Car;
    const TEGRA_I2C_CLOCK *Clock;
    ULONG SavedSource, Saved[RTL_NUMBER_OF(TegraI2cContextRegisters)];
    BOOLEAN ContextValid, ResumePending;
    PKEVENT SdGate;
    HANDLE SdGateHandle;
    ULONG PhysLow;
    volatile LONG Errors, Nacks, Timeouts, LastError, LastAddress;
} TEGRA_I2C_DEVICE, *PTEGRA_I2C_DEVICE;

static PTEGRA_I2C_DEVICE TegraI2cGetDevice(WDFDEVICE d) { return (PTEGRA_I2C_DEVICE)d; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++(*p); }
static LONG InterlockedExchange(volatile LONG *p, LONG v) { LONG o = *p; *p = v; return o; }

static bool gate_held = false;
static unsigned gate_acquires = 0;
static NTSTATUS gate_status = STATUS_SUCCESS;
static NTSTATUS TegraI2c5Acquire(PKEVENT g) {
    if (!g) return STATUS_SUCCESS;
    if (!NT_SUCCESS(gate_status)) return gate_status;
    assert(!gate_held);
    gate_held = true;
    gate_acquires++;
    return STATUS_SUCCESS;
}
static void TegraI2c5Release(PKEVENT g) {
    if (!g) return;
    assert(gate_held);
    gate_held = false;
}

static UCHAR car_mmio[0x1000];
static UCHAR i2c_mmio[I2C_MMIO_SIZE];
static PTEGRA_I2C_DEVICE active_dev;
static bool fail_enable, fail_reset_clear, fail_source, fail_cfg_load, fail_fifo_flush;
static unsigned host_reads, host_writes;

static ULONG raw_get(const UCHAR *b, ULONG o) {
    ULONG v;
    memcpy(&v, b + o, 4);
    return v;
}
static void raw_put(UCHAR *b, ULONG o, ULONG v) {
    memcpy(b + o, &v, 4);
}

static void assert_i2c_clocked(void) {
    const TEGRA_I2C_CLOCK *c = active_dev->Clock;
    assert(c != NULL);
    /* Touching I2C MMIO while gated or held in reset hangs Tegra APB! */
    assert((raw_get(car_mmio, c->Enable) & c->Mask) != 0);
    assert((raw_get(car_mmio, c->Reset) & c->Mask) == 0);
}

static ULONG mock_read(PULONG p) {
    UCHAR *b = (UCHAR *)p;
    if (b >= car_mmio && b < car_mmio + sizeof(car_mmio)) {
        return raw_get(car_mmio, (ULONG)(b - car_mmio));
    }
    assert(b >= i2c_mmio && b < i2c_mmio + sizeof(i2c_mmio));
    assert_i2c_clocked();
    host_reads++;
    return raw_get(i2c_mmio, (ULONG)(b - i2c_mmio));
}

static void mock_write(PULONG p, ULONG v) {
    UCHAR *b = (UCHAR *)p;
    if (b >= car_mmio && b < car_mmio + sizeof(car_mmio)) {
        ULONG off = (ULONG)(b - car_mmio);
        const TEGRA_I2C_CLOCK *c = active_dev->Clock;
        if (off == c->ResetSet) {
            assert(v == c->Mask);
            raw_put(car_mmio, c->Reset, raw_get(car_mmio, c->Reset) | v);
        } else if (off == c->ResetClear) {
            assert(v == c->Mask);
            if (!fail_reset_clear) raw_put(car_mmio, c->Reset, raw_get(car_mmio, c->Reset) & ~v);
        } else if (off == c->EnableSet) {
            assert(v == c->Mask);
            if (!fail_enable) raw_put(car_mmio, c->Enable, raw_get(car_mmio, c->Enable) | v);
        } else if (off == c->EnableClear) {
            assert(v == c->Mask);
            raw_put(car_mmio, c->Enable, raw_get(car_mmio, c->Enable) & ~v);
        } else if (off == c->Source) {
            if (!fail_source) raw_put(car_mmio, c->Source, v);
        } else {
            assert(!"unexpected CAR write");
        }
        return;
    }
    assert(b >= i2c_mmio && b < i2c_mmio + sizeof(i2c_mmio));
    assert_i2c_clocked();
    host_writes++;
    ULONG off = (ULONG)(b - i2c_mmio);
    if (off == I2C_CONFIG_LOAD) {
        if (!fail_cfg_load) v &= ~1u;
        raw_put(i2c_mmio, off, v);
    } else if (off == I2C_FIFO_CONTROL) {
        if (!fail_fifo_flush) v &= ~FIFO_FLUSH_TX_RX;
        raw_put(i2c_mmio, off, v);
    } else if (off == I2C_INT_STATUS) {
        raw_put(i2c_mmio, off, raw_get(i2c_mmio, off) & ~v);
    } else if (off == I2C_TX_FIFO) {
        raw_put(i2c_mmio, off, v);
        /* Simulate immediate packet completion after payload write */
        raw_put(i2c_mmio, I2C_INT_STATUS, INT_PACKET_DONE | INT_ALL_PACKETS);
    } else {
        raw_put(i2c_mmio, off, v);
    }
}

#define READ_REGISTER_ULONG(p) mock_read((PULONG)(p))
#define WRITE_REGISTER_ULONG(p, v) mock_write((PULONG)(p), (v))
'''

funcs = "".join(function(n) for n in [
    "RdReg",
    "WrReg",
    "TegraI2cWaitClear",
    "TegraI2cReadClock",
    "TegraI2cWriteClock",
    "TegraI2cSaveContext",
    "TegraI2cRestoreContext",
    "TegraI2cLoadConfig",
    "TegraI2cResetFifos",
    "TegraI2cBusClear",
    "TegraI2cTransfer",
    "TegraI2cEvtD0Entry",
    "TegraI2cEvtD0Exit",
])

tests = r'''
static void setup_bus(PTEGRA_I2C_DEVICE d, unsigned idx) {
    const TEGRA_I2C_CLOCK *c = &TegraI2cClocks[idx];
    memset(d, 0, sizeof(*d));
    memset(car_mmio, 0, sizeof(car_mmio));
    memset(i2c_mmio, 0, sizeof(i2c_mmio));
    active_dev = d;
    fail_enable = fail_reset_clear = fail_source = fail_cfg_load = fail_fifo_flush = false;
    gate_held = false; gate_acquires = 0; gate_status = STATUS_SUCCESS;
    host_reads = host_writes = 0;

    d->Base = i2c_mmio;
    d->Length = sizeof(i2c_mmio);
    d->Car = car_mmio;
    d->Clock = c;
    d->PhysLow = c->Base;
    d->SdGate = (c->Base == 0x7000d000) ? (PKEVENT)1 : NULL;

    /* Set neighbor bits in Enable and Reset to prove set/clear isolation! */
    raw_put(car_mmio, c->Enable, 0xA5A5A5A5u | c->Mask);
    raw_put(car_mmio, c->Reset,  0x5A5A5A5Au & ~c->Mask);
    raw_put(car_mmio, c->Source, 0x00000004u | (idx << 8));

    raw_put(i2c_mmio, I2C_CNFG, CNFG_PACKET_MODE | CNFG_SEND | 0x2u);
    raw_put(i2c_mmio, I2C_CLK_DIVISOR, 0x00190001u + idx);
    raw_put(i2c_mmio, I2C_SL_CNFG, 0x00000004u);
    raw_put(i2c_mmio, I2C_TLOW_SEXT, 0x00000010u + idx);
    raw_put(i2c_mmio, I2C_CLKEN_OVERRIDE, 0x00000001u);
    raw_put(i2c_mmio, I2C_INTERFACE_TIMING, 0x00020405u + idx);
    raw_put(i2c_mmio, I2C_HS_TIMING, 0x00000203u);
    raw_put(i2c_mmio, I2C_FIFO_CONTROL, 0x000000E0u | FIFO_FLUSH_TX_RX);
    raw_put(i2c_mmio, I2C_FIFO_STATUS, 0x00000080u); /* 8 TX words free */
}

int main(void) {
    TEGRA_I2C_DEVICE d;

    /* 1. Exercise all 6 Tegra210 I2C buses across repeated D0Exit -> SC7 wipe -> D0Entry cycles! */
    for (unsigned idx = 0; idx < RTL_NUMBER_OF(TegraI2cClocks); ++idx) {
        const TEGRA_I2C_CLOCK *c = &TegraI2cClocks[idx];
        setup_bus(&d, idx);
        ULONG neighbor_en = raw_get(car_mmio, c->Enable) & ~c->Mask;
        ULONG neighbor_rst = raw_get(car_mmio, c->Reset) & ~c->Mask;

        for (unsigned cycle = 0; cycle < 3; ++cycle) {
            assert(TegraI2cEvtD0Exit(&d, 3) == STATUS_SUCCESS);
            assert(d.ContextValid && d.ResumePending && !gate_held);
            if (c->Base == 0x7000d000) assert(gate_acquires == cycle * 2 + 1);

            /* Simulate SC7 power loss: controller wiped, clock gated, reset asserted! */
            memset(i2c_mmio, 0, sizeof(i2c_mmio));
            raw_put(i2c_mmio, I2C_FIFO_STATUS, 0x80u);
            raw_put(car_mmio, c->Enable, neighbor_en);
            raw_put(car_mmio, c->Reset, neighbor_rst | c->Mask);
            raw_put(car_mmio, c->Source, 0);

            assert(TegraI2cEvtD0Entry(&d, 3) == STATUS_SUCCESS);
            assert(!d.ResumePending && !gate_held);

            /* Neighbor CAR bits untouched; bus clock enabled, out of reset, source restored! */
            assert(raw_get(car_mmio, c->Enable) == (neighbor_en | c->Mask));
            assert(raw_get(car_mmio, c->Reset)  == neighbor_rst);
            assert(raw_get(car_mmio, c->Source) == (0x00000004u | (idx << 8)));

            /* CNFG_SEND cleared, CNFG_PACKET_MODE set, timing restored, INT_MASK == 0! */
            assert(raw_get(i2c_mmio, I2C_CNFG) == (CNFG_PACKET_MODE | 0x2u));
            assert(raw_get(i2c_mmio, I2C_CLK_DIVISOR) == 0x00190001u + idx);
            assert(raw_get(i2c_mmio, I2C_INTERFACE_TIMING) == 0x00020405u + idx);
            assert(raw_get(i2c_mmio, I2C_INT_MASK) == 0);
        }
    }

    /* 2. Defensive auto-restore in TegraI2cTransfer when CAR is gated/in-reset after SC7!
     *    Must enable CAR clock BEFORE reading RdReg(I2C_CNFG)! */
    setup_bus(&d, 4); /* I2C5 PMIC bus */
    assert(TegraI2cEvtD0Exit(&d, 3) == STATUS_SUCCESS);
    memset(i2c_mmio, 0, sizeof(i2c_mmio));
    raw_put(i2c_mmio, I2C_FIFO_STATUS, 0x80u);
    raw_put(car_mmio, d.Clock->Enable, 0);
    raw_put(car_mmio, d.Clock->Reset, d.Clock->Mask);
    raw_put(car_mmio, d.Clock->Source, 0);
    d.ResumePending = FALSE;
    UCHAR payload[2] = { 0x23, 110 };
    assert(TegraI2cTransfer(&d, 0x33, FALSE, payload, sizeof(payload), FALSE) == STATUS_SUCCESS);
    assert((raw_get(i2c_mmio, I2C_CNFG) & CNFG_PACKET_MODE) != 0);

    /* 3. Fault injection: CAR failure, config load timeout, FIFO flush timeout, busy bus on save, D3Final! */
    for (unsigned fault = 0; fault < 5; ++fault) {
        setup_bus(&d, 0);
        assert(TegraI2cEvtD0Exit(&d, 3) == STATUS_SUCCESS);
        memset(i2c_mmio, 0, sizeof(i2c_mmio));
        raw_put(car_mmio, d.Clock->Enable, 0);
        raw_put(car_mmio, d.Clock->Reset, d.Clock->Mask);
        raw_put(car_mmio, d.Clock->Source, 0);
        fail_enable = (fault == 0);
        fail_reset_clear = (fault == 1);
        fail_source = (fault == 2);
        fail_cfg_load = (fault == 3);
        fail_fifo_flush = (fault == 4);
        host_reads = host_writes = 0;
        NTSTATUS st = TegraI2cEvtD0Entry(&d, 3);
        if (fault < 3) {
            assert(st == STATUS_DEVICE_POWER_FAILURE);
            assert(host_reads == 0 && host_writes == 0); /* never touched unclocked MMIO! */
        } else {
            assert(st == STATUS_IO_TIMEOUT);
        }
        assert(d.ResumePending);
    }

    setup_bus(&d, 0);
    raw_put(i2c_mmio, I2C_STATUS, 0x100u); /* bus busy */
    assert(TegraI2cEvtD0Exit(&d, 3) == STATUS_DEVICE_BUSY && !d.ContextValid && !d.ResumePending);

    setup_bus(&d, 0);
    assert(TegraI2cEvtD0Exit(&d, 3) == STATUS_SUCCESS && d.ContextValid);
    assert(TegraI2cEvtD0Exit(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS && !d.ContextValid && !d.ResumePending);

    puts("PASS: Tegra210I2c 6-bus SC7 CAR/packet restore, neighbor clock isolation, CAR-first transfer guard and fault bounds");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="tegra-i2c-sc7-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(prefix + funcs + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
