/*
 * Nintendo Switch power button driver, KMDF + Virtual HID Framework (VHF).
 *
 * ACPI device PWRB (ACPI\SWOA0601):
 *   I2cSerialBus 0x3C on I2C5: MAX77620 PMIC. The power button drives its
 *   EN0 input; there is no GPIO for it on the SoC.
 * Every 50 ms the polling fallback reads (Hekate bdk/power/max77620.h):
 *   ONOFFSTAT 0x15 bit 2 (EN0): 1 while the button is held.
 *   ONOFFIRQ  0x0B (clear on read): bit 3 EN0 rising, bit 2 EN0 falling,
 *     so a press shorter than one poll is still seen.
 * A press with the display on reports HID System Power Down, so Windows
 * applies Power Options. With the display off it reports System Wake Up.
 * When firmware exposes PMC_CNTRL (0x7000E400) and GIC SPI 86 / GSI 118
 * (PMU_EXT), the passive-IRQ profile masks release edges, arms press-only
 * wake routing across S0-idle D3 and SC7, and emits complete HID pulses
 * without keeping I2C5 awake with a 50 ms polling timer.
 * Holding the button for several seconds still powers the PMIC off by itself.
 */
#include <ntddk.h>
#include <ntstrsafe.h>
#include <wdf.h>
#include <spb.h>
#include <vhf.h>
#include "button_state.h"

#define MAX77620_ONOFFIRQ    0x0B
#define MAX77620_IRQTOPM     0x0D
#define MAX77620_INTENLBT    0x0E
#define MAX77620_ONOFFIRQM   0x12
#define MAX77620_ONOFFSTAT   0x15
#define ONOFFSTAT_EN0        0x04
#define ONOFFIRQ_EN0_F       0x04
#define ONOFFIRQ_EN0_R       0x08
#define PMC_INTR_POLARITY    (1u << 17)
#define POLL_MS              50
#define REPORT_ID_SYSTEM     1

/* System Control: Power Down (0x81), Wake Up (0x83), six padding bits. */
static UCHAR SwPwrReportDescriptor[] = {
    0x05, 0x01, 0x09, 0x80, 0xA1, 0x01,
    0x85, REPORT_ID_SYSTEM,
    0x09, 0x81, 0x09, 0x83, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x02, 0x81, 0x02,
    0x95, 0x06, 0x81, 0x01,
    0xC0
};

typedef struct _SWPWR_DEVICE {
    LARGE_INTEGER I2cId;
    PULONG Pmc;
    WDFIOTARGET I2c;
    WDFTIMER Timer;
    WDFINTERRUPT Interrupt;
    BOOLEAN IrqConfigured;
    UCHAR OriginalTopMask, OriginalGlobalMask, OriginalOnOffMask;
    VHFHANDLE Vhf;
    volatile BOOLEAN Stopping;
    struct swpwr_button_state Button;
    KSPIN_LOCK TimerLock;
    PVOID DisplayCallback;
    volatile LONG DisplayOff;
    UCHAR Report[2];
    ULONG Failures;
    LONG Polls;
    LONG Presses;
    LONG Reports;
    LONG WakeReports, PowerReports;
    volatile LONG LastReportStatus, LastIrqStatus, LastOnOffIrq;
} SWPWR_DEVICE, *PSWPWR_DEVICE;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(SWPWR_DEVICE, SwPwrGetDevice)

typedef struct _SWPWR_TIMER {
    WDFDEVICE Device;
} SWPWR_TIMER, *PSWPWR_TIMER;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(SWPWR_TIMER, SwPwrGetTimer)

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD SwPwrEvtDeviceAdd;
EVT_WDF_DEVICE_PREPARE_HARDWARE SwPwrEvtPrepareHardware;
EVT_WDF_DEVICE_RELEASE_HARDWARE SwPwrEvtReleaseHardware;
EVT_WDF_DEVICE_D0_ENTRY SwPwrEvtD0Entry;
EVT_WDF_DEVICE_D0_EXIT SwPwrEvtD0Exit;
EVT_WDF_DEVICE_SELF_MANAGED_IO_INIT SwPwrEvtSelfManagedIoInit;
EVT_WDF_DEVICE_SELF_MANAGED_IO_CLEANUP SwPwrEvtSelfManagedIoCleanup;
EVT_WDF_DEVICE_SELF_MANAGED_IO_SUSPEND SwPwrEvtSuspend;
EVT_WDF_DEVICE_SELF_MANAGED_IO_RESTART SwPwrEvtRestart;
EVT_WDF_TIMER SwPwrEvtTimer;
EVT_WDF_INTERRUPT_ISR SwPwrEvtIsr;
EVT_WDF_INTERRUPT_WORKITEM SwPwrEvtInterruptWorkItem;

