/* SPDX-License-Identifier: MIT
 * KMDF S0 idle policy for peripheral devices that only need active polling or
 * host I/O while the display is on. The power button uses a dedicated PMIC
 * wake-capable interrupt and is never governed by this helper. Power-setting
 * notifications only enqueue work; no I/O or synchronous D0 wait runs inside
 * the notification callback.
 */
#ifndef SWITCH_DISPLAY_IDLE_H
#define SWITCH_DISPLAY_IDLE_H

typedef struct {
    WDFDEVICE Device;
    WDFWORKITEM Work;
    WDFWAITLOCK Lock;
    PVOID volatile Callback;
    ULONG IdleTimeoutMs;
    volatile LONG DisplayOn, Closing;
    BOOLEAN Held;
    BOOLEAN S0IdleDisabled;
    NTSTATUS LastStatus;
} SW_DISPLAY_IDLE;
typedef struct { SW_DISPLAY_IDLE *Idle; } SW_DISPLAY_IDLE_WORK;
WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(SW_DISPLAY_IDLE_WORK, SwDisplayIdleWork)
static const GUID SwDisplayIdleGuid = {0x6fe69556, 0x704a, 0x47a0, {0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47}};

static NTSTATUS SwDisplayIdleAssignSettings(WDFDEVICE Device, ULONG IdleTimeoutMs, WDF_TRI_STATE Enabled)
{
    WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS settings;
    WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS_INIT(&settings, IdleCannotWakeFromS0);
    settings.DxState = PowerDeviceD3;
    settings.IdleTimeout = IdleTimeoutMs ? IdleTimeoutMs : 1000;
    settings.IdleTimeoutType = SystemManagedIdleTimeoutWithHint;
    settings.ExcludeD3Cold = WdfTrue;
    settings.PowerUpIdleDeviceOnSystemWake = WdfTrue;
    settings.UserControlOfIdleSettings = IdleDoNotAllowUserControl;
    settings.Enabled = Enabled;
    return WdfDeviceAssignS0IdleSettings(Device, &settings);
}

static VOID SwDisplayIdleWorker(WDFWORKITEM Work)
{
    SW_DISPLAY_IDLE *idle = SwDisplayIdleWork(Work)->Idle;
    WdfWaitLockAcquire(idle->Lock, NULL);
    for (;;) {
        LONG want;
        if (InterlockedCompareExchange(&idle->Closing, 0, 0)) break;
        want = InterlockedCompareExchange(&idle->DisplayOn, 0, 0);
        if (want) {
            if (idle->Held) break;
            /* Never block a D0 transition whose callback may need this worker. */
            idle->LastStatus = WdfDeviceStopIdle(idle->Device, FALSE);
            if (!NT_SUCCESS(idle->LastStatus)) break;
            idle->Held = TRUE;
        } else {
            if (!idle->Held) break;
            idle->Held = FALSE;
            WdfDeviceResumeIdle(idle->Device);
        }
    }
    WdfWaitLockRelease(idle->Lock);
}

static NTSTATUS SwDisplayIdleChanged(LPCGUID Setting, PVOID Value, ULONG Length, PVOID Context)
{
    SW_DISPLAY_IDLE *idle = Context;
    if (Length == sizeof(ULONG) && IsEqualGUID(Setting, &SwDisplayIdleGuid) &&
        !InterlockedCompareExchange(&idle->Closing, 0, 0) && idle->Work != NULL) {
        InterlockedExchange(&idle->DisplayOn, *(PULONG)Value != 0);
        WdfWorkItemEnqueue(idle->Work);
    }
    return STATUS_SUCCESS;
}

static NTSTATUS SwDisplayIdleConfigureTimeout(WDFDEVICE Device, SW_DISPLAY_IDLE *Idle, ULONG IdleTimeoutMs)
{
    WDF_WORKITEM_CONFIG work;
    WDF_OBJECT_ATTRIBUTES attributes;
    NTSTATUS status;
    Idle->Device = Device;
    Idle->IdleTimeoutMs = IdleTimeoutMs ? IdleTimeoutMs : 1000;
    Idle->DisplayOn = 1;
    Idle->Closing = 0;
    Idle->Held = FALSE;
    Idle->S0IdleDisabled = FALSE;
    WDF_OBJECT_ATTRIBUTES_INIT(&attributes);
    attributes.ParentObject = Device;
    status = WdfWaitLockCreate(&attributes, &Idle->Lock);
    if (!NT_SUCCESS(status)) {
        Idle->Lock = NULL;
        return status;
    }
    WDF_WORKITEM_CONFIG_INIT(&work, SwDisplayIdleWorker);
    work.AutomaticSerialization = FALSE;
    WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, SW_DISPLAY_IDLE_WORK);
    attributes.ParentObject = Device;
    status = WdfWorkItemCreate(&work, &attributes, &Idle->Work);
    if (!NT_SUCCESS(status)) {
        Idle->Work = NULL;
        WdfObjectDelete(Idle->Lock);
        Idle->Lock = NULL;
        return status;
    }
    SwDisplayIdleWork(Idle->Work)->Idle = Idle;
    status = SwDisplayIdleAssignSettings(Device, Idle->IdleTimeoutMs, WdfTrue);
    if (!NT_SUCCESS(status)) {
        WdfObjectDelete(Idle->Work);
        Idle->Work = NULL;
        WdfObjectDelete(Idle->Lock);
        Idle->Lock = NULL;
    }
    return status;
}

