/*
 * Tegra210 I2C controller driver for Windows on the Nintendo Switch.
 * SpbCx (simple peripheral bus) controller, KMDF.
 *
 * Version 1 transfers by polling in packet mode, the same sequence the UEFI
 * UsbPowerAudit driver uses for BM92T/BQ24193/MAX17050 (R288 helpers) and
 * Linux i2c-tegra uses per message:
 *   header 0: PROTOCOL_I2C (1 << 4) | PACKET_ID 1 (1 << 16)
 *   header 1: payload size - 1
 *   header 2: (addr << 1) | READ (bit 19) | REPEAT_START (bit 16, not last)
 *   then payload words through TX_FIFO / RX_FIFO, wait PACKET_XFER_COMPLETE.
 * Preserve the firmware's clock source/dividers and timing. On D0 reentry,
 * restore this controller's clock/reset and packet-mode context before SPB I/O.
 */
#include <ntddk.h>
#include <ntstrsafe.h>
#include <wdf.h>
#include <spbcx.h>

#include "../../common/tegra_i2c5_gate.h"

#define I2C_CNFG            0x000
#define I2C_STATUS          0x01C
#define I2C_SL_CNFG         0x020
#define I2C_TLOW_SEXT       0x034
#define I2C_TX_FIFO         0x050
#define I2C_RX_FIFO         0x054
#define I2C_FIFO_CONTROL    0x05C
#define I2C_FIFO_STATUS     0x060
#define I2C_INT_MASK        0x064
#define I2C_INT_STATUS      0x068
#define I2C_CLK_DIVISOR     0x06C
#define I2C_BUS_CLEAR_CONFIG 0x084
#define I2C_BUS_CLEAR_STATUS 0x088
#define I2C_CONFIG_LOAD     0x08C
#define I2C_CLKEN_OVERRIDE  0x090
#define I2C_INTERFACE_TIMING 0x094
#define I2C_HS_TIMING       0x09C
#define I2C_MMIO_SIZE       0x100

#define CNFG_PACKET_MODE    (1u << 10)
#define CNFG_SEND           (1u << 9)
#define FIFO_FLUSH_TX_RX    0x3u
#define INT_ARB_LOST        (1u << 2)
#define INT_NO_ACK          (1u << 3)
#define INT_ERR_MASK        0x3Cu
#define INT_ALL_PACKETS     (1u << 6)
#define INT_PACKET_DONE     (1u << 7)
#define INT_BUS_CLEAR_DONE  (1u << 11)
#define HDR0_I2C_PACKET1    0x10010u
#define HDR_REPEAT_START    (1u << 16)
#define HDR_READ            (1u << 19)
#define CONFIG_LOAD_ALL     0x25u
#define I2C_MAX_TRANSFER    4096

#include <pshpack1.h>
typedef struct _PNP_SERIAL_BUS_DESCRIPTOR {
    UCHAR Tag;
    USHORT Length;
    UCHAR RevisionId;
    UCHAR ResourceSourceIndex;
    UCHAR SerialBusType;
    UCHAR GeneralFlags;
    USHORT TypeSpecificFlags;
    UCHAR TypeSpecificRevisionId;
    USHORT TypeDataLength;
} PNP_SERIAL_BUS_DESCRIPTOR, *PPNP_SERIAL_BUS_DESCRIPTOR;

typedef struct _PNP_I2C_SERIAL_BUS_DESCRIPTOR {
    PNP_SERIAL_BUS_DESCRIPTOR SerialBusDescriptor;
    ULONG ConnectionSpeed;
    USHORT SlaveAddress;
} PNP_I2C_SERIAL_BUS_DESCRIPTOR, *PPNP_I2C_SERIAL_BUS_DESCRIPTOR;
#include <poppack.h>

typedef struct _RH_QUERY_CONNECTION_PROPERTIES_OUTPUT_BUFFER {
    ULONG PropertiesLength;
    UCHAR ConnectionProperties[ANYSIZE_ARRAY];
} RH_QUERY_CONNECTION_PROPERTIES_OUTPUT_BUFFER, *PRH_QUERY_CONNECTION_PROPERTIES_OUTPUT_BUFFER;

#define I2C_SERIAL_BUS_TYPE 0x01
#define I2C_10BIT_ADDRESS_FLAG 0x0001

typedef struct _TEGRA_I2C_CLOCK {
    ULONG Base, Source, Enable, EnableSet, EnableClear;
    ULONG Reset, ResetSet, ResetClear, Mask;
} TEGRA_I2C_CLOCK;