static WCHAR SwPwrParamsPath[256];
static const GUID SwPwrDisplayGuid =
    {0x6fe69556,0x704a,0x47a0,{0x8f,0x24,0xc2,0x8d,0x93,0x6f,0xda,0x47}};

static NTSTATUS SwPwrDisplayChanged(LPCGUID Setting, PVOID Value, ULONG Length, PVOID Context)
{
    PSWPWR_DEVICE Dev = Context;
    if (IsEqualGUID(Setting, &SwPwrDisplayGuid) && Length == sizeof(ULONG) && *(PULONG)Value <= 2)
        InterlockedExchange(&Dev->DisplayOff, *(PULONG)Value == 0);
    return STATUS_SUCCESS;
}

static UCHAR SwPwrPressReport(PSWPWR_DEVICE Dev)
{
    return InterlockedCompareExchange(&Dev->DisplayOff, 0, 0) ? SWPWR_WAKE : SWPWR_POWER;
}

static VOID SwPwrTrace(PCWSTR Name, ULONG Value)
{
    if (SwPwrParamsPath[0] != L'\0' && KeGetCurrentIrql() == PASSIVE_LEVEL) {
        (VOID)RtlWriteRegistryValue(RTL_REGISTRY_ABSOLUTE, SwPwrParamsPath, Name, REG_DWORD, &Value, sizeof(Value));
    }
}

/* ------------------------------------------------------------------- I2C */

static NTSTATUS SwPwrOpenTarget(WDFDEVICE Device, LARGE_INTEGER Id, WDFIOTARGET *Target)
{
    WCHAR PathBuffer[64];
    UNICODE_STRING Path;
    WDF_IO_TARGET_OPEN_PARAMS Open;
    NTSTATUS Status;

    RtlInitEmptyUnicodeString(&Path, PathBuffer, sizeof(PathBuffer));
    Status = RtlUnicodeStringPrintf(&Path, L"\\Device\\RESOURCE_HUB\\%016I64x", (ULONG64)Id.QuadPart);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }
    Status = WdfIoTargetCreate(Device, WDF_NO_OBJECT_ATTRIBUTES, Target);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }
    WDF_IO_TARGET_OPEN_PARAMS_INIT_OPEN_BY_NAME(&Open, &Path, GENERIC_READ | GENERIC_WRITE);
    Status = WdfIoTargetOpen(*Target, &Open);
    if (!NT_SUCCESS(Status)) {
        WdfObjectDelete(*Target);
        *Target = NULL;
    }
    return Status;
}

static NTSTATUS SwPwrReadReg(WDFIOTARGET Target, UCHAR Register, PUCHAR Value)
{
    SPB_TRANSFER_LIST_AND_ENTRIES(2) Sequence;
    PSPB_TRANSFER_LIST_ENTRY Entries = Sequence.List.Transfers;
    WDF_MEMORY_DESCRIPTOR Input;
    WDF_REQUEST_SEND_OPTIONS Options;
    ULONG_PTR Bytes = 0;
    NTSTATUS Status;

    if (Target == NULL) {
        return STATUS_DEVICE_NOT_READY;
    }
    SPB_TRANSFER_LIST_INIT(&Sequence.List, 2);
    Entries[0] = SPB_TRANSFER_LIST_ENTRY_INIT_SIMPLE(SpbTransferDirectionToDevice, 0, &Register, 1);
    Entries[1] = SPB_TRANSFER_LIST_ENTRY_INIT_SIMPLE(SpbTransferDirectionFromDevice, 0, Value, 1);
    WDF_MEMORY_DESCRIPTOR_INIT_BUFFER(&Input, &Sequence, sizeof(Sequence));
    WDF_REQUEST_SEND_OPTIONS_INIT(&Options, WDF_REQUEST_SEND_OPTION_TIMEOUT);
    Options.Timeout = WDF_REL_TIMEOUT_IN_MS(200);
    Status = WdfIoTargetSendIoctlSynchronously(Target, NULL, IOCTL_SPB_EXECUTE_SEQUENCE, &Input, NULL, &Options, &Bytes);
    if (NT_SUCCESS(Status) && Bytes != 2) {
        Status = STATUS_DEVICE_PROTOCOL_ERROR;
    }
    return Status;
}

