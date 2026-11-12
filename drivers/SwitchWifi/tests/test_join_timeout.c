/* SPDX-License-Identifier: GPL-2.0-only */
/* Production disconnect timer and BSSID readback, with OS/firmware substitutes.
 * No Windows execution or physical wireless operation is exercised. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL line %u: %s\n", __LINE__, #x); exit(1); } } while (0)
typedef void VOID;
typedef uint8_t UCHAR, BOOLEAN;
typedef uint16_t USHORT;
typedef int16_t SHORT;
typedef int32_t NTSTATUS;
typedef uint64_t ULONGLONG;
typedef unsigned WDI_ASSOC_STATUS;
#define TRUE 1
#define FALSE 0
#define STATUS_SUCCESS ((NTSTATUS)0)
#define STATUS_PENDING ((NTSTATUS)0x103)
#define STATUS_UNSUCCESSFUL ((NTSTATUS)0xc0000001u)
#define STATUS_IO_TIMEOUT ((NTSTATUS)0xc00000b5u)
#define STATUS_DEVICE_PROTOCOL_ERROR ((NTSTATUS)0xc0000186u)
#define NT_SUCCESS(s) ((s) >= 0)
#define RtlEqualMemory(a,b,n) (!memcmp((a),(b),(n)))
enum { WDI_ASSOC_STATUS_DISASSOCIATED_BY_HOST = 1 };
enum { SwifiLeaveReadback = 3, SwifiLeaveConfirmed = 4 };
typedef struct { unsigned Report; } SWIFI_DEVICE;
static SWIFI_DEVICE owner;
typedef struct { UCHAR Peer[6]; } JOIN_CANDIDATE;
typedef struct {
    SWIFI_DEVICE *Owner;
    BOOLEAN Online, Leaving, Poisoned;
    ULONGLONG Deadline;
    JOIN_CANDIDATE *Active;
    void *Control, *Lock;
} SWIFI_JOIN;
typedef struct { SWIFI_JOIN *Join; } JOIN_TIMER_CONTEXT, *WDFTIMER;
static JOIN_TIMER_CONTEXT *JoinTimerContext(WDFTIMER Timer) { return Timer; }
static unsigned lock_depth, queries, downs, poisons, leaves;
static ULONGLONG now;
static NTSTATUS query_status, poison_status;
static SHORT firmware_status;
static USHORT reply_length;
static UCHAR bssid[6];
static void SwifiReportLeave(unsigned *Report, unsigned Phase, NTSTATUS Status) {
    (void)Status; CHECK(Report == &owner.Report); *Report = Phase;
}
static void WdfWaitLockAcquire(void *Lock, void *Timeout) {
    (void)Lock; (void)Timeout; CHECK(!lock_depth); ++lock_depth;
}
static void WdfWaitLockRelease(void *Lock) { (void)Lock; CHECK(lock_depth == 1); --lock_depth; }
static ULONGLONG KeQueryInterruptTime(void) { return now; }
static NTSTATUS SwifiFirmwareCommand(void *Control, UCHAR IfIndex, unsigned Command,
    void *Buffer, USHORT Length, USHORT *Returned, SHORT *Error)
{
    (void)Control; CHECK(!IfIndex && Command == 23 && Length == 6 && lock_depth == 1);
    ++queries; memcpy(Buffer, bssid, 6); *Returned = reply_length; *Error = firmware_status;
    return query_status;
}
static void Down(SWIFI_JOIN *J, unsigned Reason) {
    CHECK(lock_depth == 1 && Reason == WDI_ASSOC_STATUS_DISASSOCIATED_BY_HOST);
    ++downs; J->Leaving = FALSE; J->Deadline = 0; J->Active = NULL;
}
static void Poison(SWIFI_JOIN *J, NTSTATUS Reason) {
    CHECK(lock_depth == 1); ++poisons; poison_status = Reason; J->Poisoned = TRUE;
}
static NTSTATUS BeginLeave(SWIFI_JOIN *J, USHORT Reason, NTSTATUS Status, BOOLEAN Retry) {
    CHECK(lock_depth == 1 && Reason == 3 && Status == STATUS_IO_TIMEOUT && Retry);
    ++leaves; J->Leaving = TRUE; return STATUS_SUCCESS;
}

#include "join-timeout-under-test.inc"

static SWIFI_JOIN reset(JOIN_CANDIDATE *Candidate)
{
    CHECK(!lock_depth);
    queries = downs = poisons = leaves = 0;
    now = 100; query_status = STATUS_SUCCESS; firmware_status = 0; reply_length = 6;
    memset(bssid, 0, sizeof(bssid));
    SWIFI_JOIN J = {.Owner = &owner, .Online = TRUE, .Leaving = TRUE, .Deadline = now, .Active = Candidate};
    return J;
}

int main(void)
{
    (void)Down; /* The pre-fix timer has no path to normal disconnect cleanup. */
    JOIN_CANDIDATE candidate = {.Peer = {2, 3, 4, 5, 6, 7}};
    SWIFI_JOIN j = reset(&candidate);
    BOOLEAN confirmed = FALSE;
    WdfWaitLockAcquire(NULL, NULL);
    CHECK(LinkIsDown(&j, &confirmed) == STATUS_SUCCESS && confirmed);
    WdfWaitLockRelease(NULL);
    j = reset(&candidate);
    JOIN_TIMER_CONTEXT timer = {&j};
    JoinTimeout(&timer);
    CHECK(queries == 1 && downs == 1 && !poisons && !j.Poisoned && !j.Active);

    j = reset(&candidate); query_status = STATUS_UNSUCCESSFUL; firmware_status = -17;
    JoinTimeout(&timer);
    CHECK(queries == 1 && downs == 1 && !poisons);

    j = reset(&candidate); memcpy(bssid, candidate.Peer, 6);
    JoinTimeout(&timer);
    CHECK(!downs && poisons == 1 && poison_status == STATUS_IO_TIMEOUT);
    j = reset(&candidate); bssid[0] = 4;
    JoinTimeout(&timer);
    CHECK(!downs && poisons == 1 && poison_status == STATUS_DEVICE_PROTOCOL_ERROR);
    j = reset(&candidate); query_status = STATUS_IO_TIMEOUT;
    JoinTimeout(&timer);
    CHECK(!downs && poisons == 1 && poison_status == STATUS_IO_TIMEOUT);
    j = reset(&candidate); reply_length = 5;
    JoinTimeout(&timer);
    CHECK(!downs && poisons == 1 && poison_status == STATUS_DEVICE_PROTOCOL_ERROR);

    j = reset(&candidate); j.Leaving = FALSE;
    JoinTimeout(&timer);
    CHECK(leaves == 1 && !queries && !downs && !poisons);
    j = reset(&candidate); j.Online = FALSE;
    JoinTimeout(&timer); CHECK(!queries && !poisons);
    j = reset(&candidate); j.Deadline = 0;
    JoinTimeout(&timer); CHECK(!queries && !poisons);
    j = reset(&candidate); j.Deadline = now + 1;
    JoinTimeout(&timer); CHECK(!queries && !poisons);

    /* A queued old down-event must not discard a newly established link to
     * the same AP, while an actual down-state still runs normal cleanup. */
    j = reset(&candidate); j.Leaving = FALSE; memcpy(bssid, candidate.Peer, 6);
    WdfWaitLockAcquire(NULL, NULL);
    ConfirmLinkDown(&j, WDI_ASSOC_STATUS_DISASSOCIATED_BY_HOST, FALSE);
    WdfWaitLockRelease(NULL);
    CHECK(queries == 1 && !downs && !poisons && j.Active == &candidate);
    memset(bssid, 0, sizeof(bssid));
    WdfWaitLockAcquire(NULL, NULL);
    ConfirmLinkDown(&j, WDI_ASSOC_STATUS_DISASSOCIATED_BY_HOST, FALSE);
    WdfWaitLockRelease(NULL);
    CHECK(queries == 2 && downs == 1 && !poisons);
    CHECK(!lock_depth);
    puts("PASS: actual join timeout reads firmware link state before failure; missing events, still-associated, malformed and I/O-failure controls; mocked OS/radio");
    return 0;
}