/* Shared T210/B01 CAR layout, matching Hekate's clock_i2c table. Use set/clear
 * aliases so recovery of one bus does not change another peripheral's clock. */
static const TEGRA_I2C_CLOCK TegraI2cClocks[] = {
    {0x7000c000,0x124,0x010,0x320,0x324,0x004,0x300,0x304,1u<<12},
    {0x7000c400,0x198,0x014,0x328,0x32c,0x008,0x308,0x30c,1u<<22},
    {0x7000c500,0x1b8,0x018,0x330,0x334,0x00c,0x310,0x314,1u<<3},
    {0x7000c700,0x3c4,0x360,0x440,0x444,0x358,0x430,0x434,1u<<7},
    {0x7000d000,0x128,0x014,0x328,0x32c,0x008,0x308,0x30c,1u<<15},
    {0x7000d100,0x65c,0x280,0x284,0x288,0x28c,0x290,0x294,1u<<6},
};
static const ULONG TegraI2cContextRegisters[] = {
    I2C_CNFG, I2C_CLK_DIVISOR, I2C_SL_CNFG, I2C_TLOW_SEXT,
    I2C_CLKEN_OVERRIDE, I2C_INTERFACE_TIMING, I2C_HS_TIMING, I2C_FIFO_CONTROL
};

typedef struct _TEGRA_I2C_DEVICE {
    volatile UCHAR *Base;
    ULONG Length;
    volatile UCHAR *Car;
    const TEGRA_I2C_CLOCK *Clock;
    ULONG SavedSource, Saved[RTL_NUMBER_OF(TegraI2cContextRegisters)];
    BOOLEAN ContextValid, ResumePending;
    PKEVENT SdGate;
    HANDLE SdGateHandle;
    ULONG PhysLow;              /* low 32 bits of the MMIO base, for logging */
    volatile LONG Errors, Nacks, Timeouts, LastError, LastAddress;
} TEGRA_I2C_DEVICE, *PTEGRA_I2C_DEVICE;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(TEGRA_I2C_DEVICE, TegraI2cGetDevice)

typedef struct _TEGRA_I2C_TARGET {
    USHORT Address;
    ULONG Speed;
} TEGRA_I2C_TARGET, *PTEGRA_I2C_TARGET;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(TEGRA_I2C_TARGET, TegraI2cGetTarget)

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD TegraI2cEvtDeviceAdd;
EVT_WDF_DEVICE_PREPARE_HARDWARE TegraI2cEvtPrepareHardware;
EVT_WDF_DEVICE_RELEASE_HARDWARE TegraI2cEvtReleaseHardware;
EVT_WDF_DEVICE_D0_ENTRY TegraI2cEvtD0Entry;
EVT_WDF_DEVICE_D0_EXIT TegraI2cEvtD0Exit;
EVT_SPB_TARGET_CONNECT TegraI2cEvtTargetConnect;
EVT_SPB_CONTROLLER_READ TegraI2cEvtRead;
EVT_SPB_CONTROLLER_WRITE TegraI2cEvtWrite;
EVT_SPB_CONTROLLER_SEQUENCE TegraI2cEvtSequence;

/* ------------------------------------------------------------ diagnostics */

static WCHAR TegraI2cParamsPath[256];

/* DWORD "I2C_<base>_<Name>" under Services\tegra210i2c\Parameters (PASSIVE only). */
static VOID TegraI2cTrace(PTEGRA_I2C_DEVICE Dev, PCWSTR Name, ULONG Value)
{
    WCHAR Full[64];
    UNICODE_STRING Str;
    if (TegraI2cParamsPath[0] == L'\0' || KeGetCurrentIrql() != PASSIVE_LEVEL) {
        return;
    }
    RtlInitEmptyUnicodeString(&Str, Full, sizeof(Full));
    if (NT_SUCCESS(RtlUnicodeStringPrintf(&Str, L"I2C_%04X_%s", (Dev != NULL) ? (Dev->PhysLow & 0xFFFF) : 0, Name))) {
        Full[Str.Length / sizeof(WCHAR)] = L'\0';
        (VOID)RtlWriteRegistryValue(RTL_REGISTRY_ABSOLUTE, TegraI2cParamsPath, Full, REG_DWORD, &Value, sizeof(Value));
    }
}