static NTSTATUS SwPwrWriteReg(WDFIOTARGET Target, UCHAR Register, UCHAR Value)
{
    UCHAR Data[2] = {Register, Value};
    WDF_MEMORY_DESCRIPTOR Memory;
    WDF_REQUEST_SEND_OPTIONS Options;
    ULONG_PTR Bytes = 0;
    NTSTATUS Status;

    if (Target == NULL) {
        return STATUS_DEVICE_NOT_READY;
    }
    WDF_MEMORY_DESCRIPTOR_INIT_BUFFER(&Memory, Data, sizeof(Data));
    WDF_REQUEST_SEND_OPTIONS_INIT(&Options, WDF_REQUEST_SEND_OPTION_TIMEOUT);
    Options.Timeout = WDF_REL_TIMEOUT_IN_MS(200);
    Status = WdfIoTargetSendWriteSynchronously(Target, NULL, &Memory, NULL, &Options, &Bytes);
    return (NT_SUCCESS(Status) && Bytes != sizeof(Data)) ? STATUS_DEVICE_PROTOCOL_ERROR : Status;
}

/* Only the ONOFF route is owned here. Keep unrelated PMIC interrupt masks. */
static NTSTATUS SwPwrSetIrq(PSWPWR_DEVICE Dev, BOOLEAN Enable)
{
    UCHAR Top = 0, Global = 0, OnOff = 0, Pending = 0;
    NTSTATUS Status = SwPwrReadReg(Dev->I2c, MAX77620_IRQTOPM, &Top);
    if (NT_SUCCESS(Status)) Status = SwPwrReadReg(Dev->I2c, MAX77620_INTENLBT, &Global);
    if (NT_SUCCESS(Status)) Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQM, &OnOff);
    if (!NT_SUCCESS(Status)) return Status;

    if (Enable) {
        if (!Dev->IrqConfigured) {
            Dev->OriginalTopMask = Top;
            Dev->OriginalGlobalMask = Global;
            Dev->OriginalOnOffMask = OnOff;
        }
        /* Mask the top route while acknowledging inherited edges. */
        Status = SwPwrWriteReg(Dev->I2c, MAX77620_IRQTOPM, (UCHAR)(Top | 0x02u));
        if (NT_SUCCESS(Status)) Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQ, &Pending);
        /* IRQ mode emits a complete HID pulse on press. Mask release so
         * releasing the key cannot immediately wake the just-suspended SoC. */
        if (NT_SUCCESS(Status)) Status = SwPwrWriteReg(Dev->I2c, MAX77620_ONOFFIRQM,
                                                       (UCHAR)((OnOff | ONOFFIRQ_EN0_F) & ~ONOFFIRQ_EN0_R));
        if (NT_SUCCESS(Status)) Status = SwPwrWriteReg(Dev->I2c, MAX77620_INTENLBT, (UCHAR)(Global & ~0x01u));
        if (NT_SUCCESS(Status)) Status = SwPwrWriteReg(Dev->I2c, MAX77620_IRQTOPM, (UCHAR)(Top & ~0x02u));
        if (!NT_SUCCESS(Status)) {
            (VOID)SwPwrWriteReg(Dev->I2c, MAX77620_IRQTOPM, Top);
            (VOID)SwPwrWriteReg(Dev->I2c, MAX77620_ONOFFIRQM, OnOff);
            (VOID)SwPwrWriteReg(Dev->I2c, MAX77620_INTENLBT, Global);
        }
        Dev->IrqConfigured = NT_SUCCESS(Status);
    } else if (Dev->IrqConfigured) {
        Status = SwPwrWriteReg(Dev->I2c, MAX77620_IRQTOPM,
                               (UCHAR)((Top & ~0x02u) | (Dev->OriginalTopMask & 0x02u)));
        if (NT_SUCCESS(Status)) Status = SwPwrWriteReg(Dev->I2c, MAX77620_ONOFFIRQM,
                                                       (UCHAR)((OnOff & ~0x0cu) | (Dev->OriginalOnOffMask & 0x0cu)));
        if (NT_SUCCESS(Status)) Status = SwPwrWriteReg(Dev->I2c, MAX77620_INTENLBT,
                                                       (UCHAR)((Global & ~0x01u) | (Dev->OriginalGlobalMask & 0x01u)));
        if (NT_SUCCESS(Status)) Dev->IrqConfigured = FALSE;
    }
    return Status;
}

