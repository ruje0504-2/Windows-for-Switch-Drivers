#!/usr/bin/env python3
"""Exercise actual I2C D0 callbacks against clock/reset and register-loss faults."""
from pathlib import Path
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra210i2c.c").read_text()
constants = "\n".join(line for line in source.splitlines() if line.startswith("#define "))
types = source[source.index("typedef struct _TEGRA_I2C_CLOCK"):source.index("WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(TEGRA_I2C_DEVICE")]

def function(name):
    match = re.search(r"^(?:static )?(?:ULONG|VOID|BOOLEAN|NTSTATUS) " + name + r"\(", source, re.M)
    assert match
    end = source.index("\n}\n", match.start()) + 3
    return source[match.start():end]

program = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t ULONG,*PULONG;
typedef int32_t LONG,NTSTATUS;
typedef uint8_t UCHAR;
typedef void VOID,*PKEVENT,*HANDLE;
typedef bool BOOLEAN;
typedef unsigned WDF_POWER_DEVICE_STATE;
#define TRUE true
#define FALSE false
#define STATUS_SUCCESS 0
#define STATUS_DEVICE_NOT_READY -1
#define STATUS_DEVICE_BUSY -2
#define STATUS_DEVICE_POWER_FAILURE -3
#define STATUS_IO_TIMEOUT -4
#define NT_SUCCESS(x) ((x)>=0)
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define RTL_NUMBER_OF(a) (sizeof(a)/sizeof((a)[0]))
#define WdfPowerDeviceD3Final 5
'''
program += constants + "\n" + types
program += r'''
typedef PTEGRA_I2C_DEVICE WDFDEVICE;
static TEGRA_I2C_DEVICE device;
static ULONG regs[0x100/4], car[0x1000/4];
static unsigned writes, car_writes, reads, stalls, gate_acquires, gate_releases;
static bool gate_held, gate_fail, config_stuck, fifo_stuck, clock_stuck;
static PTEGRA_I2C_DEVICE TegraI2cGetDevice(WDFDEVICE d) { return d; }
static NTSTATUS TegraI2c5Acquire(PKEVENT g) {
    (void)g; assert(!gate_held);gate_acquires++;
    if(gate_fail)return STATUS_DEVICE_BUSY;
    gate_held=true;return 0;
}
static void TegraI2c5Release(PKEVENT g) {
    (void)g;assert(gate_held);gate_held=false;gate_releases++;
}
static void KeStallExecutionProcessor(unsigned us) {assert(gate_held);stalls+=us;}
static void check_access(void) {
    assert(gate_held);
    assert(car[device.Clock->Enable/4]&device.Clock->Mask);
    assert(!(car[device.Clock->Reset/4]&device.Clock->Mask));
}
static ULONG read_register(PULONG p) {
    uintptr_t addr=(uintptr_t)p;
    assert(gate_held);reads++;
    if(addr>=(uintptr_t)car && addr<(uintptr_t)(car+RTL_NUMBER_OF(car)))return *p;
    assert(addr>=(uintptr_t)regs && addr<(uintptr_t)(regs+RTL_NUMBER_OF(regs)));
    check_access();
    if(p==regs+I2C_CONFIG_LOAD/4 && !config_stuck)*p=0;
    if(p==regs+I2C_FIFO_CONTROL/4 && !fifo_stuck)*p&=~FIFO_FLUSH_TX_RX;
    return *p;
}
static void write_register(PULONG p,ULONG value) {
    const TEGRA_I2C_CLOCK *c=device.Clock;
    uintptr_t addr=(uintptr_t)p;
    assert(gate_held);writes++;
    if(addr>=(uintptr_t)car && addr<(uintptr_t)(car+RTL_NUMBER_OF(car))) {
        unsigned off=(unsigned)((UCHAR *)p-(UCHAR *)car);car_writes++;
        if(off==c->Source){assert(car[c->Reset/4]&c->Mask);*p=value;return;}
        assert(value==c->Mask); /* Never reset or gate another peripheral. */
        if(off==c->ResetSet){car[c->Reset/4]|=value;memset(regs,0,sizeof(regs));}
        else if(off==c->ResetClear){if(!clock_stuck)car[c->Reset/4]&=~value;}
        else if(off==c->EnableSet){if(!clock_stuck)car[c->Enable/4]|=value;}
        else {assert(off==c->EnableClear);car[c->Enable/4]&=~value;}
        return;
    }
    assert(addr>=(uintptr_t)regs && addr<(uintptr_t)(regs+RTL_NUMBER_OF(regs)));
    check_access();unsigned off=(unsigned)((UCHAR *)p-(UCHAR *)regs);
    assert(off!=I2C_TX_FIFO && off!=I2C_RX_FIFO && off!=4 && off!=8 && off!=12 && off!=16);
    if(off==I2C_CNFG)assert(!(value&CNFG_SEND));
    if(off==I2C_INT_STATUS)*p&=~value;else *p=value;
}
#define READ_REGISTER_ULONG(p) read_register(p)
#define WRITE_REGISTER_ULONG(p,v) write_register(p,v)
#define TegraI2cTrace(...) assert(!"Registry write in I2C power callback")
'''
for name in ("RdReg", "WrReg", "TegraI2cWaitClear", "TegraI2cReadClock", "TegraI2cWriteClock",
             "TegraI2cSaveContext", "TegraI2cRestoreContext", "TegraI2cEvtD0Entry", "TegraI2cEvtD0Exit"):
    program += function(name)
program += r'''
static void initialize(unsigned controller) {
    memset(&device,0,sizeof(device));memset(regs,0,sizeof(regs));
    memset(car,0,sizeof(car));
    device.Base=(UCHAR *)regs;device.Car=(UCHAR *)car;
    device.Clock=&TegraI2cClocks[controller];device.SdGate=&device;
    car[device.Clock->Enable/4]=0x12345678|device.Clock->Mask;
    car[device.Clock->Reset/4]=0xa5a5a5a5&~device.Clock->Mask;
    car[device.Clock->Source/4]=0xc0000004;
    regs[I2C_CNFG/4]=0x2a00;regs[I2C_CLK_DIVISOR/4]=0x50001;
    regs[I2C_SL_CNFG/4]=6;regs[I2C_TLOW_SEXT/4]=0x55;
    regs[I2C_INTERFACE_TIMING/4]=0x403;regs[I2C_HS_TIMING/4]=0x201;
    regs[I2C_FIFO_CONTROL/4]=0x60;
    writes=car_writes=reads=stalls=gate_acquires=gate_releases=0;
    gate_held=gate_fail=config_stuck=fifo_stuck=clock_stuck=false;
}
static void lose_context(void) {
    memset(regs,0,sizeof(regs));
    car[device.Clock->Enable/4]&=~device.Clock->Mask;
    car[device.Clock->Reset/4]|=device.Clock->Mask;
    car[device.Clock->Source/4]=0;
}
int main(void) {
    for(unsigned bus=0;bus<RTL_NUMBER_OF(TegraI2cClocks);bus++) {
        initialize(bus);
        /* Cold prepare captures firmware context; first D0 does not pulse reset. */
        assert(TegraI2c5Acquire(device.SdGate)==0);
        assert(TegraI2cSaveContext(&device)==0);TegraI2c5Release(device.SdGate);
        assert(TegraI2cEvtD0Entry(&device,0)==0&&car_writes==0);
        assert(regs[I2C_CNFG/4]==0x2c00&&regs[I2C_INT_MASK/4]==0);
        for(unsigned cycle=0;cycle<3;cycle++) {
            regs[I2C_INTERFACE_TIMING/4]=0x403+cycle;
            ULONG enable=car[device.Clock->Enable/4],reset=car[device.Clock->Reset/4];
            unsigned saved_writes=writes;
            assert(TegraI2cEvtD0Exit(&device,3)==0&&device.ResumePending&&device.ContextValid);
            assert(writes==saved_writes); /* Save does not disturb the shared PMIC bus. */
            lose_context();
            assert(TegraI2cEvtD0Entry(&device,3)==0&&!device.ResumePending);
            assert(car[device.Clock->Source/4]==0xc0000004);
            assert(car[device.Clock->Enable/4]==enable&&car[device.Clock->Reset/4]==reset);
            assert(regs[I2C_INTERFACE_TIMING/4]==0x403+cycle&&regs[I2C_HS_TIMING/4]==0x201);
            assert(regs[I2C_CLK_DIVISOR/4]==0x50001&&regs[I2C_FIFO_CONTROL/4]==0x60);
        }
        assert(!gate_held&&gate_acquires==gate_releases);
    }
    initialize(4);
    assert(TegraI2cEvtD0Exit(&device,3)==0);lose_context();
    clock_stuck=true;assert(TegraI2cEvtD0Entry(&device,3)==STATUS_DEVICE_POWER_FAILURE&&!gate_held);
    clock_stuck=false;config_stuck=true;
    assert(TegraI2cEvtD0Entry(&device,3)==STATUS_IO_TIMEOUT&&!gate_held&&device.ResumePending);
    config_stuck=false;fifo_stuck=true;
    assert(TegraI2cEvtD0Entry(&device,3)==STATUS_IO_TIMEOUT&&!gate_held&&device.ResumePending);
    fifo_stuck=false;assert(TegraI2cEvtD0Entry(&device,3)==0);
    regs[I2C_STATUS/4]=0x100;
    assert(TegraI2cEvtD0Exit(&device,3)==STATUS_DEVICE_BUSY&&!gate_held&&!device.ContextValid);
    unsigned before=writes;
    assert(TegraI2cEvtD0Entry(&device,3)==STATUS_DEVICE_NOT_READY&&writes==before);
    gate_fail=true;
    assert(TegraI2cEvtD0Entry(&device,3)==STATUS_DEVICE_BUSY&&!gate_held&&writes==before);
    /* Final shutdown must not reset/gate a bus that firmware may still need. */
    assert(TegraI2cEvtD0Exit(&device,WdfPowerDeviceD3Final)==0&&writes==before);
    puts("PASS: six I2C clock/reset routes, repeated context restore, no data replay, bounded failures and gate release");
}
'''

# The shared-gate problem was in the actual packet error path. Its diagnostics
# must now be issued by the request owner only after releasing that gate.
assert "TegraI2cTraceErrors(" not in function("TegraI2cTransfer")
request = function("TegraI2cRunRequest")
assert request.index("TegraI2c5Release(") < request.index("TegraI2cTraceErrors(")

with tempfile.TemporaryDirectory(prefix="tegra-i2c-context-") as temporary:
    directory = Path(temporary)
    c = directory / "context.c"
    binary = directory / "context"
    c.write_text(program)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    str(c), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