/* Error counters, written when an error happens (errors are rare; PASSIVE only). */
static VOID TegraI2cTraceErrors(PTEGRA_I2C_DEVICE Dev)
{
    TegraI2cTrace(Dev, L"Errors", (ULONG)Dev->Errors);
    TegraI2cTrace(Dev, L"Nacks", (ULONG)Dev->Nacks);
    TegraI2cTrace(Dev, L"Timeouts", (ULONG)Dev->Timeouts);
    TegraI2cTrace(Dev, L"LastErrorStatus", (ULONG)Dev->LastError);
    TegraI2cTrace(Dev, L"LastErrorAddr", (ULONG)Dev->LastAddress);
}

/* -------------------------------------------------------------- hardware */

static ULONG RdReg(PTEGRA_I2C_DEVICE Dev, ULONG Off)
{
    return READ_REGISTER_ULONG((PULONG)(Dev->Base + Off));
}

static VOID WrReg(PTEGRA_I2C_DEVICE Dev, ULONG Off, ULONG Value)
{
    WRITE_REGISTER_ULONG((PULONG)(Dev->Base + Off), Value);
}

static BOOLEAN TegraI2cWaitClear(PTEGRA_I2C_DEVICE Dev, ULONG Off, ULONG Mask, ULONG Microseconds)
{
    ULONG Waited;
    for (Waited = 0; Waited < Microseconds; Waited += 5) {
        if ((RdReg(Dev, Off) & Mask) == 0) {
            return TRUE;
        }
        KeStallExecutionProcessor(5);
    }
    return FALSE;
}

static ULONG TegraI2cReadClock(PTEGRA_I2C_DEVICE Dev, ULONG Offset)
{
    return READ_REGISTER_ULONG((PULONG)(Dev->Car + Offset));
}
static VOID TegraI2cWriteClock(PTEGRA_I2C_DEVICE Dev, ULONG Offset, ULONG Value)
{
    WRITE_REGISTER_ULONG((PULONG)(Dev->Car + Offset), Value);
}

/* Caller owns the I2C5/SD gate, and SpbCx has quiesced this controller. */
static NTSTATUS TegraI2cSaveContext(PTEGRA_I2C_DEVICE Dev)
{
    Dev->ContextValid = FALSE;
    if (!Dev->Clock || !Dev->Car || !Dev->Base) return STATUS_DEVICE_NOT_READY;
    if (!(TegraI2cReadClock(Dev, Dev->Clock->Enable) & Dev->Clock->Mask) ||
        (TegraI2cReadClock(Dev, Dev->Clock->Reset) & Dev->Clock->Mask))
        return STATUS_DEVICE_NOT_READY;
    if (!TegraI2cWaitClear(Dev, I2C_STATUS, 0x100, 20000)) return STATUS_DEVICE_BUSY;
    Dev->SavedSource = TegraI2cReadClock(Dev, Dev->Clock->Source);
    for (ULONG i = 0; i < RTL_NUMBER_OF(TegraI2cContextRegisters); ++i)
        Dev->Saved[i] = RdReg(Dev, TegraI2cContextRegisters[i]);
    Dev->ContextValid = TRUE;
    return STATUS_SUCCESS;
}