NTSTATUS SwPwrEvtD0Entry(WDFDEVICE Device, WDF_POWER_DEVICE_STATE Previous)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    NTSTATUS Status = STATUS_SUCCESS;
    UNREFERENCED_PARAMETER(Previous);

    if (Dev->Pmc != NULL) {
        WRITE_REGISTER_ULONG(Dev->Pmc, READ_REGISTER_ULONG(Dev->Pmc) | PMC_INTR_POLARITY);
    }
    if (Dev->I2c == NULL) {
        Status = SwPwrOpenTarget(Device, Dev->I2cId, &Dev->I2c);
    }
    /* On resume preserve any latched wake event in ONOFFIRQ for the passive ISR. */
    if (NT_SUCCESS(Status) && Dev->Interrupt != NULL && !Dev->IrqConfigured) {
        Status = SwPwrSetIrq(Dev, TRUE);
    }
    return Status;
}

NTSTATUS SwPwrEvtD0Exit(WDFDEVICE Device, WDF_POWER_DEVICE_STATE Target)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    POWER_ACTION Action = WdfDeviceGetSystemPowerAction(Device);
    if (Dev->IrqConfigured &&
        (Target == WdfPowerDeviceD3Final ||
         Action == PowerActionShutdown ||
         Action == PowerActionShutdownReset ||
         Action == PowerActionShutdownOff)) {
        return SwPwrSetIrq(Dev, FALSE);
    }
    return STATUS_SUCCESS;
}

/* ---------------------------------------------------------------- button */

static VOID SwPwrFlushReportTraces(PSWPWR_DEVICE Dev)
{
    LONG LastErr = InterlockedCompareExchange(&Dev->LastReportStatus, 0, 0);
    SwPwrTrace(L"Reports", (ULONG)InterlockedCompareExchange(&Dev->Reports, 0, 0));
    SwPwrTrace(L"WakeReports", (ULONG)InterlockedCompareExchange(&Dev->WakeReports, 0, 0));
    SwPwrTrace(L"PowerReports", (ULONG)InterlockedCompareExchange(&Dev->PowerReports, 0, 0));
    if (!NT_SUCCESS((NTSTATUS)LastErr)) {
        SwPwrTrace(L"LastReportError", (ULONG)LastErr);
    }
}

static VOID SwPwrReport(PSWPWR_DEVICE Dev, UCHAR Buttons)
{
    HID_XFER_PACKET Packet;
    NTSTATUS Status;

    Dev->Report[0] = REPORT_ID_SYSTEM;
    Dev->Report[1] = Buttons;
    Packet.reportBuffer = Dev->Report;
    Packet.reportBufferLen = sizeof(Dev->Report);
    Packet.reportId = REPORT_ID_SYSTEM;
    Status = VhfReadReportSubmit(Dev->Vhf, &Packet);
    if (NT_SUCCESS(Status)) {
        InterlockedIncrement(&Dev->Reports);
        if (Buttons == SWPWR_WAKE) InterlockedIncrement(&Dev->WakeReports);
        if (Buttons == SWPWR_POWER) InterlockedIncrement(&Dev->PowerReports);
    }
    InterlockedExchange(&Dev->LastReportStatus, Status);
}

