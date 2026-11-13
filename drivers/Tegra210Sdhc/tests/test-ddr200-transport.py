#!/usr/bin/env python3
"""Run the actual private DDR200 transaction with an SDHCI/card fault model.

The model checks sequencing and failure handling, not electrical timing.
"""
from pathlib import Path
import subprocess
import tempfile
import resource

root = Path(__file__).resolve().parents[1]
code = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
typedef uint32_t ULONG,*PULONG;typedef uint16_t USHORT;
typedef uint8_t UCHAR,*PUCHAR;typedef bool BOOLEAN;typedef int NTSTATUS;
#define VOID void
#define TRUE true
#define FALSE false
#define MAXULONG UINT32_MAX
#define NT_SUCCESS(s) ((s)>=0)
#define RtlCopyMemory memcpy
#define InterlockedExchange(p,v) (*(p)=(v))
enum { STATUS_SUCCESS=0,STATUS_IO_TIMEOUT=-1,STATUS_IO_DEVICE_ERROR=-2,
 STATUS_INVALID_PARAMETER=-3,STATUS_DEVICE_BUSY=-4,STATUS_DEVICE_PROTOCOL_ERROR=-5,
 STATUS_DEVICE_CONFIGURATION_ERROR=-6,STATUS_NOT_SUPPORTED=-7,
 STATUS_INVALID_DEVICE_STATE=-8,STATUS_WRONG_VOLUME=-9,STATUS_DATA_ERROR=-10 };
enum {SDHC_BLOCK_SIZE=4,SDHC_BLOCK_COUNT=6,SDHC_ARGUMENT=8,SDHC_TRANSFER_MODE=12,
 SDHC_COMMAND=14,SDHC_RESPONSE=16,SDHC_DATA_PORT=32,SDHC_PRESENT_STATE=36,
 SDHC_CLOCK_CONTROL=44,SDHC_TIMEOUT_CONTROL=46,SDHC_RESET=47,
 SDHC_INTERRUPT_STATUS=48,SDHC_HOST_CONTROL2=62};
#define SDHC_RESET_CMD 2
#define SDHC_RESET_DAT 4
#define SDHC_CC_CLOCK_STABLE 2
#define SDHC_CC_CLOCK_ENABLE 4
#define SDHC_IS_CMD_COMPLETE 1
#define SDHC_IS_TRANSFER_COMPLETE 2
#define SDHC_IS_BUFFER_READ_READY 32
#define SDHC_IS_ERROR_INTERRUPT 0x8000
#define SDHC_TM_TRANSFER_READ 16
#define SDHC_HC2_EXECUTE_TUNING 64
#define SDHC_HC2_SELECT_SAMPLING_CLOCK 128
#define WINARM_FIRST 1000
#define LiteWriteFacts 25
#if TEST_VISIBLE
#define FAT32_FIRST 2000
#define LiteFatBoot 32
#endif
enum {SdResetTypeAll,SdBusVoltage33,SdBusWidth1Bit=1,SdBusWidth4Bit=4,
 SdhcSpeedModeNormal=0,SdhcSpeedModeSDR104=7};