static NTSTATUS TegraI2cRestoreContext(PTEGRA_I2C_DEVICE Dev)
{
    const TEGRA_I2C_CLOCK *Clock = Dev->Clock;
    if (!Dev->ContextValid || !Clock || !Dev->Car || !Dev->Base)
        return STATUS_DEVICE_NOT_READY;
    if (Dev->ResumePending) {
        /* Switchroot enables the bus clock and resets the controller before
         * reinitializing packet mode. This preserves the inherited source;
         * platform/firmware restoration owns its parent PLL and oscillator. */
        TegraI2cWriteClock(Dev, Clock->ResetSet, Clock->Mask);
        TegraI2cWriteClock(Dev, Clock->EnableClear, Clock->Mask);
        TegraI2cWriteClock(Dev, Clock->Source, Dev->SavedSource);
        TegraI2cWriteClock(Dev, Clock->EnableSet, Clock->Mask);
        KeStallExecutionProcessor(2);
        TegraI2cWriteClock(Dev, Clock->ResetClear, Clock->Mask);
        KeStallExecutionProcessor(2);
    }
    if (!(TegraI2cReadClock(Dev, Clock->Enable) & Clock->Mask) ||
        (TegraI2cReadClock(Dev, Clock->Reset) & Clock->Mask) ||
        TegraI2cReadClock(Dev, Clock->Source) != Dev->SavedSource)
        return STATUS_DEVICE_POWER_FAILURE;

    /* Never replay FIFO payloads, command addresses or a pending SEND bit.
     * Only stable timing/configuration is restored; this driver polls IRQs. */
    WrReg(Dev, I2C_INT_MASK, 0);
    for (ULONG i = 0; i < RTL_NUMBER_OF(TegraI2cContextRegisters); ++i) {
        ULONG Register = TegraI2cContextRegisters[i], Value = Dev->Saved[i];
        if (Register == I2C_CNFG) Value = (Value & ~CNFG_SEND) | CNFG_PACKET_MODE;
        if (Register == I2C_FIFO_CONTROL) Value &= ~FIFO_FLUSH_TX_RX;
        WrReg(Dev, Register, Value);
    }
    WrReg(Dev, I2C_CONFIG_LOAD, CONFIG_LOAD_ALL);
    if (!TegraI2cWaitClear(Dev, I2C_CONFIG_LOAD, 1, 10000)) return STATUS_IO_TIMEOUT;
    WrReg(Dev, I2C_INT_STATUS, 0xFFFFFFFF);
    WrReg(Dev, I2C_FIFO_CONTROL, RdReg(Dev, I2C_FIFO_CONTROL) | FIFO_FLUSH_TX_RX);
    if (!TegraI2cWaitClear(Dev, I2C_FIFO_CONTROL, FIFO_FLUSH_TX_RX, 10000)) return STATUS_IO_TIMEOUT;
    Dev->ResumePending = FALSE;
    return STATUS_SUCCESS;
}

static VOID TegraI2cLoadConfig(PTEGRA_I2C_DEVICE Dev)
{
    WrReg(Dev, I2C_CONFIG_LOAD, CONFIG_LOAD_ALL);
    (VOID)TegraI2cWaitClear(Dev, I2C_CONFIG_LOAD, 1, 10000);
}

static VOID TegraI2cResetFifos(PTEGRA_I2C_DEVICE Dev)
{
    WrReg(Dev, I2C_INT_STATUS, 0xFFFFFFFF);
    WrReg(Dev, I2C_FIFO_CONTROL, RdReg(Dev, I2C_FIFO_CONTROL) | FIFO_FLUSH_TX_RX);
    (VOID)TegraI2cWaitClear(Dev, I2C_FIFO_CONTROL, FIFO_FLUSH_TX_RX, 10000);
}

/* After an arbitration loss or timeout: 9 clock pulses to free a stuck slave. */
static VOID TegraI2cBusClear(PTEGRA_I2C_DEVICE Dev)
{
    ULONG Waited;
    WrReg(Dev, I2C_BUS_CLEAR_CONFIG, (9u << 16) | (1u << 2) | 1u);  /* threshold 9, STOP, enable */
    TegraI2cLoadConfig(Dev);
    for (Waited = 0; Waited < 10000; Waited += 5) {
        if ((RdReg(Dev, I2C_INT_STATUS) & INT_BUS_CLEAR_DONE) != 0) {
            break;
        }
        KeStallExecutionProcessor(5);
    }
    WrReg(Dev, I2C_INT_STATUS, 0xFFFFFFFF);
}