VOID SwPwrEvtTimer(WDFTIMER Timer)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(SwPwrGetTimer(Timer)->Device);
    UCHAR Irq = 0, Stat = 0;
    BOOLEAN Held;
    KIRQL Irql;
    NTSTATUS Status;

    if (Dev->Stopping) return;
    Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQ, &Irq);
    if (NT_SUCCESS(Status)) {
        Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFSTAT, &Stat);
    }
    if (NT_SUCCESS(Status)) {
        Dev->Failures = 0;
        Held = (Stat & ONOFFSTAT_EN0) != 0;
        struct swpwr_button_event Event = swpwr_button_update(
            &Dev->Button, Held, (Irq & ONOFFIRQ_EN0_R) != 0,
            InterlockedCompareExchange(&Dev->DisplayOff, 0, 0) != 0);
        if (Event.emit) {
            if (Event.buttons) SwPwrTrace(L"Presses", (ULONG)InterlockedIncrement(&Dev->Presses));
            SwPwrReport(Dev, Event.buttons);
            if (Event.pulse) SwPwrReport(Dev, 0);
            SwPwrFlushReportTraces(Dev);
        }
        if (Irq != 0) {
            SwPwrTrace(L"LastOnOffIrq", Irq);
        }
    } else if (++Dev->Failures <= 3 || (Dev->Failures % 100) == 0) {
        SwPwrTrace(L"LastI2cError", (ULONG)Status);
        SwPwrTrace(L"Failures", Dev->Failures);
    }
    if ((InterlockedIncrement(&Dev->Polls) % 200) == 0) {
        SwPwrTrace(L"Polls", (ULONG)Dev->Polls);
        SwPwrTrace(L"LastOnOffStat", Stat);
    }
    KeAcquireSpinLock(&Dev->TimerLock, &Irql);
    if (!Dev->Stopping) {
        WdfTimerStart(Timer, WDF_REL_TIMEOUT_IN_MS(POLL_MS));    /* one-shot passive timer, re-armed */
    }
    KeReleaseSpinLock(&Dev->TimerLock, Irql);
}

BOOLEAN SwPwrEvtIsr(WDFINTERRUPT Interrupt, ULONG Message)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(WdfInterruptGetDevice(Interrupt));
    UCHAR Edge = 0;
    NTSTATUS Status;
    UNREFERENCED_PARAMETER(Message);

    Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQ, &Edge);
    InterlockedExchange(&Dev->LastIrqStatus, Status);
    if (!NT_SUCCESS(Status)) {
        /* Prevent an unacknowledged level IRQ from continuously waking CPUs. */
        (VOID)SwPwrSetIrq(Dev, FALSE);
        (VOID)WdfInterruptQueueWorkItemForIsr(Interrupt);
        return TRUE;
    }
    if ((Edge & (ONOFFIRQ_EN0_F | ONOFFIRQ_EN0_R)) == 0) {
        return FALSE;
    }
    InterlockedExchange(&Dev->LastOnOffIrq, Edge);
    if (Dev->Vhf != NULL && (Edge & ONOFFIRQ_EN0_R) != 0) {
        InterlockedIncrement(&Dev->Presses);
        /* Emit a complete press+release pulse; wake presses emit SWPWR_WAKE. */
        SwPwrReport(Dev, SwPwrPressReport(Dev));
        SwPwrReport(Dev, 0);
    }
    (VOID)WdfInterruptQueueWorkItemForIsr(Interrupt);
    return TRUE;
}

VOID SwPwrEvtInterruptWorkItem(WDFINTERRUPT Interrupt, WDFOBJECT Object)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(WdfInterruptGetDevice(Interrupt));
    UNREFERENCED_PARAMETER(Object);
    /* Registry I/O runs outside the passive ISR lock and D0 transition. */
    SwPwrTrace(L"WakeIrqReady", Dev->IrqConfigured);
    SwPwrTrace(L"LastOnOffIrq", (ULONG)InterlockedCompareExchange(&Dev->LastOnOffIrq, 0, 0));
    SwPwrTrace(L"WakeIrqError", (ULONG)InterlockedCompareExchange(&Dev->LastIrqStatus, 0, 0));
    SwPwrTrace(L"Presses", (ULONG)InterlockedCompareExchange(&Dev->Presses, 0, 0));
    SwPwrFlushReportTraces(Dev);
}

/* ------------------------------------------------------------------- PnP */

