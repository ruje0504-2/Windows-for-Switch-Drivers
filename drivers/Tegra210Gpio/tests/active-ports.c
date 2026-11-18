#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint32_t ULONG, BANK_ID, *PULONG;
typedef uint64_t ULONG64;
typedef void *PVOID;
typedef int NTSTATUS;
#define STATUS_SUCCESS 0

typedef struct { unsigned char *Base; } TEGRA_GPIO_CONTEXT, *PTEGRA_GPIO_CONTEXT;
typedef struct {
    BANK_ID BankId;
    ULONG64 EnabledMask;
    ULONG64 ActiveMask;
} GPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS, *PGPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS;

enum { bank_bytes = 256, banks = 8, ports = 4 };
static ULONG registers[banks * bank_bytes / sizeof(ULONG)];
static unsigned int current_bank, reads, addresses[8];

static ULONG checked_read(const volatile ULONG *address)
{
    size_t offset = (size_t)(address - registers) * sizeof(ULONG);
    if (offset >= sizeof(registers) || offset / bank_bytes != current_bank || reads >= 8) {
        fputs("GPIO query accessed an unexpected address\n", stderr);
        exit(2);
    }
    addresses[reads++] = (unsigned int)(offset % bank_bytes);
    return *address;
}

#define READ_REGISTER_ULONG(pointer) checked_read(pointer)
#include "gpio_driver.inc"

static unsigned int cases, mismatches, sparse_mismatches, total_reads;
static uint32_t prng = UINT32_C(0xb5495ad3);

static uint32_t random_word(void)
{
    prng ^= prng << 13;
    prng ^= prng >> 17;
    prng ^= prng << 5;
    return prng;
}

static void check(BANK_ID bank, ULONG64 requested, ULONG pending, ULONG enabled, const char *name)
{
    TEGRA_GPIO_CONTEXT ctx = { (unsigned char *)registers };
    GPIO_QUERY_ACTIVE_INTERRUPTS_PARAMETERS params = { bank, requested, UINT64_MAX };
    unsigned int expected_reads = 0, expected_addresses[8];
    const ULONG active = (ULONG)requested & pending & enabled;
    for (unsigned int port = 0; port < ports; ++port) {
        const unsigned int shift = port * 8;
        const ULONG mask = (ULONG)(requested >> shift) & 255;
        ULONG *base = registers + (bank_bytes * bank) / sizeof(ULONG);
        base[REG_INT_STA / sizeof(ULONG) + port] = ((pending >> shift) & 255) | UINT32_C(0xa5a50000);
        base[REG_INT_ENB / sizeof(ULONG) + port] = ((enabled >> shift) & 255) | UINT32_C(0x5a5a0000);
        if (mask) {
            expected_addresses[expected_reads++] = REG_INT_STA + port * 4;
            if (mask & (pending >> shift))
                expected_addresses[expected_reads++] = REG_INT_ENB + port * 4;
        }
    }
    reads = 0;
    current_bank = bank;
    NTSTATUS status = TegraGpioQueryActiveInterrupts(&ctx, &params);
    ++cases;
    total_reads += reads;
    if (status != STATUS_SUCCESS || params.ActiveMask != active || params.EnabledMask != requested)
        ++mismatches;
    if (reads != expected_reads || memcmp(addresses, expected_addresses, reads * sizeof(addresses[0])))
        ++sparse_mismatches;
    if (name)
        printf("%s,%u,%08x,%u,%u\n", name, bank, (unsigned int)params.ActiveMask, reads, expected_reads);
}

int main(int argc, char **argv)
{
    const int sparse = argc == 2 && strcmp(argv[1], "--require-sparse") == 0;
    check(5, 0, UINT32_MAX, UINT32_MAX, "no_requested_pins");
    check(5, UINT64_C(0xffffffff00000000), UINT32_MAX, UINT32_MAX, "outside_32_pin_bank");
    check(5, UINT32_C(0xc2000000), 0, UINT32_MAX, "quiet_touch_and_volume_port");
    check(5, UINT32_C(0xc2000000), UINT32_C(0x02000000), UINT32_MAX, "touch_active");
    check(5, UINT32_C(0xc2000000), UINT32_C(0x40000000), UINT32_MAX, "volume_up_active");
    check(5, UINT32_C(0xc2000000), UINT32_C(0x80000000), UINT32_C(0x42000000), "masked_volume_down");
    check(5, UINT32_C(0xc2000000), 1, UINT32_MAX, "other_port_pending");
    check(7, UINT32_MAX, UINT32_MAX, UINT32_MAX, "full_bank");
    for (unsigned int bank = 0; bank < banks; ++bank) {
        for (unsigned int i = 0; i < 4096; ++i) {
            uint32_t requested = random_word(), pending = random_word(), enabled = random_word();
            if (i % 3 == 0)
                requested &= UINT32_C(0xff) << (8 * (i % 4));
            check(bank, (UINT64_C(0x31415926) << 32) | requested, pending, enabled, NULL);
        }
    }
    printf("cases,%u,mask_errors,%u,sparse_address_errors,%u,register_reads,%u\n",
           cases, mismatches, sparse_mismatches, total_reads);
    return mismatches || (sparse && sparse_mismatches);
}