/* One I2C message as one packet. Buffer is a kernel buffer of Length bytes. */
static NTSTATUS TegraI2cTransfer(PTEGRA_I2C_DEVICE Dev, USHORT Address, BOOLEAN Read, PUCHAR Buffer,
                                 ULONG Length, BOOLEAN RepeatStart)
{
    ULONG Done = 0, Waited = 0, Timeout = 20000 + Length * 200, Status, Word, Index;

    if (Length == 0 || Length > I2C_MAX_TRANSFER) {
        return STATUS_INVALID_PARAMETER;
    }
    TegraI2cResetFifos(Dev);

    WrReg(Dev, I2C_TX_FIFO, HDR0_I2C_PACKET1);
    WrReg(Dev, I2C_TX_FIFO, Length - 1);
    WrReg(Dev, I2C_TX_FIFO, ((ULONG)(Address & 0x7F) << 1) | (Read ? HDR_READ : 0) | (RepeatStart ? HDR_REPEAT_START : 0));

    while (Done < Length) {
        Status = RdReg(Dev, I2C_INT_STATUS);
        if ((Status & INT_ERR_MASK) != 0) {
            goto Error;
        }
        if (Read) {
            ULONG Full = RdReg(Dev, I2C_FIFO_STATUS) & 0x0F;          /* RX words available */
            if (Full != 0) {
                Word = RdReg(Dev, I2C_RX_FIFO);
                for (Index = 0; Index < 4 && Done < Length; ++Index) {
                    Buffer[Done++] = (UCHAR)(Word >> (8 * Index));
                }
                continue;
            }
        } else {
            ULONG Empty = (RdReg(Dev, I2C_FIFO_STATUS) >> 4) & 0x0F;  /* TX words free */
            if (Empty != 0) {
                Word = 0;
                for (Index = 0; Index < 4 && Done < Length; ++Index) {
                    Word |= (ULONG)Buffer[Done++] << (8 * Index);
                }
                WrReg(Dev, I2C_TX_FIFO, Word);
                continue;
            }
        }
        if (Waited >= Timeout) {
            goto Timeout;
        }
        KeStallExecutionProcessor(5);
        Waited += 5;
    }

    for (;;) {
        Status = RdReg(Dev, I2C_INT_STATUS);
        if ((Status & INT_ERR_MASK) != 0) {
            goto Error;
        }
        if ((Status & (INT_PACKET_DONE | INT_ALL_PACKETS)) != 0) {
            WrReg(Dev, I2C_INT_STATUS, Status);
            return STATUS_SUCCESS;
        }
        if (Waited >= Timeout) {
            goto Timeout;
        }
        KeStallExecutionProcessor(5);
        Waited += 5;
    }

Error:
    InterlockedIncrement(&Dev->Errors);
    InterlockedExchange(&Dev->LastError, (LONG)Status);
    InterlockedExchange(&Dev->LastAddress, Address);
    if ((Status & INT_NO_ACK) != 0) {
        InterlockedIncrement(&Dev->Nacks);
        TegraI2cResetFifos(Dev);
        return STATUS_NO_SUCH_DEVICE;
    }
    TegraI2cResetFifos(Dev);
    TegraI2cBusClear(Dev);
    return STATUS_IO_DEVICE_ERROR;

Timeout:
    InterlockedIncrement(&Dev->Timeouts);
    InterlockedExchange(&Dev->LastAddress, Address);
    TegraI2cResetFifos(Dev);
    TegraI2cBusClear(Dev);
    return STATUS_IO_TIMEOUT;
}

/* Copy between an MDL chain and a flat buffer. */
static NTSTATUS TegraI2cMdlCopy(PMDL Mdl, PUCHAR Flat, SIZE_T Length, BOOLEAN ToFlat)
{
    SIZE_T Offset = 0;
    while (Mdl != NULL && Offset < Length) {
        PUCHAR Va = (PUCHAR)MmGetSystemAddressForMdlSafe(Mdl, NormalPagePriority | MdlMappingNoExecute);
        SIZE_T Chunk = MmGetMdlByteCount(Mdl);
        if (Va == NULL) {
            return STATUS_INSUFFICIENT_RESOURCES;
        }
        if (Chunk > Length - Offset) {
            Chunk = Length - Offset;
        }
        if (ToFlat) {
            RtlCopyMemory(Flat + Offset, Va, Chunk);
        } else {
            RtlCopyMemory(Va, Flat + Offset, Chunk);
        }
        Offset += Chunk;
        Mdl = Mdl->Next;
    }
    return (Offset == Length) ? STATUS_SUCCESS : STATUS_BUFFER_TOO_SMALL;
}