typedef struct {
 PUCHAR BaseAddress,T214Car;BOOLEAN T214DmaActive,T214Voltage18,T214Tuned;
 void *OutstandingRequests[1];
 struct {ULONG BaseClockFrequencyKhz;} Capabilities;
 UCHAR T214Scr[8],T214SwitchStatus[64],T214Cid[16];
 ULONG T214CardInfoValid,T214Rca,T214Tap;
 BOOLEAN T214DdrAttempted,T214DdrActive,T214DdrFatal,T214DdrRecoverNeeded;
 ULONG T214DdrStatus,T214DdrRecoveryStatus,T214DdrFallbacks;
 ULONG T214DdrWindow,T214DdrTap,T214DdrClockKhz,TargetFacts,SpeedMode;
 ULONG T214DdrCapability,T214DdrTuningSamples,T214DdrTuningTimeouts,T214DdrTuningControl2;
 ULONG T214DdrPassMap[4],T214DdrReadyMap[4];
 ULONG T214DdrFirstTimeoutTap,T214DdrTuningErrors;
 ULONG T214DdrTuningControl0,T214DdrTuningControl1,T214DdrSetupControl2;
 ULONG T214DdrTuningClock,T214DdrTuningSource,T214DdrTuningVendorClock;
} SDHC_EXTENSION,*PSDHC_EXTENSION;
static struct {ULONG ClockKhzRequested,ClockKhzActual,CarSdmmc1After,RegClockControl;} NvDiag;
static UCHAR regs[512],car[4096],fifo[512];
static ULONG cid[4],card_rca,card_mode,command_count,power_cycles,read_count;
static ULONG last_command,last_argument,read_offset,read_size,commands[2048],clock_us;
static unsigned good_first,good_last;
static bool timeout_tuning,corrupt_ddr,lost_vendor_reply,power_fails,wrong_cid,changed_rca;
static bool clock_fails,cmd_timeout,app_prefix,reset_stuck;
static bool early_tuning_end;
static bool tuning_armed,tuning_flushed,tuning_reset,tuning_pending;
static bool isolated_taps,last_tap_timeout;
static unsigned tuning_tap,tuning_committed_at,tuning_due;
static ULONG raw32(PUCHAR p,ULONG off){ULONG v;memcpy(&v,p+off,4);return v;}
static void put32(PUCHAR p,ULONG off,ULONG v){memcpy(p+off,&v,4);}
static USHORT raw16(PUCHAR p,ULONG off){USHORT v;memcpy(&v,p+off,2);return v;}
static void put16(PUCHAR p,ULONG off,USHORT v){memcpy(p+off,&v,2);}
static void sector(PUCHAR p,ULONG lba){
 for(unsigned i=0;i<512;i++)p[i]=(UCHAR)(i*37+lba);
 p[0]=lba==0?0x5a:lba==WINARM_FIRST?0xa5:0xf1;p[510]=0x55;p[511]=0xaa;
}
static int LiteBytesEqual(const UCHAR *a,const UCHAR *b,unsigned n){return !memcmp(a,b,n);}
static int LiteSelectWindowsPartition(UCHAR *p){return p[0]==0x5a&&p[510]==0x55&&p[511]==0xaa;}
static int LiteIsBootSector(const UCHAR *p){return p[0]==0xa5&&p[510]==0x55&&p[511]==0xaa;}
#if TEST_VISIBLE
static int LiteIsFatBootSector(const UCHAR *p){return p[0]==0xf1&&p[510]==0x55&&p[511]==0xaa;}
#endif
static void model_command(USHORT value){
 ULONG index=value>>8,arg=raw32(regs,8);last_command=index;last_argument=arg;
 assert(command_count<2048);commands[command_count++]=index;
 read_offset=0;read_size=raw16(regs,4);memset(fifo,0,sizeof(fifo));
 put32(regs,0x30,1|(read_size?32:2));put32(regs,0x10,0x900);
 if(cmd_timeout){put32(regs,0x30,0);return;}
  if(index==19){
   assert(!(raw16(regs,0x2c)&4));
   assert((raw16(regs,0x3e)&0x4f)==0x4c);
   assert(raw16(regs,4)==64&&raw16(regs,6)==1&&raw16(regs,12)==16&&arg==0);
   assert((raw32(regs,0x1c0)&0x2ffc0)==0x24040&&raw32(regs,0x1c4)==0);
   tuning_tap=(raw32(regs,0x100)>>16)&255;
   tuning_armed=true;tuning_flushed=tuning_reset=tuning_pending=false;
   put32(regs,0x30,0);return;
  }
 if(index==0){card_mode=0;app_prefix=false;}
 else if(index==2){for(unsigned i=0;i<4;i++)put32(regs,0x10+i*4,cid[i]^(wrong_cid&&i==0?1:0));}
 else if(index==3)put32(regs,0x10,changed_rca?card_rca+0x10000:card_rca);
 else if(index==8)put32(regs,0x10,0x1aa);
 else if(index==55){put32(regs,0x10,0x920);app_prefix=true;}
 else if(index==41){assert(app_prefix);app_prefix=false;put32(regs,0x10,0xc1ff8000);}
 else if(index==6&&!read_size){assert(app_prefix&&arg==2);app_prefix=false;}
 else if(index==6){
  assert(read_size==64);
  unsigned access=arg&15,system=(arg>>4)&15;
  if(arg&0x80000000u){
   if(access!=15)card_mode=(card_mode&0xf0)|access;
   if(system!=15)card_mode=(card_mode&15)|(system<<4);
  }
  fifo[10]=0x40;fifo[13]=8;fifo[16]=(UCHAR)card_mode;
  if(lost_vendor_reply&&system==14){put32(regs,0x30,0);lost_vendor_reply=false;}
 }else if(index==17){
  assert(read_size==512);read_count++;sector(fifo,arg);
  if(corrupt_ddr&&(card_mode>>4)==14)fifo[23]^=1;
 }
}
/* A same-controller read commits posted writes. Reset alone cannot create
 * tuning data: the card clock must resume and a response must arrive later.
 * This models the ordering contract, not analog margins or a card's DLL. */
