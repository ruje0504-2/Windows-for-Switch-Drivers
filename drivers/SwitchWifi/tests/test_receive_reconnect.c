/* SPDX-License-Identifier: ISC */
/* Exercise the actual Windows RX completion, join and indication functions.
 * WDF/NetAdapterCx plumbing is substituted; packet policy and conversion are
 * production code. This is a host control, not a Windows or radio test. */
#include "../core/msgbuf_data.h"
#include "../core/station_security.h"
#include "../core/station_frame.h"
#include "../core/rx_metadata.h"
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef void VOID;
typedef void *PVOID;
typedef uint8_t UCHAR, BOOLEAN, *PUCHAR;
typedef uint16_t USHORT;
typedef uint32_t ULONG;
typedef size_t SIZE_T;
typedef unsigned KIRQL;
#define TRUE 1
#define FALSE 0
#define RtlEqualMemory(a,b,n) (!memcmp((a),(b),(n)))
#define RtlCopyMemory(a,b,n) memcpy((a),(b),(n))
#define RtlZeroMemory(a,n) memset((a),0,(n))
#define CONTAINING_RECORD(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))

typedef struct LIST_ENTRY { struct LIST_ENTRY *Flink, *Blink; } LIST_ENTRY;
static void InitializeListHead(LIST_ENTRY *h) { h->Flink=h->Blink=h; }
static int IsListEmpty(const LIST_ENTRY *h) { return h->Flink==h; }
static void InsertTailList(LIST_ENTRY *h, LIST_ENTRY *e) {
    e->Blink=h->Blink; e->Flink=h; h->Blink->Flink=e; h->Blink=e;
}
static void RemoveEntryList(LIST_ENTRY *e) { e->Blink->Flink=e->Flink; e->Flink->Blink=e->Blink; }
static LIST_ENTRY *RemoveHeadList(LIST_ENTRY *h) { LIST_ENTRY *e=h->Flink; RemoveEntryList(e); return e; }

enum { PathRxCompletion=2, PathEapol=4, PathQueued=8, PathIndicated=16,
       PathDescriptorError=256, PathNativeDrop=512, PathFilterDrop=1024,
       PathEapolHeaderDrop=2048, PathKeyPolicyDrop=4096, PathForeignAuth=32768 };
enum { SwifiRxEtherBeforeKey, SwifiRxEtherUncertain, SwifiRxNativeClear, SwifiRxNativeProtected };
enum { NetPacketLayer2TypeIeee80211=1, POOL=4 };

typedef struct SWIFI_RX_BUFFER {
    LIST_ENTRY Link;
    ULONG Epoch, KeyEpoch, AcceptedKeyEpoch;
    BOOLEAN KeyInFlight;
    USHORT Offset, Length;
    BOOLEAN Protected, DerivedClear;
    enum { SwifiRxFree, SwifiRxPosted, SwifiRxReady } State;
} SWIFI_RX_BUFFER;
typedef struct { ULONG Counts[4]; } SWIFI_REPORT;
typedef struct { int ProtocolLock; } SWIFI_HARDWARE;
typedef struct { SIZE_T Offset, ValidLength, Capacity; } NET_FRAGMENT;
typedef struct {
    int Ignore; ULONG FragmentIndex, FragmentCount;
    struct { ULONG Layer2Type, Layer2HeaderLength; } Layout;
} NET_PACKET;
typedef struct {
    ULONG BeginIndex, EndIndex, NextIndex;
    NET_PACKET Packets[2]; NET_FRAGMENT Fragments[2];
} NET_RING;
typedef struct { void *VirtualAddress; } FRAGMENT_VA;
typedef struct { FRAGMENT_VA Slots[2]; } NET_EXTENSION;
typedef struct SWIFI_QUEUE SWIFI_QUEUE;
typedef struct SWIFI_STATION_DATA {
    SWIFI_HARDWARE *Hardware;
    SWIFI_QUEUE *RxQueue;
    swifi_data Core;
    swifi_eapol_tx KeyTx;
    struct { UCHAR *Memory; } RxMemory;
    SWIFI_RX_BUFFER Rx[POOL];
    LIST_ENTRY FreeRx, ReadyRx;
    ULONG Epoch, KeyEpoch, TxProgress, RxDrops, PacketPath;
    uint64_t NextLinkCheck;
    USHORT Sequence;
    UCHAR Address[6], Peer[6], EapolExemption[2];
    BOOLEAN Enabled, Associated, Joining, Secure, Pairwise, Group;
    BOOLEAN ExcludeUnencrypted, NativeProtectedSeen, SampleValid;
    /* Retained only so the pre-fix source can demonstrate the regression. */
    BOOLEAN FirstJoin;
} SWIFI_STATION_DATA;
typedef struct {
    SWIFI_STATION_DATA Data; SWIFI_HARDWARE Hw; SWIFI_REPORT Report; int Filter;
} SWIFI_DEVICE;
struct SWIFI_QUEUE {
    SWIFI_STATION_DATA *Data;
    NET_RING *Packets, *Fragments;
    NET_EXTENSION Va;
    BOOLEAN Cancelling;
};