/* Run SPB transfers [0, Count) of Request; repeated start between them. */
static VOID TegraI2cRunRequest(WDFDEVICE Controller, SPBTARGET Target, SPBREQUEST Request, ULONG Count)
{
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Controller);
    PTEGRA_I2C_TARGET Tgt = TegraI2cGetTarget(Target);
    NTSTATUS Status = STATUS_SUCCESS;
    SIZE_T Total = 0;
    ULONG Index;
    Status = TegraI2c5Acquire(Dev->SdGate);
    if (!NT_SUCCESS(Status)) {
        SpbRequestComplete(Request, Status);
        return;
    }
    for (Index = 0; Index < Count && NT_SUCCESS(Status); ++Index) {
        SPB_TRANSFER_DESCRIPTOR Desc;
        PMDL Mdl = NULL;
        PUCHAR Bounce;
        BOOLEAN Read;

        SPB_TRANSFER_DESCRIPTOR_INIT(&Desc);
        SpbRequestGetTransferParameters(Request, Index, &Desc, &Mdl);
        Read = (Desc.Direction == SpbTransferDirectionFromDevice);
        if (Desc.TransferLength == 0 || Desc.TransferLength > I2C_MAX_TRANSFER || Mdl == NULL) {
            Status = STATUS_INVALID_PARAMETER;
            break;
        }
        if (Desc.DelayInUs != 0) {
            KeStallExecutionProcessor(Desc.DelayInUs > 1000 ? 1000 : Desc.DelayInUs);
        }
        Bounce = (PUCHAR)ExAllocatePool2(POOL_FLAG_NON_PAGED, Desc.TransferLength, 'c2iT');
        if (Bounce == NULL) {
            Status = STATUS_INSUFFICIENT_RESOURCES;
            break;
        }
        if (!Read) {
            Status = TegraI2cMdlCopy(Mdl, Bounce, Desc.TransferLength, TRUE);
        }
        if (NT_SUCCESS(Status)) {
            Status = TegraI2cTransfer(Dev, Tgt->Address, Read, Bounce, (ULONG)Desc.TransferLength, (BOOLEAN)(Index + 1 < Count));
        }
        if (NT_SUCCESS(Status) && Read) {
            Status = TegraI2cMdlCopy(Mdl, Bounce, Desc.TransferLength, FALSE);
        }
        if (NT_SUCCESS(Status)) {
            Total += Desc.TransferLength;
        }
        ExFreePoolWithTag(Bounce, 'c2iT');
    }
    TegraI2c5Release(Dev->SdGate);
    /* Registry writes may need SD I/O; do not hold its PMIC gate here. */
    if (!NT_SUCCESS(Status)) TegraI2cTraceErrors(Dev);
    WdfRequestSetInformation((WDFREQUEST)Request, Total);
    SpbRequestComplete(Request, Status);
}

/* -------------------------------------------------------------------- SPB */

NTSTATUS TegraI2cEvtTargetConnect(WDFDEVICE Controller, SPBTARGET Target)
{
    SPB_CONNECTION_PARAMETERS Params;
    PRH_QUERY_CONNECTION_PROPERTIES_OUTPUT_BUFFER Conn;
    PPNP_I2C_SERIAL_BUS_DESCRIPTOR I2c;
    PTEGRA_I2C_TARGET Tgt = TegraI2cGetTarget(Target);
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Controller);

    SPB_CONNECTION_PARAMETERS_INIT(&Params);
    SpbTargetGetConnectionParameters(Target, &Params);
    Conn = (PRH_QUERY_CONNECTION_PROPERTIES_OUTPUT_BUFFER)Params.ConnectionParameters;
    if (Conn == NULL) {
        TegraI2cTrace(Dev, L"ConnectNoParams", 1);
        return STATUS_INVALID_PARAMETER;
    }
    {
        /* PropertiesLength reads 1 on this Windows build, so the buffer layout
         * is not the documented length + descriptor: find the ACPI
         * GenericSerialBus descriptor (large item tag 0x8E, SerialBusType 1 =
         * I2C) in the first bytes instead of trusting the length field. */
        PUCHAR Raw = (PUCHAR)Conn;
        ULONG Offset;
        I2c = NULL;
        for (Offset = 0; Offset <= 16; ++Offset) {
            PPNP_I2C_SERIAL_BUS_DESCRIPTOR Candidate = (PPNP_I2C_SERIAL_BUS_DESCRIPTOR)(Raw + Offset);
            if (Candidate->SerialBusDescriptor.Tag == 0x8E &&
                Candidate->SerialBusDescriptor.SerialBusType == I2C_SERIAL_BUS_TYPE &&
                Candidate->SerialBusDescriptor.Length >= sizeof(PNP_I2C_SERIAL_BUS_DESCRIPTOR) - 3) {
                I2c = Candidate;
                break;
            }
        }
        if (I2c == NULL) {
            TegraI2cTrace(Dev, L"ConnectDescriptorOffset", 0xFFFF);
            return STATUS_INVALID_PARAMETER;
        }
    }
    if ((I2c->SerialBusDescriptor.TypeSpecificFlags & I2C_10BIT_ADDRESS_FLAG) != 0 || I2c->SlaveAddress > 0x7F) {
        return STATUS_NOT_SUPPORTED;
    }
    Tgt->Address = I2c->SlaveAddress;
    Tgt->Speed = I2c->ConnectionSpeed;
    return STATUS_SUCCESS;
}

