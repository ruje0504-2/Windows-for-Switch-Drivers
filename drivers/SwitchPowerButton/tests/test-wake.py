#!/usr/bin/env python3
"""Exercise the actual PMIC wake IRQ path without changing unrelated PMIC bits."""
from pathlib import Path
import re,subprocess,tempfile
s=(Path(__file__).resolve().parents[1]/'src/switchpwrbtn.c').read_text();parts=[]
for name in ['SwPwrDisplayChanged','SwPwrPressReport','SwPwrSetIrq','SwPwrEvtD0Entry','SwPwrEvtD0Exit','SwPwrEvtIsr']:
 m=re.search(r'^(?:static )?(?:NTSTATUS|BOOLEAN|UCHAR) '+name+r'\(',s,re.M);assert m
 b=s.index('{',m.start());e=b+1;n=1
 while n:n+=(s[e]=='{')-(s[e]=='}');e+=1
 parts.append(s[m.start():e])
prefix=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
typedef bool BOOLEAN;typedef int32_t NTSTATUS;typedef uint32_t ULONG;typedef uint8_t UCHAR;
typedef int32_t LONG;typedef ULONG *PULONG;typedef void *PVOID;
typedef struct {ULONG value;} GUID;typedef const GUID *LPCGUID;
static const GUID SwPwrDisplayGuid={1}, OtherGuid={2};
#define IsEqualGUID(a,b) ((a)->value==(b)->value)
#define VOID void
#define TRUE true
#define FALSE false
#define STATUS_SUCCESS 0
#define NT_SUCCESS(s) ((s)>=0)
#define UNREFERENCED_PARAMETER(x) (void)(x)
#define MAX77620_IRQTOPM 0x0d
#define MAX77620_INTENLBT 0x0e
#define MAX77620_ONOFFIRQM 0x12
#define MAX77620_ONOFFIRQ 0x0b
#define MAX77620_ONOFFSTAT 0x15
#define ONOFFSTAT_EN0 4
#define ONOFFIRQ_EN0_R 8
#define ONOFFIRQ_EN0_F 4
#define REPORT_POWER 1
#define REPORT_WAKE 2
#define WdfPowerDeviceD3Final 5
typedef int WDF_POWER_DEVICE_STATE,POWER_ACTION;
enum {PowerActionNone,PowerActionSleep,PowerActionShutdown,PowerActionShutdownReset,PowerActionShutdownOff};
struct Device{void *I2c,*Vhf,*Interrupt;int I2cId,Presses;bool IrqConfigured;UCHAR OriginalTopMask,OriginalGlobalMask,OriginalOnOffMask;LONG DisplayOff,LastIrqStatus,LastOnOffIrq;};
typedef struct Device *WDFDEVICE,*PSWPWR_DEVICE,*WDFINTERRUPT;
static UCHAR regs[256];static int fail=-1;static POWER_ACTION action;static UCHAR reports[32];static unsigned count,queued;
static NTSTATUS SwPwrReadReg(void *t,UCHAR r,UCHAR *v){(void)t;if(r==fail)return -1;*v=regs[r];if(r==0x0b)regs[r]=0;return 0;}
static NTSTATUS SwPwrWriteReg(void *t,UCHAR r,UCHAR v){(void)t;if(r==fail)return -1;regs[r]=v;return 0;}
static PSWPWR_DEVICE SwPwrGetDevice(WDFDEVICE d){return d;}
static WDFDEVICE WdfInterruptGetDevice(WDFINTERRUPT i){return i;}
static POWER_ACTION WdfDeviceGetSystemPowerAction(WDFDEVICE d){(void)d;return action;}
static NTSTATUS SwPwrOpenTarget(WDFDEVICE d,int id,void **p){(void)id;*p=d;return 0;}
#define SwPwrTrace(...) assert(!"Registry I/O in an ISR or D0 callback")
static void SwPwrReport(PSWPWR_DEVICE d,UCHAR p){(void)d;assert(count<32);reports[count++]=p;}
static int InterlockedIncrement(int *p){return ++*p;}
static LONG InterlockedExchange(volatile LONG *p,LONG v){LONG old=*p;*p=v;return old;}
static LONG InterlockedCompareExchange(volatile LONG *p,LONG v,LONG c){LONG old=*p;if(old==c)*p=v;return old;}
static bool WdfInterruptQueueWorkItemForIsr(WDFINTERRUPT i){(void)i;queued++;return true;}
'''
tests=r'''
int main(void){struct Device d={0};d.Interrupt=&d;d.Vhf=&d;
 regs[0x0d]=0xb7;regs[0x0e]=0xa5;regs[0x12]=0xff;regs[0x0b]=8;
 assert(SwPwrEvtD0Entry(&d,0)==0&&d.IrqConfigured&&regs[0x0b]==0);
 assert(regs[0x0d]==0xb5&&regs[0x0e]==0xa4&&regs[0x12]==0xf7);
 /* Sleep keeps the wake route and D0 resume does not discard the wake edge. */
 action=PowerActionSleep;assert(SwPwrEvtD0Exit(&d,3)==0&&d.IrqConfigured);
 regs[0x0b]=8;regs[0x15]=4;assert(SwPwrEvtD0Entry(&d,3)==0&&regs[0x0b]==8);
 assert(SwPwrEvtIsr(&d,0)&&count==2&&reports[0]&&!reports[1]&&regs[0x0b]==0);
 regs[0x0b]=4;regs[0x15]=0;assert(SwPwrEvtIsr(&d,0)&&count==2);
 assert(!SwPwrEvtIsr(&d,0)&&count==2);
 /* Display-off presses emit WAKE, not a second power/sleep request. */
 ULONG display=0;SwPwrDisplayChanged(&SwPwrDisplayGuid,&display,sizeof(display),&d);
 regs[0x0b]=12;assert(SwPwrEvtIsr(&d,0)&&count==4&&reports[2]==REPORT_WAKE&&reports[3]==0);
 regs[0x0b]=4;assert(SwPwrEvtIsr(&d,0)&&count==4);
 display=1;SwPwrDisplayChanged(&OtherGuid,&display,sizeof(display),&d);assert(d.DisplayOff);
 SwPwrDisplayChanged(&SwPwrDisplayGuid,&display,1,&d);assert(d.DisplayOff);
 display=9;SwPwrDisplayChanged(&SwPwrDisplayGuid,&display,sizeof(display),&d);assert(d.DisplayOff);
 display=1;SwPwrDisplayChanged(&SwPwrDisplayGuid,&display,sizeof(display),&d);assert(!d.DisplayOff);
 regs[0x0b]=8;assert(SwPwrEvtIsr(&d,0)&&count==6&&reports[4]==REPORT_POWER&&reports[5]==0);
 /* Repeat display wake/sleep with no held HID bits or release wake interrupt. */
 for(unsigned i=0;i<4;i++) {
  display=i&1;SwPwrDisplayChanged(&SwPwrDisplayGuid,&display,sizeof(display),&d);
  unsigned before=count;regs[0x0b]=8;assert(SwPwrEvtIsr(&d,0));
  assert(count==before+2&&reports[before]==(display?REPORT_POWER:REPORT_WAKE)&&reports[before+1]==0);
  assert(regs[0x12]&ONOFFIRQ_EN0_F);
 }
 /* A different PMIC user changes other bits: shutdown preserves those edits. */
 regs[0x0d]^=0x80;regs[0x0e]^=0x20;regs[0x12]^=0x80;
 action=PowerActionShutdown;assert(SwPwrEvtD0Exit(&d,3)==0&&!d.IrqConfigured);
 assert(regs[0x0d]==0x37&&regs[0x0e]==0x85&&regs[0x12]==0x7f);
 assert(SwPwrEvtD0Entry(&d,3)==0);fail=0x0b;
 assert(SwPwrEvtIsr(&d,0)&&!d.IrqConfigured);fail=-1;
 assert(queued!=0);
 puts("PASS: press-only PMIC wake, display-aware HID wake pulses, deferred diagnostics and shutdown masks");}
'''
with tempfile.TemporaryDirectory(prefix='switchpwrbtn-wake-') as d:
 p=Path(d);(p/'test.c').write_text(prefix+'\n'.join(parts)+tests)
 subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