NTSTATUS SwPwrEvtPrepareHardware(WDFDEVICE Device, WDFCMRESLIST Raw, WDFCMRESLIST Translated)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    ULONG Count = WdfCmResourceListGetCount(Translated), Index;

    for (Index = 0; Index < Count; ++Index) {
        PCM_PARTIAL_RESOURCE_DESCRIPTOR Desc = WdfCmResourceListGetDescriptor(Translated, Index);
        if (Desc == NULL) {
            continue;
        }
        if (Desc->Type == CmResourceTypeMemory &&
            Desc->u.Memory.Start.QuadPart == 0x7000e400 && Desc->u.Memory.Length == 4 &&
            Dev->Pmc == NULL) {
            /* Switchroot nvidia,invert-interrupt: PMIC INTR is active low,
             * while the GIC sees a level-high PMU_EXT interrupt (SPI 86 / GSI 118). */
            Dev->Pmc = MmMapIoSpaceEx(Desc->u.Memory.Start, sizeof(ULONG), PAGE_READWRITE | PAGE_NOCACHE);
            if (Dev->Pmc == NULL) {
                return STATUS_INSUFFICIENT_RESOURCES;
            }
            WRITE_REGISTER_ULONG(Dev->Pmc, READ_REGISTER_ULONG(Dev->Pmc) | PMC_INTR_POLARITY);
        }
        if (Desc->Type == CmResourceTypeConnection &&
            Desc->u.Connection.Class == CM_RESOURCE_CONNECTION_CLASS_SERIAL &&
            Desc->u.Connection.Type == CM_RESOURCE_CONNECTION_TYPE_SERIAL_I2C) {
            Dev->I2cId.LowPart = Desc->u.Connection.IdLowPart;
            Dev->I2cId.HighPart = (LONG)Desc->u.Connection.IdHighPart;
        }
        if (Desc->Type == CmResourceTypeInterrupt && Dev->Interrupt == NULL) {
            WDF_INTERRUPT_CONFIG Irq;
            NTSTATUS Status;
            WDF_INTERRUPT_CONFIG_INIT(&Irq, SwPwrEvtIsr, NULL);
            Irq.PassiveHandling = TRUE;
            Irq.CanWakeDevice = TRUE;
            Irq.AutomaticSerialization = FALSE;
            Irq.EvtInterruptWorkItem = SwPwrEvtInterruptWorkItem;
            Irq.InterruptRaw = WdfCmResourceListGetDescriptor(Raw, Index);
            Irq.InterruptTranslated = Desc;
            Status = WdfInterruptCreate(Device, &Irq, WDF_NO_OBJECT_ATTRIBUTES, &Dev->Interrupt);
            if (!NT_SUCCESS(Status)) {
                return Status;
            }
        }
    }
    if (Dev->I2cId.QuadPart == 0) {
        return STATUS_DEVICE_CONFIGURATION_ERROR;
    }
    if (Dev->Interrupt != NULL) {
        WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS Idle;
        WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS Wake;
        NTSTATUS Status;

        WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS_INIT(&Idle, IdleCanWakeFromS0);
        Idle.DxState = PowerDeviceD3;
        Idle.IdleTimeout = 1000;
        Idle.IdleTimeoutType = SystemManagedIdleTimeoutWithHint;
        Idle.ExcludeD3Cold = WdfTrue;
        Status = WdfDeviceAssignS0IdleSettings(Device, &Idle);
        SwPwrTrace(L"S0WakePolicyStatus", (ULONG)Status);
        /* Per KMDF's contract, lack of bus wake support keeps D0 usable. */
        if (Status == STATUS_POWER_STATE_INVALID) {
            return STATUS_SUCCESS;
        }
        if (!NT_SUCCESS(Status)) {
            return Status;
        }
        WDF_DEVICE_POWER_POLICY_WAKE_SETTINGS_INIT(&Wake);
        Wake.DxState = PowerDeviceD3;
        Wake.UserControlOfWakeSettings = WakeDoNotAllowUserControl;
        Wake.Enabled = WdfTrue;
        Status = WdfDeviceAssignSxWakeSettings(Device, &Wake);
        SwPwrTrace(L"SxWakePolicyStatus", (ULONG)Status);
        if (Status == STATUS_POWER_STATE_INVALID) {
            return STATUS_SUCCESS;
        }
        if (!NT_SUCCESS(Status)) {
            return Status;
        }
    }
    return STATUS_SUCCESS;
}

NTSTATUS SwPwrEvtReleaseHardware(WDFDEVICE Device, WDFCMRESLIST Translated)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    UNREFERENCED_PARAMETER(Translated);
    if (Dev->Pmc != NULL) {
        MmUnmapIoSpace(Dev->Pmc, sizeof(ULONG));
        Dev->Pmc = NULL;
    }
    return STATUS_SUCCESS;
}