VOID TegraI2cEvtRead(WDFDEVICE Controller, SPBTARGET Target, SPBREQUEST Request, size_t Length)
{
    UNREFERENCED_PARAMETER(Length);
    TegraI2cRunRequest(Controller, Target, Request, 1);
}

VOID TegraI2cEvtWrite(WDFDEVICE Controller, SPBTARGET Target, SPBREQUEST Request, size_t Length)
{
    UNREFERENCED_PARAMETER(Length);
    TegraI2cRunRequest(Controller, Target, Request, 1);
}

VOID TegraI2cEvtSequence(WDFDEVICE Controller, SPBTARGET Target, SPBREQUEST Request, ULONG TransferCount)
{
    TegraI2cRunRequest(Controller, Target, Request, TransferCount);
}

/* ------------------------------------------------------------------- PnP */

NTSTATUS TegraI2cEvtPrepareHardware(WDFDEVICE Device, WDFCMRESLIST Raw, WDFCMRESLIST Translated)
{
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Device);
    ULONG Count = WdfCmResourceListGetCount(Translated), Index;
    UNREFERENCED_PARAMETER(Raw);

    for (Index = 0; Index < Count; ++Index) {
        PCM_PARTIAL_RESOURCE_DESCRIPTOR Desc = WdfCmResourceListGetDescriptor(Translated, Index);
        if (Desc != NULL && Desc->Type == CmResourceTypeMemory && Dev->Base == NULL) {
            if (Desc->u.Memory.Length < I2C_MMIO_SIZE) {
                return STATUS_DEVICE_CONFIGURATION_ERROR;
            }
            Dev->Base = (volatile UCHAR *)MmMapIoSpaceEx(Desc->u.Memory.Start, Desc->u.Memory.Length,
                                                         PAGE_READWRITE | PAGE_NOCACHE);
            if (Dev->Base == NULL) {
                return STATUS_INSUFFICIENT_RESOURCES;
            }
            Dev->Length = Desc->u.Memory.Length;
            Dev->PhysLow = Desc->u.Memory.Start.LowPart;
        }
    }
    if (Dev->Base == NULL) {
        return STATUS_DEVICE_CONFIGURATION_ERROR;
    }
    for (Index = 0; Index < RTL_NUMBER_OF(TegraI2cClocks); ++Index)
        if (TegraI2cClocks[Index].Base == Dev->PhysLow) Dev->Clock = &TegraI2cClocks[Index];
    if (Dev->Clock == NULL) return STATUS_DEVICE_CONFIGURATION_ERROR;
    PHYSICAL_ADDRESS CarAddress;
    CarAddress.QuadPart = 0x60006000;
    Dev->Car = MmMapIoSpaceEx(CarAddress, 0x1000, PAGE_READWRITE | PAGE_NOCACHE);
    if (Dev->Car == NULL) return STATUS_INSUFFICIENT_RESOURCES;
    if (Dev->PhysLow == 0x7000d000 && Dev->SdGate == NULL) {
        Dev->SdGate = TegraI2c5OpenGate(&Dev->SdGateHandle);
        if (Dev->SdGate == NULL) return STATUS_INSUFFICIENT_RESOURCES;
    }
    NTSTATUS GateStatus = TegraI2c5Acquire(Dev->SdGate);
    if (!NT_SUCCESS(GateStatus)) return GateStatus;
    NTSTATUS Status = TegraI2cSaveContext(Dev);
    Dev->ResumePending = FALSE;
    TegraI2c5Release(Dev->SdGate);
    TegraI2cTrace(Dev, L"ResourceCount", Count);
    return Status;
}

NTSTATUS TegraI2cEvtReleaseHardware(WDFDEVICE Device, WDFCMRESLIST Translated)
{
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Device);
    UNREFERENCED_PARAMETER(Translated);
    Dev->ContextValid = Dev->ResumePending = FALSE;
    Dev->Clock = NULL;
    if (Dev->SdGateHandle != NULL) {
        ZwClose(Dev->SdGateHandle); Dev->SdGateHandle = NULL; Dev->SdGate = NULL;
    }
    if (Dev->Base != NULL) {
        MmUnmapIoSpace((PVOID)Dev->Base, Dev->Length);
        Dev->Base = NULL;
    }
    if (Dev->Car != NULL) {
        MmUnmapIoSpace((PVOID)Dev->Car, 0x1000);
        Dev->Car = NULL;
    }
    return STATUS_SUCCESS;
}

