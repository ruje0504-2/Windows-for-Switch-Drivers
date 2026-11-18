"""Run the controller's board-specific D0 diagnostics against substituted registers."""
import os
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'src/tegra210i2c.c').read_text()

def definition(signature):
    start = source.index(signature)
    brace = source.index('{', start)
    return source[start:source.index('\n}', brace) + 2]

stubs = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <wchar.h>
typedef void VOID;
typedef uint32_t ULONG, *PULONG;
typedef int32_t NTSTATUS;
typedef struct { uint64_t QuadPart; } PHYSICAL_ADDRESS;
typedef struct { ULONG PhysLow, Hidrev; VOID *SdGate; } DEVICE, *PTEGRA_I2C_DEVICE;
typedef DEVICE *WDFDEVICE;
typedef int WDF_POWER_DEVICE_STATE;
#define PAGE_READONLY 2u
#define PAGE_NOCACHE 512u
#define STATUS_SUCCESS 0
#define STATUS_NOT_SUPPORTED ((NTSTATUS)0xC00000BBu)
#define NT_SUCCESS(x) ((x) >= 0)
#define UNREFERENCED_PARAMETER(x) (void)(x)
#define TegraI2cGetDevice(x) (x)
static ULONG chip, mapped, reads, unmaps, ports[4], regs[4], calls, skipped, done, acqs, rels;
static int map_fails, gate_fails;
static PULONG MmMapIoSpaceEx(PHYSICAL_ADDRESS address, size_t bytes, ULONG flags) {
    assert(address.QuadPart == 0x70000804 && bytes == sizeof(ULONG));
    assert(flags == (PAGE_READONLY | PAGE_NOCACHE));
    ++mapped;
    return map_fails ? NULL : &chip;
}
static ULONG READ_REGISTER_ULONG(PULONG address) { assert(address == &chip); ++reads; return *address; }
static void MmUnmapIoSpace(PULONG address, size_t bytes) {
    assert(address == &chip && bytes == sizeof(ULONG)); ++unmaps;
}
static NTSTATUS TegraI2c5Acquire(VOID *gate) { (void)gate; ++acqs; return gate_fails ? -9 : 0; }
static VOID TegraI2c5Release(VOID *gate) { (void)gate; ++rels; }
static VOID TegraI2cSelfTest(DEVICE *dev, unsigned address, unsigned reg, const wchar_t *name) {
    (void)dev; (void)name; assert(calls < 4); ports[calls] = address; regs[calls++] = reg;
}
static VOID TegraI2cTrace(DEVICE *dev, const wchar_t *name, ULONG value) {
    (void)dev;
    if (!wcscmp(name, L"SelfTest_MAX77621_Vout")) {
        assert(value == (ULONG)STATUS_NOT_SUPPORTED); ++skipped;
    } else { assert(!wcscmp(name, L"D0EntryDone") && value == 1); ++done; }
}
'''
tests = r'''
int main(void) {
    chip = 0x12127u;
    assert(TegraI2cReadHidrev() == chip && mapped == 1 && reads == 1 && unmaps == 1);
    map_fails = 1;
    assert(TegraI2cReadHidrev() == 0 && mapped == 2 && reads == 1 && unmaps == 1);
    DEVICE d = {0x7000d000u, 0x12113u, NULL};
    assert(TegraI2cEvtD0Entry(&d, 0) == 0);
    assert(calls == 2 && ports[0] == 0x3c && regs[0] == 0x15 && ports[1] == 0x1b && regs[1] == 0);
    assert(skipped == 0 && done == 1 && rels == 1);
    const ULONG other[] = {0x12127u, 0u, 0xffffffffu, 0x12310u};
    for (unsigned i=0; i<sizeof(other)/sizeof(other[0]); ++i) {
        calls = 0; d.Hidrev = other[i];
        assert(TegraI2cEvtD0Entry(&d, 0) == 0 && calls == 1);
        assert(ports[0] == 0x3c && regs[0] == 0x15 && skipped == i+1);
    }
    d.PhysLow = 0x7000c000u; calls = 0;
    assert(TegraI2cEvtD0Entry(&d, 0) == 0 && calls == 3);
    assert(ports[0] == 0x4c && regs[0] == 0 && ports[1] == 0x4c && regs[1] == 1);
    assert(ports[2] == 0x6b && regs[2] == 0x0a);
    d.PhysLow = 0x7000c500u; calls = 0;
    assert(TegraI2cEvtD0Entry(&d, 0) == 0 && calls == 0);
    assert(done == 7 && acqs == 7 && rels == 7);
    gate_fails = 1;
    assert(TegraI2cEvtD0Entry(&d, 0) == -9 && calls == 0 && acqs == 8 && rels == 7 && done == 7);
    return 0;
}
'''
d0 = 'NTSTATUS TegraI2cEvtD0Entry(WDFDEVICE Device, WDF_POWER_DEVICE_STATE PreviousState)\n{'
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    case = root / 'diagnostics.c'
    case.write_text(stubs + '\n' + definition('static ULONG TegraI2cReadHidrev(VOID)') + '\n' + definition(d0) + '\n' + tests)
    exe = root / 'diagnostics'
    subprocess.run([os.environ.get('CC','cc'), '-std=c11', '-Wall', '-Wextra', '-Werror', str(case), '-o', str(exe)], check=True)
    subprocess.run([exe], check=True)
print('Tegra210, B01, unknown revisions, mapping, I2C1/3 and gate diagnostics passed.')
