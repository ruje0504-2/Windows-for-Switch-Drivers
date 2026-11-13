#!/usr/bin/env python3
"""Exercise actual dump callbacks with synthetic card identities and SDHC I/O.

These are host fault-injection tests, not evidence of a Windows crash dump.
No private card header, device access or installation is required.
"""
from pathlib import Path
import ast
import os
import re
import struct
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/nvtsdhc.c').read_text()
board = (root / 'src/t214_sdmmc.h').read_text()
dump = (root / 'src/t214_crashdump.h').read_text()
tree = ast.parse((root / 'tests/test-t214-data.py').read_text())
extract = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'function')
exec(compile(ast.Module(body=[extract], type_ignores=[]), '<extract-c>', 'exec'))


def target_header(visible):
    """Use the real target filter with public, deliberately synthetic data."""
    tail = bytearray(70)
    struct.pack_into('<I', tail, 0, 0x12345678)
    tail[10] = 0x0c
    struct.pack_into('<II', tail, 14, 2048, 8192)
    tail[26] = 0xee
    struct.pack_into('<II', tail, 30, 1, 2047)
    tail[58] = 7
    struct.pack_into('<II', tail, 62, 32768, 167772160)
    values = {
        'WINARM_FIRST': '32768', 'WINARM_COUNT': '167772160',
        'FAT32_EXTENT': '#define FAT32_FIRST 2048u\n#define FAT32_COUNT 8192u' if visible else '',
        'WINARM_SERIAL': '0x123456789abcdef0', 'LITE_CARD_BYTES': str(256 * 1024**3),
        'WINARM_MBR_SIGNATURE': '0x12345678',
        'DISK_GUID': ','.join(str(i + 1) for i in range(16)),
        'TYPE_AND_GUID': ','.join(str(i + 1) for i in range(32)),
        'MBR_TAIL': ','.join(str(v) for v in tail),
    }
    text = (root / 'winarm/winarm-target.h.in').read_text()
    for key, value in values.items():
        text = text.replace(f'@{key}@', value)
    assert '@' not in text
    return text, tail


prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
typedef uint32_t ULONG, *PULONG; typedef int32_t LONG;
typedef uint16_t USHORT; typedef uint8_t UCHAR, BOOLEAN, *PUCHAR;
typedef USHORT *PUSHORT;
typedef size_t SIZE_T; typedef int NTSTATUS; typedef void *HANDLE;
#define VOID void
#define _In_
#define _In_reads_(n)
#define NT_ASSERT assert
#define TRUE 1
#define FALSE 0
#define MAXULONG UINT32_MAX
#define NT_SUCCESS(s) ((s)>=0)
#define RtlCopyMemory memcpy
#define KeMemoryBarrier() ((void)0)
#define MAX(a,b) ((a)>(b)?(a):(b))
#define min(a,b) ((a)<(b)?(a):(b))
#define NV_T210_LITE_WINARM_WRITE 1
#define NV_T214_SDR104 1
#define NV_PIO_POLL 1
#define NV_PIO_POLL_US 200
#define NV_PIO_POLL_BLOCKS 32
#define SdPortWait(us) KeStallExecutionProcessor(us)
#ifndef NV_T214_CRASHDUMP
#define NV_T214_CRASHDUMP 1
#endif
enum { STATUS_SUCCESS=0, STATUS_INSUFFICIENT_RESOURCES=-1, STATUS_NOT_SUPPORTED=-2,
 STATUS_DEVICE_BUSY=-3, STATUS_IO_TIMEOUT=-4, STATUS_IO_DEVICE_ERROR=-5,
 STATUS_MEDIA_WRITE_PROTECTED=-6, STATUS_DEVICE_POWER_FAILURE=-7,
 STATUS_PENDING=1, STATUS_MORE_PROCESSING_REQUIRED=-8,
 STATUS_DEVICE_CONFIGURATION_ERROR=-9, STATUS_INVALID_PARAMETER=-10 };