NTSTATUS SwPwrEvtSelfManagedIoInit(WDFDEVICE Device)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    VHF_CONFIG Config;
    UCHAR Stat = 0, Irq = 0;
    NTSTATUS Status;

    if (Dev->I2c == NULL) {
        Status = SwPwrOpenTarget(Device, Dev->I2cId, &Dev->I2c);
        SwPwrTrace(L"OpenI2cStatus", (ULONG)Status);
        if (!NT_SUCCESS(Status)) {
            return Status;
        }
    }
    Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFSTAT, &Stat);
    SwPwrTrace(L"FirstReadStatus", (ULONG)Status);
    if (!NT_SUCCESS(Status)) {
        return Status;
    }
    if (Dev->Interrupt == NULL) {
        (VOID)SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQ, &Irq);    /* drop edges latched before Windows */
    }
    SwPwrTrace(L"OnOffStatAtStart", Stat);
    SwPwrTrace(L"OnOffIrqAtStart", Irq);
    SwPwrTrace(L"WakeIrqReady", Dev->IrqConfigured);
    Dev->Button.suppress_held = (Stat & ONOFFSTAT_EN0) != 0;

    if (Dev->Vhf == NULL) {
        VHF_CONFIG_INIT(&Config, WdfDeviceWdmGetDeviceObject(Device), sizeof(SwPwrReportDescriptor),
                        SwPwrReportDescriptor);
        Config.VhfClientContext = Dev;
        Config.VendorID = 0x057E;       /* Nintendo */
        Config.ProductID = 0x5601;
        Config.VersionNumber = 0x0005;
        Status = VhfCreate(&Config, &Dev->Vhf);
        SwPwrTrace(L"VhfCreateStatus", (ULONG)Status);
        if (!NT_SUCCESS(Status)) {
            Dev->Vhf = NULL;
            return Status;
        }
        Status = VhfStart(Dev->Vhf);
        SwPwrTrace(L"VhfStartStatus", (ULONG)Status);
        if (!NT_SUCCESS(Status)) {
            VhfDelete(Dev->Vhf, TRUE);
            Dev->Vhf = NULL;
            return Status;
        }
    }
    if (Dev->Timer == NULL) {
        WDF_TIMER_CONFIG TimerConfig;
        WDF_OBJECT_ATTRIBUTES Attributes;
        WDF_TIMER_CONFIG_INIT(&TimerConfig, SwPwrEvtTimer);
        TimerConfig.AutomaticSerialization = FALSE;
        WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&Attributes, SWPWR_TIMER);
        Attributes.ParentObject = Device;
        Attributes.ExecutionLevel = WdfExecutionLevelPassive;
        Status = WdfTimerCreate(&TimerConfig, &Attributes, &Dev->Timer);
        SwPwrTrace(L"TimerCreateStatus", (ULONG)Status);
        if (!NT_SUCCESS(Status)) {
            Dev->Timer = NULL;
            return Status;
        }
        SwPwrGetTimer(Dev->Timer)->Device = Device;
    }
    if (Dev->DisplayCallback == NULL) {
        Status = PoRegisterPowerSettingCallback(WdfDeviceWdmGetDeviceObject(Device),
            &SwPwrDisplayGuid, SwPwrDisplayChanged, Dev, &Dev->DisplayCallback);
        SwPwrTrace(L"DisplayCallbackStatus", (ULONG)Status);
        if (!NT_SUCCESS(Status)) {
            Dev->DisplayCallback = NULL;
            return Status;
        }
    }
    return SwPwrEvtRestart(Device);
}

NTSTATUS SwPwrEvtSuspend(WDFDEVICE Device)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    KIRQL Irql;
    KeAcquireSpinLock(&Dev->TimerLock, &Irql);
    Dev->Stopping = TRUE;
    KeReleaseSpinLock(&Dev->TimerLock, Irql);
    if (Dev->Timer != NULL) WdfTimerStop(Dev->Timer, TRUE);
    if (Dev->Button.held && Dev->Vhf != NULL) {
        SwPwrReport(Dev, 0);
        SwPwrFlushReportTraces(Dev);
    }
    Dev->Button.held = false;
    SwPwrTrace(L"Suspended", 1);
    return STATUS_SUCCESS;
}