static __inline NTSTATUS SwDisplayIdleConfigure(WDFDEVICE Device, SW_DISPLAY_IDLE *Idle)
{
    return SwDisplayIdleConfigureTimeout(Device, Idle, 1000);
}

/* Called after the first D0 entry, not from DeviceAdd. Later D0 entries leave
 * the existing screen-off decision and reference count intact. */
static NTSTATUS SwDisplayIdleStart(SW_DISPLAY_IDLE *Idle)
{
    NTSTATUS status;
    PVOID callback = NULL;
    BOOLEAN needEnable;
    if (!Idle->Device || !Idle->Work || !Idle->Lock) return STATUS_INVALID_DEVICE_STATE;
    if (InterlockedCompareExchangePointer(&Idle->Callback, NULL, NULL) != NULL) return STATUS_SUCCESS;
    WdfWaitLockAcquire(Idle->Lock, NULL);
    needEnable = Idle->S0IdleDisabled;
    WdfWaitLockRelease(Idle->Lock);
    if (needEnable) {
        status = SwDisplayIdleAssignSettings(Idle->Device, Idle->IdleTimeoutMs, WdfTrue);
        if (!NT_SUCCESS(status)) return status;
    }
    WdfWaitLockAcquire(Idle->Lock, NULL);
    if (InterlockedCompareExchangePointer(&Idle->Callback, NULL, NULL) != NULL) {
        WdfWaitLockRelease(Idle->Lock);
        return STATUS_SUCCESS;
    }
    Idle->S0IdleDisabled = FALSE;
    InterlockedExchange(&Idle->Closing, 0);
    InterlockedExchange(&Idle->DisplayOn, 1);
    if (!Idle->Held) {
        status = WdfDeviceStopIdle(Idle->Device, FALSE);
        if (!NT_SUCCESS(status)) {
            Idle->LastStatus = status;
            InterlockedExchange(&Idle->Closing, 1);
            Idle->S0IdleDisabled = TRUE;
            WdfWaitLockRelease(Idle->Lock);
            (VOID)SwDisplayIdleAssignSettings(Idle->Device, Idle->IdleTimeoutMs, WdfFalse);
            return status;
        }
        Idle->Held = TRUE;
    }
    WdfWaitLockRelease(Idle->Lock);
    status = PoRegisterPowerSettingCallback(WdfDeviceWdmGetDeviceObject(Idle->Device),
        &SwDisplayIdleGuid, SwDisplayIdleChanged, Idle, &callback);
    if (!NT_SUCCESS(status)) {
        InterlockedExchange(&Idle->Closing, 1);
        WdfWorkItemFlush(Idle->Work);
        WdfWaitLockAcquire(Idle->Lock, NULL);
        InterlockedExchangePointer(&Idle->Callback, NULL);
        if (Idle->Held) {
            Idle->Held = FALSE;
            WdfDeviceResumeIdle(Idle->Device);
        }
        Idle->S0IdleDisabled = TRUE;
        WdfWaitLockRelease(Idle->Lock);
        (VOID)SwDisplayIdleAssignSettings(Idle->Device, Idle->IdleTimeoutMs, WdfFalse);
        return status;
    }
    InterlockedExchangePointer(&Idle->Callback, callback);
    if (InterlockedCompareExchange(&Idle->Closing, 0, 0)) {
        callback = InterlockedExchangePointer(&Idle->Callback, NULL);
        if (callback) PoUnregisterPowerSettingCallback(callback);
        WdfWorkItemFlush(Idle->Work);
        WdfWaitLockAcquire(Idle->Lock, NULL);
        if (Idle->Held) {
            Idle->Held = FALSE;
            WdfDeviceResumeIdle(Idle->Device);
        }
        WdfWaitLockRelease(Idle->Lock);
    }
    return STATUS_SUCCESS;
}

static VOID SwDisplayIdleStop(SW_DISPLAY_IDLE *Idle)
{
    PVOID callback;
    InterlockedExchange(&Idle->Closing, 1);
    callback = InterlockedExchangePointer(&Idle->Callback, NULL);
    if (callback) PoUnregisterPowerSettingCallback(callback);
    if (Idle->Work) WdfWorkItemFlush(Idle->Work);
    if (Idle->Lock) {
        WdfWaitLockAcquire(Idle->Lock, NULL);
        if (Idle->Held) {
            Idle->Held = FALSE;
            WdfDeviceResumeIdle(Idle->Device);
        }
        WdfWaitLockRelease(Idle->Lock);
    } else if (Idle->Held) {
        Idle->Held = FALSE;
        WdfDeviceResumeIdle(Idle->Device);
    }
}
#endif
