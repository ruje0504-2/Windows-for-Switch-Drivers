#!/usr/bin/env python3
"""Run actual GPIO power callbacks against a register-loss/wake-event model."""
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra210gpio.c").read_text()
types = source[source.index("#define TEGRA_GPIO_BANKS"):source.index("DRIVER_INITIALIZE")]

def function(name):
    position = source.index(name + "(")
    start = source.rfind("\nstatic ", 0, position) + 1
    end = source.index("\n}\n", position) + 3
    return source[start:end]

program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t UCHAR;
typedef uint32_t ULONG, *PULONG;
typedef unsigned int BANK_ID;
typedef int NTSTATUS, WDF_POWER_DEVICE_STATE;
typedef void VOID, *PVOID;
typedef bool BOOLEAN;
#define TRUE true
#define FALSE false
#define STATUS_SUCCESS 0
#define STATUS_INVALID_DEVICE_STATE -1
#define STATUS_DEVICE_NOT_READY -2
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define TegraGpioTrace(...) assert(!"Registry I/O in GPIO power callbacks")
'''
program += types
program += r'''
static ULONG hardware[TEGRA_GPIO_MMIO_SIZE / 4], expected[TEGRA_GPIO_MMIO_SIZE / 4];
static unsigned writes, reads;
static bool out_ready[8][4], check_order;
static const ULONG saved_regs[] = {REG_CNF, REG_OE, REG_OUT, REG_INT_ENB,
                                  REG_INT_LVL, REG_DBC_EN, REG_DBC_CNT};
static ULONG read_register(PULONG p) {
    assert(p >= hardware && p < hardware + sizeof(hardware)/sizeof(*hardware));
    reads++; return *p;
}
static void write_register(PULONG p, ULONG value) {
    assert(p >= hardware && p < hardware + sizeof(hardware)/sizeof(*hardware));
    unsigned offset = (unsigned)((UCHAR *)p - (UCHAR *)hardware);
    unsigned bank = offset / 0x100, port = (offset & 0xf) / 4, reg = offset & 0xf0;
    assert(bank < 8);
    assert(reg != REG_INT_CLR && reg != REG_INT_STA && reg != REG_IN);
    if (check_order) {
        if (writes < 32) assert(reg == REG_INT_ENB && value == 0);
        if (reg == REG_OUT) out_ready[bank][port] = true;
        if (reg == REG_OE || reg == REG_CNF) assert(out_ready[bank][port]);
        if (reg == REG_INT_ENB && value) {
            /* No interrupt becomes live while any pin's context is incomplete. */
            for (unsigned b=0;b<8;b++) for (unsigned n=0;n<4;n++)
                for (unsigned i=0;i<sizeof(saved_regs)/sizeof(*saved_regs);i++) {
                    if (saved_regs[i] == REG_INT_ENB) continue;
                    unsigned index=(b*0x100+n*4+saved_regs[i])/4;
                    assert(hardware[index] == expected[index]);
                }
        }
    }
    writes++;
    if (reg == REG_DBC_EN) {
        ULONG mask=(value>>8)&0xff;
        *p=(*p&~mask)|(value&mask);
    } else *p=value;
}
#define READ_REGISTER_ULONG(p) read_register(p)
#define WRITE_REGISTER_ULONG(p,v) write_register(p,v)
'''
program += function("TegraGpioReg")
program += function("TegraGpioStartController")
program += function("TegraGpioStopController")
program += r'''
static void initialize(unsigned cycle) {
    memset(hardware,0,sizeof(hardware));
    for (unsigned b=0;b<8;b++) for (unsigned p=0;p<4;p++) {
        unsigned base=(b*0x100+p*4)/4;
        for (unsigned i=0;i<sizeof(saved_regs)/sizeof(*saved_regs);i++) {
            ULONG value=(17*b+11*p+53*i+cycle)|1;
            if (saved_regs[i] == REG_INT_LVL) value=value*0x010101;
            else value&=0xff;
            hardware[base+saved_regs[i]/4]=value;
        }
    }
    memcpy(expected,hardware,sizeof(hardware));
    memset(out_ready,0,sizeof(out_ready)); writes=reads=0;check_order=false;
}
int main(void) {
    TEGRA_GPIO_CONTEXT ctx={0};
    assert(TegraGpioStartController(&ctx,false,0)==STATUS_DEVICE_NOT_READY);
    ctx.Base=(UCHAR *)hardware;ctx.Length=sizeof(hardware);
    initialize(0);
    assert(TegraGpioStartController(&ctx,true,3)==STATUS_INVALID_DEVICE_STATE);
    assert(writes==0 && reads==0);
    assert(TegraGpioStartController(&ctx,false,0)==0);
    assert(writes==0 && reads==0);
    for (unsigned cycle=1;cycle<=4;cycle++) {
        initialize(cycle);
        assert(TegraGpioStopController(&ctx,true,3)==0 && ctx.SavedValid);
        assert(writes==0); /* Saving must preserve configured wake/power pins. */
        /* Lose programmable registers, but retain a newly latched wake event. */
        memset(hardware,0,sizeof(hardware));
        for (unsigned b=0;b<8;b++) for (unsigned p=0;p<4;p++) {
            hardware[(b*0x100+p*4+REG_INT_STA)/4]=0xa5;
            hardware[(b*0x100+p*4+REG_IN)/4]=0x5a;
            expected[(b*0x100+p*4+REG_INT_STA)/4]=0xa5;
            expected[(b*0x100+p*4+REG_IN)/4]=0x5a;
        }
        check_order=true;
        assert(TegraGpioStartController(&ctx,true,3)==0 && !ctx.SavedValid);
        assert(memcmp(hardware,expected,sizeof(hardware))==0);
    }
    initialize(9);
    assert(TegraGpioStopController(&ctx,true,3)==0);
    writes=reads=0;
    assert(TegraGpioStopController(&ctx,false,4)==0 && !ctx.SavedValid);
    assert(reads==0 && writes==0);
    assert(TegraGpioStartController(&ctx,true,3)==STATUS_INVALID_DEVICE_STATE);
    assert(reads==0 && writes==0);
    assert(TegraGpioStopController(&ctx,true,3)==0);
    assert(TegraGpioStartController(&ctx,false,0)==0 && !ctx.SavedValid);
    puts("PASS: repeated GPIO register-loss restore, output/IRQ ordering and wake preservation");
}
'''
with tempfile.TemporaryDirectory(prefix="tegra-gpio-context-") as temporary:
    directory = Path(temporary)
    c = directory / "context.c"
    binary = directory / "context"
    c.write_text(program)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
