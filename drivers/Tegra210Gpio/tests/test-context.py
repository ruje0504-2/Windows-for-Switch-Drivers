#!/usr/bin/env python3
"""Exercise Tegra210Gpio 8-bank SC7 context save/restore ordering and interrupt masking."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra210gpio.c").read_text()

def function(name):
    m = re.search(r"^(?:static\s+)?(?:volatile\s+)?(?:VOID|ULONG|ULONG64|NTSTATUS)\s*\*?\s*" + re.escape(name) + r"\([^;]*?\)\s*\{", source, re.M)
    assert m, f"missing {name}"
    end = source.index("{", m.start()) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[m.start():end] + "\n"

defines = "\n".join(re.findall(r"^#define (?:TEGRA_GPIO_|REG_|LVL_)[^\n]+", source, re.M))

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

typedef uint32_t ULONG, *PULONG, PIN_NUMBER;
typedef uint16_t BANK_ID;
typedef uint64_t ULONG64;
typedef uint8_t UCHAR, BOOLEAN;
typedef int32_t NTSTATUS;
typedef void VOID, *PVOID;
typedef unsigned WDF_POWER_DEVICE_STATE;
typedef enum { LevelSensitive = 0, Latched = 1 } KINTERRUPT_MODE;
typedef enum { InterruptActiveHigh = 1, InterruptActiveLow = 2, InterruptActiveBoth = 3 } KINTERRUPT_POLARITY;

#define TRUE 1
#define FALSE 0
#define STATUS_SUCCESS 0
#define STATUS_DEVICE_NOT_READY -1
#define STATUS_INVALID_DEVICE_STATE -2
#define STATUS_INVALID_PARAMETER -3
#define UNREFERENCED_PARAMETER(x) ((void)(x))
''' + defines + r'''

typedef struct _TEGRA_GPIO_PORT_STATE {
    ULONG Cnf, Oe, Out, InterruptEnable, InterruptLevel;
    ULONG DebounceEnable, DebounceCount;
} TEGRA_GPIO_PORT_STATE;

typedef struct _TEGRA_GPIO_CONTEXT {
    volatile UCHAR *Base;
    ULONG Length;
    TEGRA_GPIO_PORT_STATE Saved[TEGRA_GPIO_BANKS][4];
    BOOLEAN SavedValid;
} TEGRA_GPIO_CONTEXT, *PTEGRA_GPIO_CONTEXT;

typedef struct {
    BANK_ID BankId;
    PIN_NUMBER PinNumber;
    KINTERRUPT_MODE InterruptMode;
    KINTERRUPT_POLARITY Polarity;
} GPIO_ENABLE_INTERRUPT_PARAMETERS, *PGPIO_ENABLE_INTERRUPT_PARAMETERS;

typedef struct {
    BANK_ID BankId;
    ULONG64 EnabledMask;
    ULONG64 ActiveMask;
} GPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS, *PGPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS;

static UCHAR mmio[TEGRA_GPIO_MMIO_SIZE];
static PTEGRA_GPIO_CONTEXT active_ctx = NULL;
static bool restoring = false;
static bool any_unmasked_during_config = false;
static unsigned trace_calls = 0;

static void TegraGpioTrace(const wchar_t *name, ULONG val) {
    (void)name; (void)val;
    /* StartController(RestoreContext=TRUE) must NEVER touch registry I/O! */
    assert(!restoring);
    trace_calls++;
}

static ULONG get_reg(BANK_ID b, ULONG p, ULONG r) {
    ULONG v;
    memcpy(&v, mmio + b * 0x100 + p * 4 + r, 4);
    return v;
}

static void put_reg(BANK_ID b, ULONG p, ULONG r, ULONG v) {
    memcpy(mmio + b * 0x100 + p * 4 + r, &v, 4);
}

static ULONG mock_read(PULONG addr) {
    ULONG v;
    memcpy(&v, addr, 4);
    return v;
}

static void mock_write(PULONG addr, ULONG val) {
    uintptr_t off = (uintptr_t)((UCHAR *)addr - mmio);
    assert(off + 4 <= sizeof(mmio));
    ULONG bank = (ULONG)(off / 0x100);
    ULONG rem = (ULONG)(off % 0x100);
    ULONG port = (rem & 0x0F) / 4;
    ULONG reg = rem & 0xF0;

    /* Handle masked write alias (reg + 0x80): (mask << 8) | bits */
    if (reg >= REG_MASKED && reg != REG_DBC_EN && reg != REG_DBC_CNT) {
        ULONG base_reg = reg - REG_MASKED;
        ULONG mask = (val >> 8) & 0xFFu;
        ULONG bits = val & mask;
        ULONG cur = get_reg((BANK_ID)bank, port, base_reg);
        val = (cur & ~mask) | bits;
        addr = (PULONG)(mmio + bank * 0x100 + port * 4 + base_reg);
        reg = base_reg;
    } else if (reg == REG_DBC_EN && (val & 0xFF00u) != 0) {
        ULONG mask = (val >> 8) & 0xFFu;
        ULONG bits = val & mask;
        ULONG cur = get_reg((BANK_ID)bank, port, REG_DBC_EN);
        val = (cur & ~mask) | bits;
    }

    if (restoring) {
        /* Never clear pending wake edges in INT_STA / INT_CLR during restore! */
        assert(reg != REG_INT_STA && reg != REG_INT_CLR);
        /* OE must only be restored AFTER OUT and CNF are already written! */
        if (reg == REG_OE && val != 0) {
            assert(active_ctx != NULL);
            assert(get_reg((BANK_ID)bank, port, REG_OUT) == active_ctx->Saved[bank][port].Out);
            assert(get_reg((BANK_ID)bank, port, REG_CNF) == active_ctx->Saved[bank][port].Cnf);
        }
        /* While configuring OUT/CNF/OE/DBC/INT_LVL, ALL banks/ports must have INT_ENB == 0! */
        if (reg == REG_OUT || reg == REG_CNF || reg == REG_OE || reg == REG_DBC_CNT || reg == REG_DBC_EN || reg == REG_INT_LVL) {
            for (ULONG b = 0; b < TEGRA_GPIO_BANKS; ++b) {
                for (ULONG p = 0; p < 4; ++p) {
                    if (get_reg((BANK_ID)b, p, REG_INT_ENB) != 0) {
                        any_unmasked_during_config = true;
                    }
                }
            }
        }
    }
    memcpy(addr, &val, 4);
}

#define READ_REGISTER_ULONG(p) mock_read((PULONG)(p))
#define WRITE_REGISTER_ULONG(p, v) mock_write((PULONG)(p), (v))
'''

funcs = "".join(function(n) for n in [
    "TegraGpioReg",
    "TegraGpioMaskedWrite",
    "TegraGpioReadBank",
    "TegraGpioLevelType",
    "TegraGpioSetLevel",
    "TegraGpioStartController",
    "TegraGpioStopController",
    "TegraGpioUnmaskInterrupt",
    "TegraGpioQueryActiveInterrupts",
])

tests = r'''
int main(void) {
    TEGRA_GPIO_CONTEXT ctx;
    memset(&ctx, 0, sizeof(ctx));
    memset(mmio, 0, sizeof(mmio));
    active_ctx = &ctx;

    /* 1. Reject null base and invalid restore state cleanly. */
    assert(TegraGpioStopController(&ctx, TRUE, 3) == STATUS_DEVICE_NOT_READY);
    assert(TegraGpioStartController(&ctx, TRUE, 3) == STATUS_DEVICE_NOT_READY);
    ctx.Base = mmio;
    ctx.Length = sizeof(mmio);
    assert(TegraGpioStartController(&ctx, FALSE, 3) == STATUS_SUCCESS && !ctx.SavedValid);
    assert(TegraGpioStartController(&ctx, TRUE, 3) == STATUS_INVALID_DEVICE_STATE);

    /* 2. Populate all 8 banks x 4 ports with distinct GPIO/output/IRQ/debounce settings. */
    for (ULONG b = 0; b < TEGRA_GPIO_BANKS; ++b) {
        for (ULONG p = 0; p < 4; ++p) {
            ULONG seed = (b * 4 + p + 1);
            put_reg((BANK_ID)b, p, REG_CNF, (seed * 3u) & 0xFFu);
            put_reg((BANK_ID)b, p, REG_OUT, (seed * 5u) & 0xFFu);
            put_reg((BANK_ID)b, p, REG_OE,  (seed * 7u) & 0xFFu);
            put_reg((BANK_ID)b, p, REG_INT_ENB, (seed * 11u) & 0xFFu);
            put_reg((BANK_ID)b, p, REG_INT_LVL, ((seed & 0xFFu) << 16) | ((seed * 2u & 0xFFu) << 8) | (seed * 9u & 0xFFu));
            put_reg((BANK_ID)b, p, REG_DBC_EN,  (seed * 13u) & 0xFFu);
            put_reg((BANK_ID)b, p, REG_DBC_CNT, (seed * 17u) & 0xFFu);
        }
    }

    /* 3. Repeated SC7 cycles: save -> full MMIO wipe + latched wake edge in INT_STA -> restore. */
    for (unsigned cycle = 0; cycle < 4; ++cycle) {
        assert(TegraGpioStopController(&ctx, TRUE, 3) == STATUS_SUCCESS && ctx.SavedValid);
        /* StopController must leave wake INT_ENB intact before SC7 entry! */
        assert(get_reg(2, 1, REG_INT_ENB) == ((10u * 11u) & 0xFFu));

        /* Simulate SC7 power loss + hardware wake latch on bank 2 port 1 bit 1. */
        memset(mmio, 0, sizeof(mmio));
        put_reg(2, 1, REG_INT_STA, 0x02u);

        restoring = true;
        any_unmasked_during_config = false;
        assert(TegraGpioStartController(&ctx, TRUE, 3) == STATUS_SUCCESS);
        restoring = false;
        assert(!any_unmasked_during_config);
        assert(!ctx.SavedValid); /* single-use snapshot consumed */

        /* Verify every bank/port register and preserved wake INT_STA! */
        assert(get_reg(2, 1, REG_INT_STA) == 0x02u);
        for (ULONG b = 0; b < TEGRA_GPIO_BANKS; ++b) {
            for (ULONG p = 0; p < 4; ++p) {
                ULONG seed = (b * 4 + p + 1);
                assert(get_reg((BANK_ID)b, p, REG_CNF) == ((seed * 3u) & 0xFFu));
                assert(get_reg((BANK_ID)b, p, REG_OUT) == ((seed * 5u) & 0xFFu));
                assert(get_reg((BANK_ID)b, p, REG_OE)  == ((seed * 7u) & 0xFFu));
                assert(get_reg((BANK_ID)b, p, REG_INT_ENB) == ((seed * 11u) & 0xFFu));
                assert(get_reg((BANK_ID)b, p, REG_INT_LVL) == (((seed & 0xFFu) << 16) | ((seed * 2u & 0xFFu) << 8) | (seed * 9u & 0xFFu)));
                assert(get_reg((BANK_ID)b, p, REG_DBC_EN)  == ((seed * 13u) & 0xFFu));
                assert(get_reg((BANK_ID)b, p, REG_DBC_CNT) == ((seed * 17u) & 0xFFu));
            }
        }

        /* QueryActiveInterrupts sees the preserved wake status immediately. */
        GPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS q = { .BankId = 2, .EnabledMask = 0xFFFFFFFFULL, .ActiveMask = 0 };
        assert(TegraGpioQueryActiveInterrupts(&ctx, &q) == STATUS_SUCCESS);
        assert((q.ActiveMask & (1ULL << 9)) != 0); /* port 1 bit 1 */
    }

    /* 4. UnmaskInterrupt re-programs INT_LVL if lost before unmasking INT_ENB! */
    memset(mmio, 0, sizeof(mmio));
    GPIO_ENABLE_INTERRUPT_PARAMETERS unmask = {
        .BankId = 5,
        .PinNumber = 19, /* port 2 bit 3 */
        .InterruptMode = Latched,
        .Polarity = InterruptActiveLow,
    };
    assert(TegraGpioUnmaskInterrupt(&ctx, &unmask) == STATUS_SUCCESS);
    assert(get_reg(5, 2, REG_INT_LVL) == (LVL_EDGE_FALLING << 3));
    assert(get_reg(5, 2, REG_INT_ENB) == (1u << 3));

    puts("PASS: Tegra210Gpio 8-bank SC7 save/restore ordering, masked IRQ restore, preserved wake status and UnmaskInterrupt level recovery");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="tegra-gpio-sc7-") as tmp:
    c = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    c.write_text(prefix + funcs + tests)
    subprocess.run([os.environ.get("CC", "clang"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