static void KeAcquireSpinLock(void *lock, KIRQL *irql) { (void)lock; *irql=0; }
static void KeReleaseSpinLock(void *lock, KIRQL irql) { (void)lock; (void)irql; }
static void UpdateEapolEvent(SWIFI_STATION_DATA *d) { (void)d; }
static void SwifiDataNotify(SWIFI_DEVICE *d) { (void)d; }
static USHORT Service(SWIFI_STATION_DATA *d) { (void)d; return 0; }
static void Observe(SWIFI_STATION_DATA *d, ULONG bit) { d->PacketPath|=bit; }
static void SwifiReportRx(SWIFI_REPORT *r, unsigned kind) { assert(kind<4); ++r->Counts[kind]; }
static int swifi_filter_accepts(const int *filter, const UCHAR *addr, const UCHAR *frame) {
    (void)filter; return (frame[0]&1) || !memcmp(addr,frame,6);
}
static BOOLEAN IsEapol(const UCHAR *f) { return f[12]==0x88 && f[13]==0x8e; }
static void ReadMetadata(SWIFI_STATION_DATA *d, SWIFI_RX_BUFFER *s, USHORT bytes) {
    (void)d; (void)s; assert(!bytes);
}
static NET_PACKET *NetRingGetPacketAtIndex(NET_RING *r, ULONG i) { assert(i<2); return &r->Packets[i]; }
static NET_FRAGMENT *NetRingGetFragmentAtIndex(NET_RING *r, ULONG i) { assert(i<2); return &r->Fragments[i]; }
static FRAGMENT_VA *NetExtensionGetFragmentVirtualAddress(NET_EXTENSION *e, ULONG i) { assert(i<2); return &e->Slots[i]; }
static ULONG NetRingIncrementIndex(NET_RING *r, ULONG i) { (void)r; return (i+1)%2; }

#include "receive-path-under-test.inc"

static SWIFI_DEVICE device;
static UCHAR memory[POOL*SWIFI_PACKET_SIZE], output[SWIFI_PACKET_SIZE];
static const UCHAR peer[6]={2,1,2,3,4,5}, bridge[6]={2,6,7,8,9,10};

static void receive(unsigned slot, const UCHAR *source, BOOLEAN eapol)
{
    SWIFI_STATION_DATA *d=&device.Data;
    SWIFI_RX_BUFFER *s=&d->Rx[slot];
    if (s->State==SwifiRxFree) RemoveEntryList(&s->Link);
    s->State=SwifiRxPosted;
    UCHAR *f=memory+slot*SWIFI_PACKET_SIZE;
    memset(f,0,SWIFI_PACKET_SIZE);
    memcpy(f,d->Address,6); memcpy(f+6,source,6);
    f[12]=eapol?0x88:8; f[13]=eapol?0x8e:0;
    if (eapol) { f[14]=2; f[15]=3; f[17]=95; f[18]=2; f[20]=0x8a; f[22]=16; f[30]=1; }
    swifi_packet_completion c={.cookie=s,.reason=SWIFI_COMPLETED,.length=eapol?113:64};
    RxComplete(d,&c);
}

static int indicate(void)
{
    NET_RING packets={.EndIndex=1}, fragments={.EndIndex=1};
    fragments.Fragments[0].Capacity=sizeof(output);
    SWIFI_QUEUE q={.Data=&device.Data,.Packets=&packets,.Fragments=&fragments};
    q.Va.Slots[0].VirtualAddress=output;
    memset(output,0,sizeof(output));
    AdvanceRx(&q);
    return packets.BeginIndex==1;
}

static void join(void)
{
    SwifiDataBeginJoin(&device,peer,TRUE,TRUE);
    SwifiDataLink(&device,peer,TRUE,FALSE,FALSE);
}

int main(void)
{
    SWIFI_STATION_DATA *d=&device.Data;
    d->Enabled=TRUE; d->FirstJoin=TRUE;
    d->Hardware=&device.Hw; d->Core.rx_count=POOL; d->RxMemory.Memory=memory;
    d->Address[0]=2; d->Address[5]=42;
    d->EapolExemption[0]=d->EapolExemption[1]=1;
    InitializeListHead(&d->FreeRx); InitializeListHead(&d->ReadyRx);
    for (unsigned i=0;i<POOL;i++) d->Rx[i].State=SwifiRxPosted;
    join();
    receive(0,peer,TRUE);
    assert(indicate()); /* First association already worked in 0.5.5. */

    d->KeyEpoch+=2; d->Pairwise=d->Group=TRUE;
    SwifiDataLink(&device,NULL,FALSE,FALSE,FALSE);
    join();
    /* Slot 1 is still firmware-owned from before the FIRST association.
     * Its posting epochs cannot describe this newly received EAPOL M1. */
    receive(1,peer,TRUE);
    assert(d->Rx[1].State==SwifiRxReady);
    assert(indicate());
    receive(2,bridge,TRUE); /* Bridged authenticator after reassociation. */
    assert(d->Rx[2].State==SwifiRxReady && indicate());

    /* Reject an accepted clear EAPOL that stayed in ReadyRx across a key
     * change. Fixing idle RXPOST lifetime must not remove this check. */
    receive(3,peer,TRUE);
    assert(d->Rx[3].State==SwifiRxReady);
    ++d->KeyEpoch; d->Pairwise=d->Group=TRUE;
    assert(!indicate());

    receive(0,peer,FALSE); /* DHCP/data after reconnect, old pool slot. */
    assert(d->Rx[0].State==SwifiRxReady && d->Rx[0].Protected);
    assert(indicate() && (output[1]&0x40));
    receive(1,peer,FALSE);
    ++d->KeyEpoch;
    assert(!indicate()); /* Ready data may not cross a key replacement. */

    for (unsigned i=0;i<3;i++) {
        SwifiDataLink(&device,NULL,FALSE,FALSE,FALSE);
        join();
        receive(i,bridge,TRUE);
        assert(indicate());
        d->Pairwise=d->Group=TRUE; ++d->KeyEpoch;
    }
    assert(!device.Report.Counts[SwifiRxEtherUncertain]);
    puts("PASS: actual RX/join/indication source, retained RXPOST buffers across reconnects, bridged EAPOL and queued-key boundaries; host only");
    return 0;
}