enum {SdResetTypeAll,SdResetTypeCmd,SdResetTypeDat,SdhcSpeedModeNormal=0};
enum { SdTransferTypeNone, SdTransferTypeSingleBlock, SdTransferTypeMultiBlock, SdTransferTypeMultiBlockNoStop };
enum { SdCommandClassStandard, SdCommandClassApp };
enum { SdTransferDirectionRead, SdTransferDirectionWrite };
enum { SdTransferMethodPio, SdTransferMethodSgDma };
enum { SdRequestTypeCommandNoTransfer, SdRequestTypeCommandWithTransfer, SdRequestTypeStartTransfer };
enum { SdResponseTypeR1 };
enum { LiteSdxc=1, LiteGpt=2, LitePartition=4, LiteNtfs=8, LiteMbr=16, LiteFatBoot=32,
 LiteWriteFacts=LiteSdxc|LiteMbr|LiteNtfs, LiteFatWriteFacts=LiteSdxc|LiteMbr|LiteFatBoot };
typedef struct { ULONG Index,Class,ResponseType,TransferDirection,TransferMethod,TransferType,Argument,Length;
 USHORT BlockSize,BlockCount; PUCHAR DataBuffer; } SDPORT_COMMAND,*PSDPORT_COMMAND;
typedef struct { ULONG Type,RequiredEvents; NTSTATUS Status; SDPORT_COMMAND Command; } SDPORT_REQUEST,*PSDPORT_REQUEST;
typedef struct {
 LONG TargetFacts; ULONG NextReadLba,IssuedWriteLba; USHORT IssuedWriteCount;
 UCHAR IssuedWriteIndex; BOOLEAN AppPrefixAccepted,CrashdumpMode;
 BOOLEAN T214DmaActive,T214DmaComplete,T214DmaFault,T214CardOn,T214Voltage18,T214Tuned;
 BOOLEAN T214DdrActive,T214DdrFatal,T214DdrRecoverNeeded,T214DdrAttempted;
 ULONG T214CardInfoValid,T214Rca,SpeedMode,T214CalFallbacks;
 PUCHAR T214Bounce,T214DmaBuffer; struct { ULONG LowPart; } T214BouncePa;
 ULONG T214DmaLength,T214DmaBlocks,T214DmaCapacity,T214DmaRequests,T214DmaMaxBlocks,T214PioFallbacks;
 struct { ULONG MaximumBlockCount,MaximumBlockSize,BaseClockFrequencyKhz;
  struct {BOOLEAN ScatterGatherDma,Address64Bit;} Supported; } Capabilities;
 void *OutstandingRequests[1];
 PUCHAR BaseAddress,T214Car,T214Misc,T214Pmc,T214I2c,T214Gpio;
 HANDLE T214Gate,T214GateHandle;
 UCHAR regs[512];
} SDHC_EXTENSION,*PSDHC_EXTENSION;
static LONG InterlockedOr(LONG *p,LONG n){return *p|=n;}
static LONG InterlockedAnd(LONG *p,LONG n){return *p&=n;}
static ULONG raw32(PUCHAR p,ULONG off){ULONG v;memcpy(&v,p+off,4);return v;}
static void put32(PUCHAR p,ULONG off,ULONG v){memcpy(p+off,&v,4);}
static unsigned mode,fifo,sector_reads,active_lba,error_resets,elapsed_us,mmio_writes;
static bool reset_stuck;
static struct {ULONG BlocksRead,BlocksWritten;} NvDiag;
static UCHAR written[16384];
static unsigned written_bytes,ready_blocks;
static UCHAR mbr[512],ntfs[512],maps[5][16384];
static unsigned map_calls,unmap_calls,map_fail,gate_acquires,gate_releases,free_calls,close_calls;
static bool gate_busy;
static ULONG pmic_value;
static PUCHAR checked_host;
static bool gate_fail,reset_fail,source_fail,pll_fail;
static unsigned card_power_writes;
static void KeStallExecutionProcessor(ULONG us){elapsed_us+=us;}
static PUCHAR T214Map(ULONG address,ULONG length){
 static const ULONG addresses[]={0x60006000,0x70000000,0x7000e000,0x7000d000,0x6000d000};
 static const ULONG lengths[]={0x1000,0x4000,0x1000,0x1000,0x1000};
 unsigned i=map_calls++;assert(i<5&&address==addresses[i]&&length==lengths[i]);
 return map_fail==i+1?NULL:maps[i];
}
static void MmUnmapIoSpace(PUCHAR p,ULONG length){assert(p&&length);unmap_calls++;}
static void MmFreeContiguousMemory(PUCHAR p){assert(p);free_calls++;}
static void ZwClose(HANDLE h){assert(h);close_calls++;}
static NTSTATUS TegraI2c5Acquire(HANDLE h){assert(h);gate_acquires++;return gate_busy?STATUS_IO_TIMEOUT:0;}
static void TegraI2c5Release(HANDLE h){assert(h);gate_releases++;}
static NTSTATUS T214PmicMessage(PSDHC_EXTENSION s,BOOLEAN read,ULONG *word,ULONG bytes){
 (void)s;
 if(read)*word=pmic_value;
 else if(bytes==2)pmic_value=(*word>>8)&255;
 else assert(*word==0x27);
 return 0;
}
static void accessible(void){if(checked_host)assert((raw32(maps[0],0x10)&(1u<<14))&&!(raw32(maps[0],4)&(1u<<14)));}
static ULONG T214Read(PUCHAR p,ULONG off){if(p==checked_host)accessible();return raw32(p,off);}
static void T214Write(PUCHAR p,ULONG off,ULONG v){
 mmio_writes++;
 if(p==maps[0]){
  switch(off){
  case 0x300:assert(v==(1u<<14));put32(p,4,raw32(p,4)|v);return;
  case 0x304:assert(v==(1u<<14));if(!reset_fail){put32(p,4,raw32(p,4)&~v);if(checked_host)memset(checked_host,0,512);}return;
  case 0x320:assert(v==(1u<<14));if(!gate_fail)put32(p,0x10,raw32(p,0x10)|v);return;
  case 0x150:
   if(checked_host)assert(raw32(p,4)&(1u<<14)); /* old DMA must be reset before retiming */
   if(source_fail)return;break;
  case 0x5a4:if((v&(1u<<30))&&!pll_fail)v|=1u<<27;break;
  }
 }
 if(p==checked_host)accessible();
 if(p==maps[4]&&off==0x1a0)card_power_writes++;
 if(p==maps[3]&&off==0x8c)v=0; /* PMIC config load completes. */
 if(p!=maps[3]&&off==0x30)v=raw32(p,off)&~v;
 put32(p,off,v);
}
static ULONG SdhcReadRegisterUlong(PSDHC_EXTENSION s,ULONG off){
 if(s->regs==checked_host)accessible();
 if(off==0x20){
  assert(fifo<128);ULONG v=raw32(active_lba?ntfs:mbr,fifo++*4);
  if(fifo==128)put32(s->regs,0x30,raw32(s->regs,0x30)|2|(mode==3?1:0));
  return v;
 }
 return raw32(s->regs,off);
}
static UCHAR SdhcReadRegisterUchar(PSDHC_EXTENSION s,ULONG off){if(s->regs==checked_host)accessible();return s->regs[off];}
static USHORT SdhcReadRegisterUshort(PSDHC_EXTENSION s,ULONG off){USHORT v;if(s->regs==checked_host)accessible();memcpy(&v,s->regs+off,2);return v;}
static void SdhcWriteRegisterUlong(PSDHC_EXTENSION s,ULONG off,ULONG v){if(s->regs==checked_host)accessible();mmio_writes++;put32(s->regs,off,v);}
static void SdhcWriteRegisterUchar(PSDHC_EXTENSION s,ULONG off,UCHAR v){
 if(s->regs==checked_host)accessible();
 mmio_writes++;
 if(off==0x2f){error_resets++;if(!reset_stuck){if(v&1)memset(s->regs,0,0x40);v=0;}}
 s->regs[off]=v;
}
static void SdhcWriteRegisterUshort(PSDHC_EXTENSION s,ULONG off,USHORT v){
 if(s->regs==checked_host)accessible();
 mmio_writes++;
 if(off==0x30)v=SdhcReadRegisterUshort(s,off)&~v;
 memcpy(s->regs+off,&v,2);
 if(off==0x0e){
  assert(v==0x113a);assert(raw32(s->regs,0x38)==0); /* no IRQ signals for private I/O */
  assert(SdhcReadRegisterUshort(s,0x0c)==18); /* never DMA */
  fifo=0;sector_reads++;active_lba=raw32(s->regs,8);
  assert(active_lba==0||active_lba==32768);
  put32(s->regs,0x30,mode==1?0x108000:mode==2?0:mode==3?0x22:0x21);
 }
}
static LONG InterlockedExchange(LONG *p,LONG v){LONG old=*p;*p=v;return old;}
static NTSTATUS T214DdrRecover(PSDHC_EXTENSION s){(void)s;assert(!"dump must not invoke normal DDR recovery");return STATUS_NOT_SUPPORTED;}
static void SdhcReadDataPort(PSDHC_EXTENSION s,PUCHAR buffer,SIZE_T length){
 (void)s;(void)buffer;(void)length;assert(!"unexpected PIO read in write test");
}
static void SdhcWriteRegisterBufferUlong(PSDHC_EXTENSION s,ULONG off,PULONG p,ULONG count){
 assert(off==0x20&&count==1&&ready_blocks&&written_bytes+4<=sizeof(written));
 memcpy(written+written_bytes,p,4);written_bytes+=4;
 if(written_bytes%512==0&&!--ready_blocks)put32(s->regs,0x24,0);
}
static void SdhcWriteRegisterBufferUshort(PSDHC_EXTENSION s,ULONG off,PUSHORT p,ULONG count){
 (void)s;(void)off;(void)p;(void)count;assert(!"unexpected short write");
}
static void SdhcWriteRegisterBufferUchar(PSDHC_EXTENSION s,ULONG off,PUCHAR p,ULONG count){
 (void)s;(void)off;(void)p;(void)count;assert(!"unexpected byte write");
}
'''
# Use the production register constants, avoiding a second SDHCI definition.
header = (root / 'src/nvtsdhc.h').read_text().replace('\\\n', ' ')
prefix += '\n'.join(re.findall(r'^#define[ \t]+SDHC_\w+[ \t]+[^\n]+', header, re.M)) + '\n'
prefix += re.search(r'^#define T214_IO_BIT[^\n]+', board, re.M).group(0) + '\n'

body = ''
for name, result in [('LiteForgetCard', 'void'), ('LiteObserveRead', 'void'), ('LiteBlocksInExtent', 'BOOLEAN'), ('LiteAllowRequest', 'BOOLEAN'),
                     ('SdhcEnableInterrupt', 'void'), ('SdhcDisableInterrupt', 'void'),
                     ('SdhcWriteDataPort', 'void'), ('SdhcStartPioTransfer', 'NTSTATUS')]:
    definition = source[source.index('\n' + name + '(\n'):] if name.startswith('Sdhc') else source
    body += function(definition, name, result)
for name, result in [('T214WaitClear', 'BOOLEAN'), ('T214InheritedClockKhz', 'ULONG'),
                     ('T214Release', 'void'), ('T214Ldo2', 'NTSTATUS'), ('T214ParentClock', 'NTSTATUS'),
                     ('T214CardPower', 'void'), ('T214PadCalibrate', 'NTSTATUS'),
                     ('T214VendorReset', 'void'), ('T214Power', 'NTSTATUS'), ('T214BuildDma', 'BOOLEAN')]:
    definition = board[re.search(r'static (?:VOID|NTSTATUS|BOOLEAN|ULONG) ' + name + r'\([^;]+?\)\s*\{', board).start():]
    body += function(definition, name, result)
body += dump
body += function(source[source.index('\nSdhcResetHost(\n'):], 'SdhcResetHost', 'NTSTATUS')

checks = r'''
static void u64(PUCHAR p,uint64_t v){memcpy(p,&v,8);}
static void slot(SDHC_EXTENSION *s){memset(s,0,sizeof(*s));s->BaseAddress=s->regs;s->CrashdumpMode=1;s->TargetFacts=LiteSdxc;s->NextReadLba=99;
 put32(s->regs,0x34,0x11112222);put32(s->regs,0x38,0);s->regs[0x2e]=7;}
