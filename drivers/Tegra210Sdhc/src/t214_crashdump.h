/* SPDX-License-Identifier: GPL-2.0-only
 * T214 SDPORT dump support. Uses the Hekate/Switchroot board primitives in
 * t214_sdmmc.h. Identity polling derives from this project's f6ee89ede53e
 * S4 experiment; this path allocates no DMA buffer and does not enable S4.
 */

/* SDPORT_INITIALIZE is a PASSIVE_LEVEL preparation callback (sdport.h).
 * Map resources now; do not reset, retime or power-cycle the live boot disk.
 * Later bus/request callbacks use only these mappings and bounded polling. */
static NTSTATUS T214InitializeDump(PSDHC_EXTENSION s)
{
    NTSTATUS status = STATUS_INSUFFICIENT_RESOURCES;
    s->T214Car = T214Map(0x60006000, 0x1000);
    s->T214Misc = T214Map(0x70000000, 0x4000);
    s->T214Pmc = T214Map(0x7000e000, 0x1000);
    s->T214I2c = T214Map(0x7000d000, 0x1000);
    s->T214Gpio = T214Map(0x6000d000, 0x1000);
    if (!s->T214Car || !s->T214Misc || !s->T214Pmc || !s->T214I2c || !s->T214Gpio) goto fail;
    if ((T214Read(s->T214Misc, 0x804) & 0xfff0u) != 0x2120u) {
        status = STATUS_NOT_SUPPORTED;
        goto fail;
    }
    s->Capabilities.MaximumBlockCount = 32; /* 16 KiB, PIO only. */
    s->Capabilities.MaximumBlockSize = 512;
    return STATUS_SUCCESS;
fail:
    T214Release(s);
    return status;
}

/* Called only for the dump stack's reset-all, never during preparation.
 * The live driver can have selected DDR200 after our extension was created.
 * Inspect CAR now, stop SDCLK before changing its source, and leave it off
 * until SDPORT sets the identification clock on the re-enumerated card. */
static NTSTATUS T214DumpTakeoverClock(PSDHC_EXTENSION s)
{
    const ULONG sdmmc1 = 1u << 14;
    PUCHAR car = s->T214Car;
    BOOLEAN ready = (T214Read(car, 0x10) & sdmmc1) &&
                    !(T214Read(car, 0x04) & sdmmc1);
    if (ready) {
        T214Write(s->BaseAddress, SDHC_INTERRUPT_SIGNAL_ENABLE, 0);
        SdhcWriteRegisterUshort(s, SDHC_CLOCK_CONTROL, 0);
    }
    /* Abort inherited ADMA as well as card traffic before retiming the host.
     * Only SDMMC1 is held in reset; other CAR clients are untouched. */
    T214Write(car, 0x300, sdmmc1);
    if (!(T214Read(car, 0x04) & sdmmc1)) return STATUS_DEVICE_POWER_FAILURE;
    NTSTATUS status = T214ParentClock(s);
    if (!NT_SUCCESS(status)) return status;
    if ((T214Read(car, 0x150) & 0xe00000ffu) != (3u << 29))
        return STATUS_DEVICE_CONFIGURATION_ERROR;
    T214Write(car, 0x320, sdmmc1);
    KeStallExecutionProcessor(2);
    if (!(T214Read(car, 0x10) & sdmmc1)) return STATUS_DEVICE_POWER_FAILURE;
    T214Write(car, 0x304, sdmmc1);
    KeStallExecutionProcessor(2);
    if (T214Read(car, 0x04) & sdmmc1) return STATUS_DEVICE_POWER_FAILURE;
    s->SpeedMode = SdhcSpeedModeNormal;
#if NV_T214_DDR200
    s->T214DdrActive = FALSE;
    s->T214DdrFatal = FALSE;
    s->T214DdrRecoverNeeded = FALSE;
    s->T214DdrAttempted = TRUE;
#endif
    return STATUS_SUCCESS;
}