NTSTATUS SwPwrEvtRestart(WDFDEVICE Device)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    UCHAR Irq = 0, Stat = 0;
    KIRQL Irql;
    NTSTATUS Status;

    if (Dev->Interrupt != NULL) {
        Dev->Stopping = FALSE;
        SwPwrTrace(L"Suspended", 0);
        return STATUS_SUCCESS; /* no 50 ms polling timer in the wake-interrupt profile */
    }
    Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFIRQ, &Irq);
    if (NT_SUCCESS(Status)) Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFSTAT, &Stat);
    SwPwrTrace(L"ResumeReadStatus", (ULONG)Status);
    if (!NT_SUCCESS(Status)) return Status;
    Dev->Button.held = false;
    Dev->Button.suppress_held = (Stat & ONOFFSTAT_EN0) != 0;
    SwPwrTrace(L"Suspended", 0);
    KeAcquireSpinLock(&Dev->TimerLock, &Irql);
    Dev->Stopping = FALSE;
    WdfTimerStart(Dev->Timer, WDF_REL_TIMEOUT_IN_MS(POLL_MS));
    KeReleaseSpinLock(&Dev->TimerLock, Irql);
    return STATUS_SUCCESS;
}

VOID SwPwrEvtSelfManagedIoCleanup(WDFDEVICE Device)
{
    PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
    (VOID)SwPwrEvtSuspend(Device);
    if (Dev->DisplayCallback != NULL) {
        PoUnregisterPowerSettingCallback(Dev->DisplayCallback);
        Dev->DisplayCallback = NULL;
    }
    if (Dev->Vhf != NULL) {
        VhfDelete(Dev->Vhf, TRUE);
        Dev->Vhf = NULL;
    }
    if (Dev->I2c != NULL) {
        WdfIoTargetClose(Dev->I2c);
        WdfObjectDelete(Dev->I2c);
        Dev->I2c = NULL;
    }
}

NTSTATUS SwPwrEvtDeviceAdd(WDFDRIVER Driver, PWDFDEVICE_INIT DeviceInit)
{
    WDF_PNPPOWER_EVENT_CALLBACKS Pnp;
    WDF_OBJECT_ATTRIBUTES Attributes;
    WDFDEVICE Device;
    NTSTATUS Status;
    UNREFERENCED_PARAMETER(Driver);

    WDF_PNPPOWER_EVENT_CALLBACKS_INIT(&Pnp);
    Pnp.EvtDevicePrepareHardware = SwPwrEvtPrepareHardware;
    Pnp.EvtDeviceReleaseHardware = SwPwrEvtReleaseHardware;
    Pnp.EvtDeviceD0Entry = SwPwrEvtD0Entry;
    Pnp.EvtDeviceD0Exit = SwPwrEvtD0Exit;
    Pnp.EvtDeviceSelfManagedIoInit = SwPwrEvtSelfManagedIoInit;
    Pnp.EvtDeviceSelfManagedIoCleanup = SwPwrEvtSelfManagedIoCleanup;
    Pnp.EvtDeviceSelfManagedIoSuspend = SwPwrEvtSuspend;
    Pnp.EvtDeviceSelfManagedIoRestart = SwPwrEvtRestart;
    WdfDeviceInitSetPnpPowerEventCallbacks(DeviceInit, &Pnp);
    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&Attributes, SWPWR_DEVICE);
    Status = WdfDeviceCreate(&DeviceInit, &Attributes, &Device);
    if (NT_SUCCESS(Status)) {
        PSWPWR_DEVICE Dev = SwPwrGetDevice(Device);
        KeInitializeSpinLock(&Dev->TimerLock);
        Dev->Stopping = TRUE;
    }
    return Status;
}

NTSTATUS DriverEntry(PDRIVER_OBJECT DriverObject, PUNICODE_STRING RegistryPath)
{
    WDF_DRIVER_CONFIG Config;

    if (RegistryPath->Length / sizeof(WCHAR) + 12 < RTL_NUMBER_OF(SwPwrParamsPath)) {
        RtlCopyMemory(SwPwrParamsPath, RegistryPath->Buffer, RegistryPath->Length);
        RtlCopyMemory(&SwPwrParamsPath[RegistryPath->Length / sizeof(WCHAR)], L"\\Parameters", sizeof(L"\\Parameters"));
        (VOID)RtlCreateRegistryKey(RTL_REGISTRY_ABSOLUTE, SwPwrParamsPath);
    }
    SwPwrTrace(L"DriverVersion", 5);
    WDF_DRIVER_CONFIG_INIT(&Config, SwPwrEvtDeviceAdd);
    return WdfDriverCreate(DriverObject, RegistryPath, WDF_NO_OBJECT_ATTRIBUTES, &Config, WDF_NO_HANDLE);
}