static void restored(SDHC_EXTENSION *s){assert(raw32(s->regs,0x34)==0x11112222&&raw32(s->regs,0x38)==0&&s->NextReadLba==99&&s->regs[0x2e]==7);}
int main(void){
 SDHC_EXTENSION s;
 assert(T214InheritedClockKhz(15)==48000&&T214InheritedClockKhz(3u<<29)==199680);
 assert(T214InheritedClockKhz(7u<<29|3)==(NV_T214_DDR200?399360u:0));
 assert(T214InheritedClockKhz(2u<<29)==0);
 /* Dump preparation cannot reset/retime the live boot disk or acquire a lock. */
 for(map_fail=0;map_fail<=5;map_fail++){
  slot(&s);map_calls=unmap_calls=0;mmio_writes=0;put32(maps[1],0x804,0x2120);
  assert(T214InitializeDump(&s)==(map_fail?STATUS_INSUFFICIENT_RESOURCES:0));
  assert(map_calls==5&&mmio_writes==0&&gate_acquires==0&&!s.T214Bounce&&!s.T214Gate);
  if(!map_fail){assert(s.Capabilities.MaximumBlockCount==32&&s.Capabilities.MaximumBlockSize==512);T214Release(&s);}
  assert(unmap_calls==(map_fail?4:5)&&!s.T214Car&&!s.T214Misc&&!s.T214Pmc&&!s.T214I2c&&!s.T214Gpio);
 }
 slot(&s);map_calls=unmap_calls=0;map_fail=0;put32(maps[1],0x804,0);
 assert(T214InitializeDump(&s)==STATUS_NOT_SUPPORTED&&unmap_calls==5);
 assert(free_calls==0&&close_calls==0);
 /* Prepare while SDR104 is active, then let the live card switch to DDR200
    before takeover. No cached dump-extension DDR flag can identify this. */
 for(unsigned ddr=0;ddr<2;ddr++)for(unsigned loss=0;loss<4;loss++){
  slot(&s);map_calls=0;put32(maps[1],0x804,0x2120);
  put32(maps[0],0x150,3u<<29);assert(T214InitializeDump(&s)==0);
  put32(maps[0],0x150,ddr?0xe0000003:0x60000000);
  put32(maps[0],0x5a4,0x48006804);put32(maps[0],0x10,0x90000002|((loss&1)?0:1u<<14));
  put32(maps[0],4,0x10000004|((loss&2)?1u<<14:0));
  s.regs[0x2c]=7;s.regs[0x2d]=ddr?1:0;s.regs[0x3e]=ddr?0x8c:0x8b;
  s.T214CardOn=1;s.T214Voltage18=1;checked_host=s.regs;card_power_writes=0;
  assert(SdhcResetHost(&s,SdResetTypeAll)==0);
  assert(raw32(maps[0],0x150)==0x60000000&&s.Capabilities.BaseClockFrequencyKhz==199680);
  assert(!(s.regs[0x2c]&4)&&!(s.regs[0x3e]&15)&&s.regs[0x29]==15);
  assert(s.T214CardOn&&!s.T214Voltage18&&!s.T214DdrActive&&card_power_writes==2&&s.TargetFacts==0);
  assert(raw32(maps[0],0x10)==(0x90000002|(1u<<14))&&raw32(maps[0],4)==0x10000004);
#if NV_T214_DDR200
  assert(s.T214DdrAttempted&&!s.T214DdrFatal&&!s.T214DdrRecoverNeeded);
#endif
  checked_host=NULL;T214Release(&s);
 }
 for(unsigned fault=0;fault<4;fault++){
  slot(&s);map_calls=0;put32(maps[1],0x804,0x2120);assert(T214InitializeDump(&s)==0);
  put32(maps[0],0x150,0xe0000003);put32(maps[0],0x5a4,fault==3?0x40006804:0x48006804);
  put32(maps[0],0x10,0);put32(maps[0],4,1u<<14);
  gate_fail=fault==0;reset_fail=fault==1;source_fail=fault==2;pll_fail=fault==3;
  checked_host=s.regs;card_power_writes=0;elapsed_us=0;
  assert(SdhcResetHost(&s,SdResetTypeAll)<0&&card_power_writes==0&&elapsed_us<=1004);
  checked_host=NULL;T214Release(&s);gate_fail=reset_fail=source_fail=pll_fail=false;
 }
 /* A stopped normal-mode owner may hold the I2C gate indefinitely. */
 slot(&s);s.T214I2c=maps[3];s.T214Gate=&s;gate_busy=true;
 put32(maps[3],0,0x1234);put32(maps[3],0x64,0x5678);
 assert(T214Ldo2(&s,3300,1)==0&&gate_acquires==0&&gate_releases==0);
 assert(raw32(maps[3],0)==(0x1234&~0x200)&&raw32(maps[3],0x64)==0x5678);
 s.CrashdumpMode=0;assert(T214Ldo2(&s,3300,1)==STATUS_IO_TIMEOUT&&gate_acquires==1&&gate_releases==0);
 gate_busy=false;assert(T214Ldo2(&s,3300,1)==0&&gate_acquires==2&&gate_releases==1);
 s.CrashdumpMode=1;put32(maps[3],0x1c,0x100);elapsed_us=0;
 assert(T214Ldo2(&s,3300,1)==STATUS_IO_TIMEOUT&&elapsed_us==20000);
 assert(raw32(maps[3],0x64)==0x5678&&gate_acquires==2&&gate_releases==1);
 /* Polling must retain all status enables while keeping IRQ delivery off. */
 slot(&s);put32(s.regs,0x34,0);put32(s.regs,0x38,0xffffffff);
 SdhcEnableInterrupt(&s,1);SdhcEnableInterrupt(&s,32);
 assert(SdhcReadRegisterUshort(&s,0x34)==33&&raw32(s.regs,0x38)==0);
 SdhcDisableInterrupt(&s,32);assert(SdhcReadRegisterUshort(&s,0x34)==1&&raw32(s.regs,0x38)==0);
 assert(SdhcReadRegisterUshort(&s,0x36)==0xffff);
 SdhcDisableInterrupt(&s,1);assert(raw32(s.regs,0x34)==0&&raw32(s.regs,0x38)==0);
 s.CrashdumpMode=0;SdhcEnableInterrupt(&s,33);SdhcDisableInterrupt(&s,32);
 assert(SdhcReadRegisterUshort(&s,0x34)==1&&SdhcReadRegisterUshort(&s,0x38)==1);
 /* Real target predicates, including the filtered MBR, gate dump writes. */
 memcpy(mbr+440,physical_tail,70);mbr[510]=0x55;mbr[511]=0xaa;
 memcpy(ntfs+3,"NTFS    ",8);ntfs[12]=2;ntfs[13]=8;ntfs[510]=0x55;ntfs[511]=0xaa;
 put32(ntfs,28,WINARM_FIRST);u64(ntfs+40,WINARM_COUNT-1);u64(ntfs+72,WINARM_SERIAL);
 SDPORT_REQUEST r={0};r.Type=SdRequestTypeCommandWithTransfer;
 _Alignas(16) UCHAR payload[16384];
 for(unsigned i=0;i<sizeof(payload);i++)payload[i]=(UCHAR)(i*37+i/512);
 r.Command.DataBuffer=payload;r.Command.Length=sizeof(payload);
 r.Command.Index=25;r.Command.BlockSize=512;r.Command.BlockCount=32;r.Command.Argument=WINARM_FIRST+32;
 r.Command.TransferDirection=SdTransferDirectionWrite;r.Command.TransferMethod=SdTransferMethodPio;
 r.Command.TransferType=SdTransferTypeMultiBlock;
 slot(&s);assert(T214PrepareDumpWrite(&s,&r)==0&&sector_reads==2);
 restored(&s);assert(s.TargetFacts==LiteWriteFacts&&LiteAllowRequest(&s,&r)==NV_T214_CRASHDUMP);
 assert(T214PrepareDumpWrite(&s,&r)==0&&sector_reads==2); /* no re-read on each write */
 assert(!memcmp(mbr+440,physical_tail,70)); /* physical MBR not rewritten */
 _Alignas(16) UCHAR bounce[20480];s.T214Bounce=bounce;s.T214DmaCapacity=16384;
 unsigned before=mmio_writes;assert(!T214BuildDma(&s,&r)&&mmio_writes==before);
 /* Exercise the real PIO write callback with a stall between two batches.
    It must preserve the buffer/count and wait for transfer completion. */
 ready_blocks=8;put32(s.regs,0x24,SDHC_PS_BUFFER_WRITE_ENABLE);elapsed_us=0;
 assert(SdhcStartPioTransfer(&s,&r)==STATUS_PENDING&&r.Command.BlockCount==24);
 assert(r.Command.DataBuffer==payload+4096&&r.Status==STATUS_MORE_PROCESSING_REQUIRED);
 assert(r.RequiredEvents==SDHC_IS_BUFFER_WRITE_READY&&elapsed_us==NV_PIO_POLL_US);
 ready_blocks=24;put32(s.regs,0x24,SDHC_PS_BUFFER_WRITE_ENABLE);r.RequiredEvents=0;
 assert(SdhcStartPioTransfer(&s,&r)==STATUS_PENDING&&r.Command.BlockCount==0);
 assert(r.RequiredEvents==SDHC_IS_TRANSFER_COMPLETE&&r.Status==STATUS_SUCCESS);
 assert(written_bytes==sizeof(payload)&&!memcmp(written,payload,sizeof(payload))&&NvDiag.BlocksWritten==32);
 r.Command.BlockCount=32;r.Command.DataBuffer=payload;
 r.Type=SdRequestTypeStartTransfer;s.IssuedWriteLba=r.Command.Argument;s.IssuedWriteIndex=25;s.IssuedWriteCount=32;
 assert(LiteAllowRequest(&s,&r)==NV_T214_CRASHDUMP);s.IssuedWriteLba++;
 assert(!LiteAllowRequest(&s,&r));r.Type=SdRequestTypeCommandWithTransfer;
 r.Command.Argument=WINARM_FIRST+WINARM_COUNT-31;assert(!LiteAllowRequest(&s,&r));
 r.Command.Argument=UINT32_MAX-4;assert(!LiteAllowRequest(&s,&r));
 r.Command.Argument=0;assert(!LiteAllowRequest(&s,&r));
#ifdef FAT32_FIRST
 s.TargetFacts|=LiteFatBoot;r.Command.Argument=FAT32_FIRST+32;assert(!LiteAllowRequest(&s,&r));
 s.CrashdumpMode=0;assert(LiteAllowRequest(&s,&r));s.CrashdumpMode=1;
#endif
 r.Command.Argument=WINARM_FIRST;r.Command.Index=38;assert(!LiteAllowRequest(&s,&r));r.Command.Index=25;
 slot(&s);ntfs[72]^=1;assert(T214PrepareDumpWrite(&s,&r)==STATUS_MEDIA_WRITE_PROTECTED);ntfs[72]^=1;
 assert(!(s.TargetFacts&LiteWriteFacts));restored(&s);
 slot(&s);mbr[440]^=1;assert(T214PrepareDumpWrite(&s,&r)==STATUS_MEDIA_WRITE_PROTECTED);mbr[440]^=1;
 assert(!(s.TargetFacts&LiteWriteFacts));restored(&s);
 for(mode=1;mode<=2;mode++){
  slot(&s);elapsed_us=0;
  assert(T214PrepareDumpWrite(&s,&r)==(mode==1?STATUS_IO_DEVICE_ERROR:STATUS_IO_TIMEOUT));
  assert(!(s.TargetFacts&LiteWriteFacts)&&elapsed_us<=1001000);restored(&s);
 }
 mode=3;slot(&s);assert(T214PrepareDumpWrite(&s,&r)==0);restored(&s); /* command completion arrives last */
 mode=0;slot(&s);s.AppPrefixAccepted=1;before=sector_reads;
 assert(T214PrepareDumpWrite(&s,&r)==STATUS_MEDIA_WRITE_PROTECTED&&sector_reads==before);
 slot(&s);s.TargetFacts=0;assert(T214PrepareDumpWrite(&s,&r)==STATUS_MEDIA_WRITE_PROTECTED&&sector_reads==before);
 slot(&s);s.OutstandingRequests[0]=&r;assert(T214PrepareDumpWrite(&s,&r)==STATUS_DEVICE_BUSY&&sector_reads==before);
 slot(&s);put32(s.regs,0x24,3);elapsed_us=0;
 assert(T214PrepareDumpWrite(&s,&r)==STATUS_DEVICE_BUSY&&sector_reads==before&&elapsed_us==100000);restored(&s);
 slot(&s);mode=1;reset_stuck=1;elapsed_us=0;
 assert(T214PrepareDumpWrite(&s,&r)==STATUS_IO_TIMEOUT&&elapsed_us==1000);restored(&s);
 puts("PASS: dump preparation, I2C gate, polled events, identity, completion ordering, timeout/CRC/reset, PIO data and WINARM bounds");
}
'''

with tempfile.TemporaryDirectory(prefix='lite-crashdump-') as temp:
    path = Path(temp)
    for visible in (False, True):
        target, tail = target_header(visible)
        constants = '\nstatic const UCHAR physical_tail[70]={' + ','.join(str(v) for v in tail) + '};\n'
        (path / 'test.c').write_text(prefix + target + body + constants + checks)
        for enabled, ddr in ((1, 0), (1, 1)):
            subprocess.run(['clang', '-std=c11', '-Wall', '-Werror', '-Wno-unused-function',
                            '-fsanitize=address,undefined', '-g', f'-DNV_T214_CRASHDUMP={enabled}',
                            f'-DNV_T214_DDR200={ddr}',
                            str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True)
        # Existing DMA/filter/power tests also run against both partition views.
        (path / 'winarm-target.h').write_text(target)
        subprocess.run(['python3', str(root / 'tests/test-t214-data.py'), str(path / 'winarm-target.h')], check=True)
        # Optional cross-build harness can consume these public fixtures only.
        if os.environ.get('SD_DUMP_FIXTURE_DIR'):
            out = Path(os.environ['SD_DUMP_FIXTURE_DIR']) / ('visible' if visible else 'hidden')
            out.mkdir(parents=True, exist_ok=True)
            (out / 'winarm-target.h').write_text(target)