static NTSTATUS T214ReadIdentitySector(PSDHC_EXTENSION s, ULONG Lba, PUCHAR Buffer)
{
    if (!T214WaitClear(s->BaseAddress, SDHC_PRESENT_STATE,
                      SDHC_PS_CMD_INHIBIT | SDHC_PS_DAT_INHIBIT, 100000))
        return STATUS_DEVICE_BUSY;
    T214Write(s->BaseAddress, SDHC_INTERRUPT_STATUS, 0xffffffff);
    T214Write(s->BaseAddress, SDHC_INTERRUPT_STATUS_ENABLE, 0x037f8023);
    SdhcWriteRegisterUchar(s, SDHC_TIMEOUT_CONTROL, SDHC_TC_MAX_DATA_TIMEOUT);
    SdhcWriteRegisterUshort(s, SDHC_BLOCK_SIZE, 512);
    SdhcWriteRegisterUshort(s, SDHC_BLOCK_COUNT, 1);
    SdhcWriteRegisterUshort(s, SDHC_TRANSFER_MODE, SDHC_TM_TRANSFER_READ | SDHC_TM_BLKCNT_ENABLE);
    SdhcWriteRegisterUlong(s, SDHC_ARGUMENT, Lba);
    SdhcWriteRegisterUshort(s, SDHC_COMMAND, (17u << 8) | SDHC_CMD_RESPONSE_48BIT_NOBUSY |
        SDHC_CMD_CRC_CHECK_ENABLE | SDHC_CMD_INDEX_CHECK_ENABLE | SDHC_CMD_DATA_PRESENT);
    BOOLEAN received = FALSE, commandDone = FALSE, transferDone = FALSE;
    for (ULONG elapsed = 0; elapsed < 1000000; elapsed += 5) {
        ULONG events = T214Read(s->BaseAddress, SDHC_INTERRUPT_STATUS);
        if (events & 0xffff8000u) return STATUS_IO_DEVICE_ERROR;
        if (events & SDHC_IS_CMD_COMPLETE) commandDone = TRUE;
        if (events & SDHC_IS_TRANSFER_COMPLETE) transferDone = TRUE;
        if ((events & SDHC_IS_BUFFER_READ_READY) && !received) {
            for (ULONG i = 0; i < 512; i += sizeof(ULONG)) {
                ULONG word = SdhcReadRegisterUlong(s, SDHC_DATA_PORT);
                RtlCopyMemory(Buffer + i, &word, sizeof(word));
            }
            received = TRUE;
        }
        SdhcWriteRegisterUshort(s, SDHC_INTERRUPT_STATUS, (USHORT)events);
        if (received && commandDone && transferDone) return STATUS_SUCCESS;
        KeStallExecutionProcessor(5);
    }
    return STATUS_IO_TIMEOUT;
}

/* SDPORT's dump writer can go straight to saved pagefile extents without
 * reading the partition tables. Validate the same physical MBR and NTFS
 * identity as runtime I/O before allowing its first CMD24/25. */
static NTSTATUS T214ValidateDumpTarget(PSDHC_EXTENSION s)
{
    if (!(s->TargetFacts & LiteSdxc) || s->AppPrefixAccepted)
        return STATUS_MEDIA_WRITE_PROTECTED;
    if (s->T214DmaActive || s->OutstandingRequests[0]) return STATUS_DEVICE_BUSY;
    ULONG signals = T214Read(s->BaseAddress, SDHC_INTERRUPT_SIGNAL_ENABLE);
    ULONG enables = T214Read(s->BaseAddress, SDHC_INTERRUPT_STATUS_ENABLE);
    UCHAR timeout = SdhcReadRegisterUchar(s, SDHC_TIMEOUT_CONTROL);
    ULONG next = s->NextReadLba;
    UCHAR block[512];
    SDPORT_COMMAND command = {0};
    command.BlockSize = 512;
    command.DataBuffer = block;
    T214Write(s->BaseAddress, SDHC_INTERRUPT_SIGNAL_ENABLE, 0);
    NTSTATUS status = T214ReadIdentitySector(s, 0, block);
    if (NT_SUCCESS(status)) {
        s->NextReadLba = 0;
        LiteObserveRead(s, &command);
        if (!(s->TargetFacts & LiteMbr)) status = STATUS_MEDIA_WRITE_PROTECTED;
    }
    if (NT_SUCCESS(status)) {
        status = T214ReadIdentitySector(s, WINARM_FIRST, block);
        if (NT_SUCCESS(status)) {
            s->NextReadLba = WINARM_FIRST;
            LiteObserveRead(s, &command);
            if ((s->TargetFacts & LiteWriteFacts) != LiteWriteFacts) status = STATUS_MEDIA_WRITE_PROTECTED;
        }
    }
    if (!NT_SUCCESS(status)) {
        InterlockedAnd(&s->TargetFacts, ~LiteWriteFacts);
        SdhcWriteRegisterUchar(s, SDHC_RESET, SDHC_RESET_CMD | SDHC_RESET_DAT);
        ULONG n;
        for (n = 0; n < 1000; n += 5) {
            if (!(SdhcReadRegisterUchar(s, SDHC_RESET) & (SDHC_RESET_CMD | SDHC_RESET_DAT))) break;
            KeStallExecutionProcessor(5);
        }
        if (n == 1000) status = STATUS_IO_TIMEOUT;
    }
    s->NextReadLba = next;
    SdhcWriteRegisterUchar(s, SDHC_TIMEOUT_CONTROL, timeout);
    T214Write(s->BaseAddress, SDHC_INTERRUPT_STATUS, 0xffffffff);
    T214Write(s->BaseAddress, SDHC_INTERRUPT_STATUS_ENABLE, enables);
    T214Write(s->BaseAddress, SDHC_INTERRUPT_SIGNAL_ENABLE, signals);
    return status;
}

static NTSTATUS T214PrepareDumpWrite(PSDHC_EXTENSION s, PSDPORT_REQUEST r)
{
    if (!s->CrashdumpMode || r->Type != SdRequestTypeCommandWithTransfer ||
        (r->Command.Index != 24 && r->Command.Index != 25) ||
        !LiteBlocksInExtent(r->Command.Argument, r->Command.BlockCount, WINARM_FIRST, WINARM_COUNT) ||
        (s->TargetFacts & LiteWriteFacts) == LiteWriteFacts)
        return STATUS_SUCCESS;
    return T214ValidateDumpTarget(s);
}