static void model_read(void){
 if(tuning_armed&&!tuning_flushed){tuning_flushed=true;tuning_committed_at=clock_us;}
 if(tuning_pending&&(raw16(regs,0x2c)&4)&&clock_us>=tuning_due){
  USHORT hc=raw16(regs,0x3e);
  bool good=isolated_taps?!(tuning_tap&1):(tuning_tap>=good_first&&tuning_tap<=good_last);
  if(good)hc|=128;else hc&=~128u;
  if(tuning_tap==127||early_tuning_end)hc&=~64u;
  put16(regs,0x3e,hc);
  put32(regs,0x30,(timeout_tuning||(last_tap_timeout&&tuning_tap==127))?0:32);
  tuning_armed=tuning_pending=false;
 }
}
static ULONG T214Read(PUCHAR b,ULONG off){
 if(b==regs)model_read();
 if(b==regs&&off==SDHC_DATA_PORT){
  assert(read_offset+4<=read_size);ULONG v=raw32(fifo,read_offset);read_offset+=4;
  if(read_offset==read_size)put32(regs,0x30,raw32(regs,0x30)|2);
  return v;
 }
 return raw32(b,off);
}
static void T214Write(PUCHAR b,ULONG off,ULONG v){
 if(b==regs&&off==0x100){
  assert(!(raw16(regs,0x2c)&4));
  assert(!(raw32(regs,0x1c0)&0x20000));
 }
 if(b==regs&&off==0x30)put32(b,off,raw32(b,off)&~v);else put32(b,off,v);
}
static USHORT SdhcReadRegisterUshort(PSDHC_EXTENSION s,ULONG off){model_read();return raw16(s->BaseAddress,off);}
static UCHAR SdhcReadRegisterUchar(PSDHC_EXTENSION s,ULONG off){model_read();return s->BaseAddress[off];}
static void SdhcWriteRegisterUlong(PSDHC_EXTENSION s,ULONG off,ULONG v){T214Write(s->BaseAddress,off,v);}
static void SdhcWriteRegisterUshort(PSDHC_EXTENSION s,ULONG off,USHORT v){
 if(off==0x30)put16(s->BaseAddress,off,raw16(s->BaseAddress,off)&~v);
 else {
  if(off==0x2c&&(v&1)&&!clock_fails)v|=2;
  put16(s->BaseAddress,off,v);
  if(off==SDHC_COMMAND)model_command(v);
   if(off==SDHC_CLOCK_CONTROL&&(v&4)&&tuning_armed&&tuning_reset){
    tuning_pending=true;tuning_due=clock_us+5;
   }
 }
}
static void SdhcWriteRegisterUchar(PSDHC_EXTENSION s,ULONG off,UCHAR v){
 s->BaseAddress[off]=v;
 if(off==SDHC_RESET&&!reset_stuck){
  s->BaseAddress[off]=0;
  if(tuning_armed){
   assert(tuning_flushed&&clock_us-tuning_committed_at>=1);
   assert(!(raw16(regs,0x2c)&4));
   tuning_reset=true;put32(regs,0x30,0);
  }
 }
}
static void KeStallExecutionProcessor(ULONG us){clock_us+=us;}
static void SdPortWait(ULONG us){clock_us+=us;}
static BOOLEAN T214WaitClear(PUCHAR b,ULONG off,ULONG mask,ULONG us){(void)us;return !(raw32(b,off)&mask);}
static NTSTATUS SdhcConvertErrorToStatus(USHORT e){(void)e;return STATUS_IO_DEVICE_ERROR;}
static NTSTATUS T214Power(PSDHC_EXTENSION s,BOOLEAN on){
 assert(on);power_cycles++;if(power_fails)return STATUS_IO_DEVICE_ERROR;
 s->T214DdrActive=false;s->T214Voltage18=false;s->T214CardInfoValid=s->T214Rca=s->TargetFacts=0;
 s->SpeedMode=0;card_mode=0;clock_fails=false;memset(regs,0,sizeof(regs));return 0;
}
static NTSTATUS SdhcResetHost(PSDHC_EXTENSION s,int type){(void)type;assert(!s->T214DdrActive);return 0;}
static NTSTATUS T214SetBusVoltage(PSDHC_EXTENSION s,int v){(void)s;assert(v==SdBusVoltage33);return 0;}
static NTSTATUS SdhcSetBusWidth(PSDHC_EXTENSION s,int w){(void)s;assert(w==1||w==4);return 0;}
static NTSTATUS SdhcSetClock(PSDHC_EXTENSION s,ULONG rate){assert(!s->T214DdrActive);NvDiag.ClockKhzActual=rate;return 0;}
static NTSTATUS T214Signaling(PSDHC_EXTENSION s,BOOLEAN on){assert(on&&last_command==11);s->T214Voltage18=true;put16(regs,0x3e,8);return 0;}
static NTSTATUS T214Tune(PSDHC_EXTENSION s){assert(card_mode==3);s->T214Tuned=true;return 0;}
'''
code += '#include "' + str(root / 'src/t214_ddr200.h') + '"\n'
code += r'''
static void init(SDHC_EXTENSION *s){
 memset(s,0,sizeof(*s));memset(regs,0,sizeof(regs));memset(car,0,sizeof(car));
 s->BaseAddress=regs;s->T214Car=car;s->T214Voltage18=true;s->SpeedMode=SdhcSpeedModeSDR104;
 s->T214CardInfoValid=7;s->T214Rca=card_rca=0x12340000;s->TargetFacts=LiteWriteFacts;
 s->T214Scr[0]=2;s->T214Scr[1]=4;s->T214Scr[2]=0x80;s->T214Scr[3]=0x80;
 s->T214SwitchStatus[10]=0x40;s->T214SwitchStatus[13]=8;s->T214SwitchStatus[16]=3;
 for(unsigned i=0;i<4;i++)cid[i]=i+1;memcpy(s->T214Cid,cid,16);
 put32(car,0x5a4,0x48006804);put32(car,0x150,3u<<29);
 put16(regs,0x2c,7);put16(regs,0x3e,0x8b);put32(regs,0x34,0x12345678);put32(regs,0x38,0x87654321);
 card_mode=3;command_count=power_cycles=read_count=last_command=clock_us=0;
 good_first=32;good_last=95;
 timeout_tuning=corrupt_ddr=lost_vendor_reply=power_fails=wrong_cid=changed_rca=false;
 clock_fails=cmd_timeout=app_prefix=reset_stuck=early_tuning_end=false;
 tuning_armed=tuning_flushed=tuning_reset=tuning_pending=false;
 isolated_taps=last_tap_timeout=false;
}
int main(void){
 SDHC_EXTENSION s;ULONG data[128];
 unsigned expected_facts=LiteWriteFacts;
#ifdef FAT32_FIRST
 expected_facts|=LiteFatBoot;
#endif
 init(&s);s.T214Scr[3]=0;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&command_count==0&&power_cycles==0);
 assert(s.T214DdrCapability==1&&!s.T214DdrTuningSamples);
 init(&s);s.T214CardInfoValid=0;
 assert(T214DdrTry(&s)==0&&!s.T214DdrAttempted&&command_count==0);
 assert(s.T214DdrCapability==0);
 init(&s);assert(T214DdrTry(&s)==0&&s.T214DdrActive&&card_mode==0xe0);
 assert(s.T214DdrWindow==64&&s.T214DdrTap==63&&NvDiag.ClockKhzActual==199680);
 assert(s.T214DdrCapability==2&&s.T214DdrTuningSamples==128&&!s.T214DdrTuningTimeouts);
 assert(s.T214DdrPassMap[0]==0&&s.T214DdrPassMap[1]==UINT32_MAX&&
        s.T214DdrPassMap[2]==UINT32_MAX&&s.T214DdrPassMap[3]==0);
 for(unsigned i=0;i<4;i++)assert(s.T214DdrReadyMap[i]==UINT32_MAX);
 assert(s.T214DdrFirstTimeoutTap==UINT32_MAX&&!s.T214DdrTuningErrors);
 assert(s.T214DdrTuningControl0==0x24040&&s.T214DdrTuningControl1==0&&s.T214DdrSetupControl2==0x4c);
 assert(s.T214DdrTuningClock==0x107&&s.T214DdrTuningSource==0xe0000003);
 assert(read_count==5&&power_cycles==0&&raw32(car,0x150)==0xe0000003);
 assert((raw16(regs,0x3e)&7)==4&&s.T214Tuned);
 assert(T214DdrClock(&s,0)==0&&NvDiag.ClockKhzActual==0&&!(raw16(regs,0x2c)&5));
 assert(T214DdrClock(&s,200000)==0&&NvDiag.ClockKhzActual==199680);
 assert(T214DdrClock(&s,208000)==0&&NvDiag.ClockKhzRequested==200000&&NvDiag.ClockKhzActual==199680);
 /* Re-tuning stays in DDR, with no extra vendor switch or power cycle. */
 assert(T214DdrTry(&s)==0&&s.T214DdrActive&&power_cycles==0);
 good_first=40;good_last=46;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&!s.T214DdrFatal&&power_cycles==1);
 assert(s.T214DdrWindow==7&&s.T214DdrFallbacks==1&&card_mode==3&&s.TargetFacts==expected_facts);
 assert(s.T214DdrCapability==2&&s.T214CardInfoValid==4&&s.T214DdrTuningSamples==128);
 /* Reset-all loses the SDPORT-owned event masks. Recovery must restore both
  * masks before the next interrupt-driven disk request can complete. */
 assert(raw32(regs,0x34)==0x12345678&&raw32(regs,0x38)==0x87654321);
 unsigned before=command_count;assert(T214DdrTry(&s)==0&&command_count==before);
 init(&s);good_last=38;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&s.T214DdrWindow==7&&power_cycles==1);
 init(&s);good_first=good_last=0;early_tuning_end=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&s.T214DdrWindow==1&&power_cycles==1);
 assert(s.T214DdrCapability==2&&s.T214DdrTuningSamples==1&&s.T214DdrTuningTimeouts==0);
 assert(!(s.T214DdrTuningControl2&64));
 /* The real 128/1/1/0x0c aggregate is ambiguous: 64 isolated successful
  * samples have the same longest run as a single successful sample. */
 init(&s);isolated_taps=last_tap_timeout=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&s.T214DdrWindow==1);
 assert(s.T214DdrTuningSamples==128&&s.T214DdrTuningTimeouts==1&&s.T214DdrTuningControl2==12);
 assert(s.T214DdrFirstTimeoutTap==127&&s.T214CardInfoValid==4);
 for(unsigned i=0;i<4;i++)assert(s.T214DdrPassMap[i]==0x55555555);
 assert(s.T214DdrReadyMap[3]==0x7fffffff&&s.T214DdrTuningSource==0xe0000003);
 init(&s);lost_vendor_reply=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&card_mode==3&&power_cycles==1);
 init(&s);corrupt_ddr=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&(NTSTATUS)s.T214DdrStatus==STATUS_DATA_ERROR);
 init(&s);timeout_tuning=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&clock_us<2000000);
 init(&s);good_last=38;changed_rca=true;
 assert(T214DdrTry(&s)==STATUS_DEVICE_PROTOCOL_ERROR&&s.T214DdrFatal&&s.TargetFacts==0);
 assert(raw32(regs,0x34)==0x12345678&&raw32(regs,0x38)==0x87654321);
 /* Only a new native SDPORT enumeration may repair a changed RCA. */
 assert(!s.T214DdrRecoverNeeded);
 card_mode=3;s.T214CardInfoValid=4;s.T214Rca=0x43210000;
 assert(T214DdrTry(&s)==0&&!s.T214DdrFatal&&!s.T214DdrActive&&s.TargetFacts==0);
 init(&s);good_last=38;wrong_cid=true;
 assert(T214DdrTry(&s)==STATUS_WRONG_VOLUME&&s.T214DdrFatal&&s.TargetFacts==0);
 init(&s);good_last=38;power_fails=true;
 assert(T214DdrTry(&s)==STATUS_IO_DEVICE_ERROR&&s.T214DdrFatal);
 assert(raw32(regs,0x34)==0x12345678&&raw32(regs,0x38)==0x87654321);
 init(&s);clock_fails=true;
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&power_cycles==1);
 /* Restoring zero masks matters too: a polling/crash-free caller must not
  * acquire interrupts it never requested. Successful recovery clears private
  * completion flags before any original signal mask is restored. */
 init(&s);good_last=38;put32(regs,0x34,0);put32(regs,0x38,0);
 assert(T214DdrTry(&s)==0&&!s.T214DdrActive&&!s.T214DdrFatal);
 assert(raw32(regs,0x34)==0&&raw32(regs,0x38)==0&&raw32(regs,0x30)==0);
 init(&s);s.OutstandingRequests[0]=&s;
 assert(T214DdrTry(&s)==STATUS_DEVICE_BUSY&&command_count==0);
 init(&s);
 assert(T214DdrCommand(&s,24,WINARM_FIRST,0x1a,data,sizeof(data),NULL)==STATUS_INVALID_PARAMETER);
 assert(T214DdrCommand(&s,17,WINARM_FIRST,0x1a,data,516,NULL)==STATUS_INVALID_PARAMETER);
 assert(command_count==0);
 cmd_timeout=true;
 assert(T214DdrCommand(&s,13,card_rca,0x1a,NULL,0,NULL)==STATUS_IO_TIMEOUT&&clock_us==50000);
 assert(raw32(regs,0x34)==0x12345678&&raw32(regs,0x38)==0x87654321);
 puts("PASS: DDR posted-write/reset/clock ordering, delayed tuning results, retained sample maps, readback mismatch, narrow-window recovery, RCA/CID checks, event masks, no private data writes");
}
'''
with tempfile.TemporaryDirectory(prefix='lite-ddr200-transport-') as temp:
    path = Path(temp)
    (path / 'test.c').write_text(code)
    for visible in (0, 1):
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        f'-DTEST_VISIBLE={visible}', '-fsanitize=address,undefined',
                        '-g', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], check=True)
    # Prove that the model rejects missing hardware actions that the previous
    # reset-generated result model accepted. Only temporary copies are edited.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    transport = root / 'src/t214_ddr200.h'
    original = transport.read_text()
    mutations = {
        'missing-clock-enable': (
            '        SdhcWriteRegisterUshort(s, SDHC_CLOCK_CONTROL, clock | SDHC_CC_CLOCK_ENABLE);\n'
            '        (VOID)SdhcReadRegisterUshort(s, SDHC_CLOCK_CONTROL);',
            '        /* mutation: keep the card clock gated */\n'
            '        (VOID)SdhcReadRegisterUshort(s, SDHC_CLOCK_CONTROL);'),
        'missing-command-commit': (
            '        (VOID)SdhcReadRegisterUshort(s, SDHC_CLOCK_CONTROL);\n        KeStallExecutionProcessor(1);\n        if (!T214DdrResetLines(s))',
            '        KeStallExecutionProcessor(1);\n        if (!T214DdrResetLines(s))'),
        'missing-tuning-setup': (
            '    T214Write(b, 0x1c0, (T214Read(b, 0x1c0) & ~0xffc0u) |\n'
            '               (2u << 13) | (1u << 6) | (1u << 17));',
            '    /* mutation: omit tuning iteration/multiplier setup */'),
    }
    for name, (before, after) in mutations.items():
        assert original.count(before) == 1, name
        altered = path / (name + '.h')
        altered.write_text(original.replace(before, after))
        (path / 'test.c').write_text(code.replace(str(transport), str(altered)))
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-DTEST_VISIBLE=0', '-I' + str(root / 'src'),
                        '-fsanitize=address,undefined', '-g', str(path / 'test.c'),
                        '-o', str(path / 'test')], check=True)
        result = subprocess.run([str(path / 'test')], capture_output=True, text=True)
        assert result.returncode != 0 and 'Assertion' in result.stderr and 'failed' in result.stderr, (name, result.stderr)
        print('PASS: model rejects ' + name)
