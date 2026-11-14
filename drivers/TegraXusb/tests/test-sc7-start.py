#!/usr/bin/env python3
"""Exercise TegraXusb Start/Stop across retained D3 vs SC7 Falcon power loss."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "src/tegraxusb.c").read_text()

def function(name):
    m = re.search(r"^(?:static\s+)?(?:void|ULONG|BOOLEAN|NTSTATUS)\s+" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
    assert m, f"missing {name}"
    end = source.index("{", m.start()) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[m.start():end] + "\n"

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
''' + f'#include "{root / "src/mailbox.h"}"\n' + r'''

typedef uint32_t ULONG, *PULONG;
typedef uint8_t UCHAR, *PUCHAR, BOOLEAN;
typedef int32_t NTSTATUS;
typedef unsigned WDF_POWER_DEVICE_STATE;
typedef void *WDFINTERRUPT, *WDFDEVICE;
typedef struct { const wchar_t *Buffer; } UNICODE_STRING;
typedef const UNICODE_STRING *PCUNICODE_STRING;
typedef union { int64_t QuadPart; } LARGE_INTEGER;

#define TRUE 1
#define FALSE 0
#define NT_SUCCESS(s) ((s) >= 0)
#define STATUS_SUCCESS 0
#define STATUS_IO_TIMEOUT ((NTSTATUS)0xC00000B5)
#define STATUS_DEVICE_HARDWARE_ERROR ((NTSTATUS)0xC0000182)
#define MAXULONG UINT32_MAX
#define KernelMode 0
#define WdfPowerDeviceD3 3
#define WdfPowerDeviceD3Final 5
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define DECLARE_CONST_UNICODE_STRING(var, str) const UNICODE_STRING var = { str }

enum {
    MailCmd = 0xe4,
    MailIn = 0xe8,
    MailOut = 0xec,
    MailOwner = 0xf0,
    SmiIntr = 0x428
};
#define TO_FALCON ((ULONG)(1u << 27))
#define TO_HOST ((ULONG)(1u << 29))
#define MAIL_IRQ ((ULONG)(1u << 31))

typedef struct {
    PUCHAR Fpci;
    PULONG Lane;
    WDFINTERRUPT Interrupt;
    ULONG Messages;
    BOOLEAN Enabled, Notified, Hung;
} SWUSB_DEVICE;

static SWUSB_DEVICE *SwUsbDevice(WDFDEVICE d) { return (SWUSB_DEVICE *)d; }

static UCHAR fpci[0x1000];
static ULONG lane_reg;
static bool irq_locked = false;
static bool falcon_alive = true;
static ULONG traced_start_status = 0xDEADBEEFu;

static void WdfInterruptAcquireLock(WDFINTERRUPT i) { (void)i; assert(!irq_locked); irq_locked = true; }
static void WdfInterruptReleaseLock(WDFINTERRUPT i) { (void)i; assert(irq_locked); irq_locked = false; }
static void Trace(WDFDEVICE d, PCUNICODE_STRING n, ULONG v) {
    (void)d;
    assert(!irq_locked);
    if (!wcscmp(n->Buffer, L"MailboxStartStatus")) traced_start_status = v;
}
static NTSTATUS KeDelayExecutionThread(int m, BOOLEAN a, LARGE_INTEGER *i) { (void)m; (void)a; (void)i; return 0; }

static ULONG raw_get(ULONG o) { ULONG v; memcpy(&v, fpci + o, 4); return v; }
static void raw_put(ULONG o, ULONG v) { memcpy(fpci + o, &v, 4); }

static ULONG mock_read(PULONG p) {
    if (p == &lane_reg) return lane_reg;
    ULONG off = (ULONG)((PUCHAR)p - fpci);
    return raw_get(off);
}
static void mock_write(PULONG p, ULONG v) {
    if (p == &lane_reg) { lane_reg = v; return; }
    ULONG off = (ULONG)((PUCHAR)p - fpci);
    if (!falcon_alive) {
        /* Unpowered/unloaded Falcon after SC7 ignores host mailbox ownership/IRQ! */
        if (off == MailOwner) return;
        if (off == MailCmd) { raw_put(off, 0); return; }
    }
    raw_put(off, v);
    if (falcon_alive && off == MailCmd && (v & TO_FALCON)) {
        /* Live Falcon consumes SwUsbEnableMessages and releases MailOwner to 0, keeping MAIL_IRQ set! */
        raw_put(MailOwner, 0);
        raw_put(MailCmd, v & ~TO_FALCON);
    }
}

#define READ_REGISTER_ULONG(p) mock_read((PULONG)(p))
#define WRITE_REGISTER_ULONG(p, v) mock_write((PULONG)(p), (v))
'''

funcs = "".join(function(n) for n in ["Read", "Write", "Start", "Stop"])

tests = r'''
int main(void) {
    SWUSB_DEVICE d;
    memset(&d, 0, sizeof(d));
    memset(fpci, 0, sizeof(fpci));
    d.Fpci = fpci;
    d.Lane = &lane_reg;
    d.Interrupt = (WDFINTERRUPT)&d;

    /* 1. Cold boot with live Falcon succeeds and arms MAIL_IRQ + Notified=TRUE. */
    falcon_alive = true;
    assert(Start(&d, WdfPowerDeviceD3Final) == STATUS_SUCCESS);
    assert(d.Notified && (raw_get(MailCmd) & MAIL_IRQ) != 0 && traced_start_status == 0);

    /* 2. Retained D3 (non-SC7) preserves MAIL_IRQ -> Start succeeds immediately. */
    assert(Stop(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && d.Notified);
    assert(Start(&d, WdfPowerDeviceD3) == STATUS_SUCCESS && d.Notified);

    /* 3. SC7 power loss with unpowered/unreloaded Falcon wipes FPCI (MAIL_IRQ == 0):
     *    Start MUST clear stale Notified and return STATUS_IO_TIMEOUT (NEVER fake success!). */
    memset(fpci, 0, sizeof(fpci));
    falcon_alive = false;
    assert(Start(&d, WdfPowerDeviceD3) == STATUS_IO_TIMEOUT);
    assert(!d.Notified && traced_start_status == (ULONG)STATUS_IO_TIMEOUT);

    /* 4. Once Falcon firmware is reloaded/alive after SC7, Start re-sends SwUsbEnableMessages and succeeds! */
    falcon_alive = true;
    assert(Start(&d, WdfPowerDeviceD3) == STATUS_SUCCESS);
    assert(d.Notified && (raw_get(MailCmd) & MAIL_IRQ) != 0 && traced_start_status == 0);

    /* 5. All-ones MMIO readback (power-gated aperture) returns STATUS_DEVICE_HARDWARE_ERROR! */
    memset(fpci, 0xFF, sizeof(fpci));
    assert(Start(&d, WdfPowerDeviceD3) == STATUS_DEVICE_HARDWARE_ERROR);

    puts("PASS: TegraXusb SC7 stale-Notified invalidation, unpowered Falcon timeout honesty and recovery");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="tegraxusb-sc7-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(prefix + funcs + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