NTSTATUS TegraI2cEvtD0Entry(WDFDEVICE Device, WDF_POWER_DEVICE_STATE PreviousState)
{
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Device);
    UNREFERENCED_PARAMETER(PreviousState);
    NTSTATUS Status = TegraI2c5Acquire(Dev->SdGate);
    if (!NT_SUCCESS(Status)) return Status;
    Status = TegraI2cRestoreContext(Dev);
    TegraI2c5Release(Dev->SdGate);
    return Status;
}

NTSTATUS TegraI2cEvtD0Exit(WDFDEVICE Device, WDF_POWER_DEVICE_STATE TargetState)
{
    PTEGRA_I2C_DEVICE Dev = TegraI2cGetDevice(Device);
    if (TargetState == WdfPowerDeviceD3Final) {
        Dev->ContextValid = Dev->ResumePending = FALSE;
        return STATUS_SUCCESS;
    }
    NTSTATUS Status = TegraI2c5Acquire(Dev->SdGate);
    if (!NT_SUCCESS(Status)) return Status;
    Status = TegraI2cSaveContext(Dev);
    Dev->ResumePending = NT_SUCCESS(Status);
    TegraI2c5Release(Dev->SdGate);
    return Status;
}

NTSTATUS TegraI2cEvtDeviceAdd(WDFDRIVER Driver, PWDFDEVICE_INIT DeviceInit)
{
    WDF_PNPPOWER_EVENT_CALLBACKS Pnp;
    WDF_OBJECT_ATTRIBUTES Attributes;
    SPB_CONTROLLER_CONFIG SpbConfig;
    WDFDEVICE Device;
    NTSTATUS Status;
    UNREFERENCED_PARAMETER(Driver);

    Status = SpbDeviceInitConfig(DeviceInit);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }
    WDF_PNPPOWER_EVENT_CALLBACKS_INIT(&Pnp);
    Pnp.EvtDevicePrepareHardware = TegraI2cEvtPrepareHardware;
    Pnp.EvtDeviceReleaseHardware = TegraI2cEvtReleaseHardware;
    Pnp.EvtDeviceD0Entry = TegraI2cEvtD0Entry;
    Pnp.EvtDeviceD0Exit = TegraI2cEvtD0Exit;
    WdfDeviceInitSetPnpPowerEventCallbacks(DeviceInit, &Pnp);

    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&Attributes, TEGRA_I2C_DEVICE);
    Status = WdfDeviceCreate(&DeviceInit, &Attributes, &Device);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }

    SPB_CONTROLLER_CONFIG_INIT(&SpbConfig);
    SpbConfig.PowerManaged = WdfTrue;
    SpbConfig.EvtSpbTargetConnect = TegraI2cEvtTargetConnect;
    SpbConfig.EvtSpbIoRead = TegraI2cEvtRead;
    SpbConfig.EvtSpbIoWrite = TegraI2cEvtWrite;
    SpbConfig.EvtSpbIoSequence = TegraI2cEvtSequence;
    Status = SpbDeviceInitialize(Device, &SpbConfig);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }

    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&Attributes, TEGRA_I2C_TARGET);
    SpbControllerSetTargetAttributes(Device, &Attributes);
    return STATUS_SUCCESS;
}

NTSTATUS DriverEntry(PDRIVER_OBJECT DriverObject, PUNICODE_STRING RegistryPath)
{
    WDF_DRIVER_CONFIG Config;

    if (RegistryPath->Length / sizeof(WCHAR) + 12 < RTL_NUMBER_OF(TegraI2cParamsPath)) {
        RtlCopyMemory(TegraI2cParamsPath, RegistryPath->Buffer, RegistryPath->Length);
        RtlCopyMemory(&TegraI2cParamsPath[RegistryPath->Length / sizeof(WCHAR)], L"\\Parameters", sizeof(L"\\Parameters"));
        (VOID)RtlCreateRegistryKey(RTL_REGISTRY_ABSOLUTE, TegraI2cParamsPath);
    }
    TegraI2cTrace(NULL, L"DriverVersion", 6);

    WDF_DRIVER_CONFIG_INIT(&Config, TegraI2cEvtDeviceAdd);
    return WdfDriverCreate(DriverObject, RegistryPath, WDF_NO_OBJECT_ATTRIBUTES, &Config, WDF_NO_HANDLE);
}
