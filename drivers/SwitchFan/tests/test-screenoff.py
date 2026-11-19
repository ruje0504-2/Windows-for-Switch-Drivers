#!/usr/bin/env python3
"""Exercise the actual fan timer and D0 callbacks with a simulated TMP451/CPU."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'src/switchfan.c').read_text()
names = ['SwFanSetDuty', 'SwFanDutyForTemp', 'SwFanEvtTimer',
         'SwFanEvtD0Entry', 'SwFanEvtD0Exit', 'SwFanApplyBacklight', 'SwFanDisplayStateCallback',
         'SwFanEvtSuspend', 'SwFanEvtRestart', 'SwFanEvtSelfManagedIoCleanup', 'SwFanEvtReleaseHardware']

def function(name):
    match = re.search(r'^(?:static )?(?:VOID|ULONG|NTSTATUS) ' + name + r'\([^;]*?\)\n\{', source, re.M)
    assert match, name
    end = match.end()
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]

prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
typedef uint32_t ULONG; typedef uint8_t UCHAR; typedef int32_t LONG;
typedef int32_t NTSTATUS; typedef bool BOOLEAN; typedef void VOID;
typedef ULONG *PULONG; typedef void *WDFDEVICE; typedef void *WDFTIMER; typedef void *WDFCMRESLIST;
typedef unsigned WDF_POWER_DEVICE_STATE;
typedef void *PVOID; typedef const int *LPCGUID;
static const int SwFanDisplayStateGuid=1;
#define IsEqualGUID(a,b) ((a)==(b))
#define PWM_CSR_0 0
#define WdfPowerDeviceD3Final 5
typedef enum {PowerActionNone,PowerActionSleep,PowerActionHibernate,
              PowerActionShutdown,PowerActionShutdownReset} POWER_ACTION;
#define TRUE true
#define FALSE false
#define STATUS_SUCCESS 0
#define NT_SUCCESS(s) ((s)>=0)
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define PWM_DUTY_MAX 236u
#define PWM_CSR_EN (1u<<31)
#define PWM_ABSOLUTE_OFF (0x100u<<16)
#define LITE_CPU_KHZ 1996800u
#define TMP451_REMOTE 1
#define TMP451_LOCAL 0
#define FAILSAFE_DUTY 153u
#define POLL_MS 2000
#define WDF_REL_TIMEOUT_IN_MS(n) (n)
typedef struct {
 UCHAR *Pwm,*Car,*BacklightPwm; ULONG PwmLength,BacklightLength,FanOffset,Duty,DisplayState,LastTemp,BootCpuKhz,Brightness,BacklightBootCsr;
 void *Sensor,*Pmic,*DisplayCallback,*Timer,*BacklightLock,*Device;
 BOOLEAN IsLite,Stopping,BacklightReady,CpuConfigured,CpuApplied,TempValid,CpuOverclockEnabled;
 BOOLEAN ScreenOffIdle,SensorNeedsReinit,GpuRailOn;
 struct {ULONG CpuKhz;} Config;
 ULONG Failures; LONG Runs;
} SWFAN_DEVICE,*PSWFAN_DEVICE;
VOID SwFanEvtTimer(WDFTIMER Timer);
NTSTATUS SwFanDisplayStateCallback(LPCGUID Setting,PVOID Value,ULONG Length,PVOID Context);
static PSWFAN_DEVICE SwFanGetDevice(void *d){return d;}
static PSWFAN_DEVICE SwFanGetTimer(void *d){return d;}
static ULONG temp=32,sleep_off; static bool sensor_fail,sensor_config_fail,clock_fail;
static POWER_ACTION action; static char order[64];static unsigned next;
static bool cpu_locked,backlight_locked,timer_armed,stopping_set_under_lock;
static bool wake_during_unregister,wake_during_timer_stop,run_timer_in_stop,wake_during_read;
static PSWFAN_DEVICE hook_dev;
static unsigned registry_writes,timer_starts,last_timer_ms,sensor_inits,unmaps,deletes;
static void SwFanCpuLock(PSWFAN_DEVICE d){(void)d;assert(!cpu_locked);cpu_locked=true;}
static void SwFanCpuUnlock(PSWFAN_DEVICE d){(void)d;assert(cpu_locked);cpu_locked=false;}
static void event(char c){assert(next<sizeof(order));order[next++]=c;}
static void SwFanTrace(const wchar_t *n,ULONG v){assert(!backlight_locked);registry_writes++;if(!wcscmp(n,L"SleepFanOff"))sleep_off=v;}
static void WdfWaitLockAcquire(void *l,void *t){(void)l;(void)t;assert(!backlight_locked);backlight_locked=true;}
static void WdfWaitLockRelease(void *l){(void)l;assert(backlight_locked);if(hook_dev&&hook_dev->Stopping)stopping_set_under_lock=true;backlight_locked=false;}
static NTSTATUS SwFanConfigureSensor(void *s){(void)s;sensor_inits++;return sensor_config_fail?-2:0;}
static NTSTATUS SwFanReadReg(void *s,UCHAR reg,UCHAR *v){
 (void)s;assert(reg==0||reg==1);
 if(wake_during_read&&reg==TMP451_REMOTE){wake_during_read=false;ULONG on=1;SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&on,sizeof(on),hook_dev);}
 if(sensor_fail)return -1;*v=(UCHAR)temp;return 0;
}
static NTSTATUS SwCpuApply(PSWFAN_DEVICE d,BOOLEAN oc){
 assert(!oc);event('c');if(clock_fail)return -1;d->CpuApplied=false;return 0;
}
static void SwCpuPrepare(WDFDEVICE d){(void)d;event('p');}
static void SwFanStartBacklight(WDFDEVICE d){(void)d;}
static ULONG SwCpuKhz(PSWFAN_DEVICE d){return d->CpuApplied?LITE_CPU_KHZ:d->BootCpuKhz;}
static void SwCpuTemperature(PSWFAN_DEVICE d,ULONG t,BOOLEAN valid){
 if(d->CpuConfigured){d->CpuApplied=d->CpuOverclockEnabled&&valid&&t<80;event(d->CpuApplied?'O':'c');}
}
static void write_pwm(PULONG p,ULONG v){*p=v;event(v==(PWM_CSR_EN|PWM_ABSOLUTE_OFF)?'f':'F');}
#define WRITE_REGISTER_ULONG(p,v) write_pwm((p),(v))
#define READ_REGISTER_ULONG(p) (*(p))
static LONG InterlockedIncrement(volatile LONG *p){return ++*p;}
static void WdfTimerStart(void *t,unsigned ms){(void)t;assert(ms==2000||ms==50||ms==500);timer_armed=true;timer_starts++;last_timer_ms=ms;}
static BOOLEAN WdfTimerStop(WDFTIMER t,BOOLEAN wait){
 assert(t!=NULL&&wait&&!backlight_locked);event('T');
 if(run_timer_in_stop){run_timer_in_stop=false;timer_armed=false;SwFanEvtTimer(t);}
 timer_armed=false;
 if(wake_during_timer_stop){wake_during_timer_stop=false;ULONG on=1;SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&on,sizeof(on),hook_dev);}
 return TRUE;
}
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID h){
 assert(h!=NULL&&!backlight_locked);event('U');
 if(wake_during_unregister){wake_during_unregister=false;ULONG on=1;SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&on,sizeof(on),hook_dev);}
 return 0;
}
static void MmUnmapIoSpace(PVOID base,size_t len){assert(base!=NULL&&len!=0&&!timer_armed);event('M');unmaps++;}
static void WdfObjectDelete(PVOID o){assert(o!=NULL&&!timer_armed);event('D');deletes++;}
static POWER_ACTION WdfDeviceGetSystemPowerAction(void *d){(void)d;return action;}
'''
tests = r'''
static ULONG pwm;
static ULONG bl_reg,car_reg;
static void init(PSWFAN_DEVICE d){
 memset(d,0,sizeof(*d));d->Pwm=(UCHAR*)&pwm;d->PwmLength=sizeof(ULONG);d->Device=d;d->Timer=d;
 d->IsLite=d->BacklightReady=d->CpuConfigured=d->CpuOverclockEnabled=true;d->CpuApplied=true;
 d->Config.CpuKhz=LITE_CPU_KHZ;d->BootCpuKhz=998400;d->DisplayState=1;d->Duty=51;d->BacklightBootCsr=0x31;
 temp=32;sensor_fail=sensor_config_fail=clock_fail=false;action=PowerActionNone;sleep_off=0;next=0;
 timer_starts=last_timer_ms=sensor_inits=unmaps=deletes=0;
 timer_armed=stopping_set_under_lock=false;
 wake_during_unregister=wake_during_timer_stop=run_timer_in_stop=wake_during_read=false;
 hook_dev=d;
}
static void fire_timer(PSWFAN_DEVICE d){timer_armed=false;SwFanEvtTimer(d);}
int main(void){
 SWFAN_DEVICE d;
 /* Earlier active cooling, reaching full PWM at 45 C. Exercise both
    threshold edges and hysteresis through the actual timer callback. */
 init(&d);d.CpuOverclockEnabled=false;d.CpuApplied=false;d.Duty=0;
 temp=34;SwFanEvtTimer(&d);assert(d.Duty==0);
 next=0;temp=35;SwFanEvtTimer(&d);assert(d.Duty==76);
 next=0;temp=33;SwFanEvtTimer(&d);assert(d.Duty==76);
 next=0;temp=32;SwFanEvtTimer(&d);assert(d.Duty==0);
 const ULONG thresholds[]={35,38,40,43,45};
 const ULONG duties[]={76,118,177,212,236};
 for(unsigned i=0;i<5;i++){
  next=0;temp=thresholds[i];SwFanEvtTimer(&d);assert(d.Duty==duties[i]);
  assert(pwm==(PWM_CSR_EN|((PWM_DUTY_MAX-duties[i])<<16)));
  if(i){next=0;temp=thresholds[i]-1;SwFanEvtTimer(&d);assert(d.Duty==duties[i-1]);}
 }
 init(&d);SwFanEvtTimer(&d);assert(d.Duty==51&&d.CpuApplied&&order[0]=='F'&&order[1]=='O');
 /* Drop CPU overclock before a cool screen-off session stops the fan and parks polling for SC7. */
 init(&d);d.DisplayState=0;SwFanEvtTimer(&d);
 assert(!d.CpuApplied&&d.Duty==0&&d.TempValid&&d.ScreenOffIdle&&timer_starts==0&&order[0]=='c'&&order[1]=='f');
 /* Active GPU rail keeps thermal polling alive even while screen is off. */
 init(&d);d.DisplayState=0;d.GpuRailOn=true;SwFanEvtTimer(&d);
 assert(!d.CpuApplied&&d.Duty==0&&!d.ScreenOffIdle&&timer_starts==1&&last_timer_ms==2000);
 /* Display wake starts 76 cooling floor + 50 ms fast poll + TMP451 reinit before CPU goes back to overclock. */
 init(&d);d.DisplayState=0;SwFanEvtTimer(&d);assert(d.ScreenOffIdle&&timer_starts==0);
 ULONG state_on=1;assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&state_on,sizeof(state_on),&d)==0);
 assert(!d.ScreenOffIdle&&d.SensorNeedsReinit&&d.Duty==76&&timer_starts==1&&last_timer_ms==50);
 next=0;SwFanEvtTimer(&d);
 assert(!d.SensorNeedsReinit&&sensor_inits==1&&d.Duty==51&&d.CpuApplied&&order[0]=='F'&&order[1]=='O');
 /* Lost PWM_CSR_0 after SC7 recovers boot PFM divider 0x31 on display wake. */
 ULONG lost_bl=0;d.BacklightPwm=(UCHAR*)&lost_bl;d.Brightness=150;
 assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&state_on,sizeof(state_on),&d)==0);
 assert((lost_bl&0xffff)==0x31&&((lost_bl>>16)&0x7fff)==150u);
 /* Screen off is not proof that heat production stopped. */
 init(&d);d.DisplayState=0;temp=55;SwFanEvtTimer(&d);assert(d.Duty==236&&!d.CpuApplied);
 init(&d);d.DisplayState=0;clock_fail=true;SwFanEvtTimer(&d);assert(d.Duty==51&&d.CpuApplied);
 init(&d);d.DisplayState=0;d.CpuConfigured=false;SwFanEvtTimer(&d);assert(d.Duty==51);
 init(&d);d.DisplayState=2;SwFanEvtTimer(&d);assert(d.Duty==51&&d.CpuApplied);
 /* Stock-clock diagnostics retain the ordinary thermal curve on both sides
    of display-off, without the optional overclock's cooling floor. */
 init(&d);d.CpuOverclockEnabled=false;d.CpuApplied=false;
 SwFanEvtTimer(&d);assert(!d.CpuApplied&&d.Duty==0&&d.TempValid);
 d.DisplayState=0;next=0;SwFanEvtTimer(&d);assert(!d.CpuApplied&&d.Duty==0);
 d.DisplayState=1;next=0;temp=55;SwFanEvtTimer(&d);assert(!d.CpuApplied&&d.Duty==236);
 init(&d);d.CpuOverclockEnabled=false;d.CpuApplied=false;d.CpuConfigured=false;
 SwFanEvtTimer(&d);assert(d.Duty==51); /* Unconfirmed clock retains cooling. */
 /* A failed sensor immediately restarts an already stopped fan. */
 init(&d);d.DisplayState=0;d.Duty=0;sensor_fail=true;SwFanEvtTimer(&d);
 assert(d.Duty==153&&!d.TempValid&&!d.CpuApplied&&d.Failures==1);
 /* Existing Erista behavior and shutdown/removal cooling stay intact. */
 init(&d);d.IsLite=false;d.DisplayState=0;SwFanEvtTimer(&d);assert(d.CpuApplied);
 for(action=PowerActionNone;action<=PowerActionShutdownReset;action++){
  POWER_ACTION saved=action;init(&d);action=saved;d.CpuApplied=false;
  d.TempValid=true;d.LastTemp=35;d.Duty=76;SwFanEvtD0Exit(&d,3);
  bool sleeping=action==PowerActionSleep||action==PowerActionHibernate;
  assert(d.Duty==(sleeping?0:76));assert(sleep_off==(unsigned)sleeping);
 }
 init(&d);action=PowerActionSleep;d.CpuApplied=false;d.TempValid=true;d.LastTemp=55;
 d.Duty=76;SwFanEvtD0Exit(&d,3);assert(d.Duty==76&&!sleep_off);
 d.LastTemp=35;d.TempValid=false;SwFanEvtD0Exit(&d,3);assert(d.Duty==76);
 d.TempValid=true;SwFanEvtD0Exit(&d,WdfPowerDeviceD3Final);assert(d.Duty==76);
 d.Duty=0;sleep_off=1;SwFanEvtD0Entry(&d,3);assert(d.Duty==76&&!d.TempValid&&!sleep_off);
 init(&d);d.Stopping=true;SwFanEvtTimer(&d);assert(next==0);
 /* The power callback never writes a registry hive while holding its lock. */
 init(&d);ULONG backlight=0x80000031;d.BacklightPwm=(UCHAR*)&backlight;d.Brightness=150;
 unsigned before=registry_writes;
 for(unsigned i=0;i<40;i++){
  ULONG state=i%2;next=0;
  assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&state,sizeof(state),&d)==0);
  assert(d.DisplayState==state&&((backlight>>16)&0x7fff)==(state?150u:0u));
  assert((backlight&0xffff)==0x31&&!backlight_locked&&registry_writes==before);
 }
 /* Dim/undim while screen stays on does not spuriously request sensor reinit. */
 init(&d);ULONG st_dim=2,st_on=1;
 assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&st_dim,sizeof(st_dim),&d)==0&&!d.SensorNeedsReinit);
 assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&st_on,sizeof(st_on),&d)==0&&!d.SensorNeedsReinit);
 /* Failed TMP451 reinit on wake keeps SensorNeedsReinit set and engages failsafe cooling. */
 init(&d);d.DisplayState=0;fire_timer(&d);assert(d.ScreenOffIdle&&!timer_armed&&d.Duty==0);
 assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&st_on,sizeof(st_on),&d)==0&&d.SensorNeedsReinit&&timer_armed&&last_timer_ms==50);
 d.Duty=0;sensor_config_fail=true;next=0;fire_timer(&d);
 assert(d.SensorNeedsReinit&&!d.TempValid&&d.Duty==FAILSAFE_DUTY&&!d.CpuApplied&&d.Failures==1&&timer_armed&&last_timer_ms==2000);
 sensor_config_fail=false;next=0;fire_timer(&d);
 assert(!d.SensorNeedsReinit&&d.TempValid&&d.Failures==0&&d.Duty==51&&d.CpuApplied&&timer_armed&&last_timer_ms==2000);
 /* Mid-tick display wake prevents parking in ScreenOffIdle and fast-rearms cooling in 50 ms. */
 init(&d);d.DisplayState=0;wake_during_read=true;fire_timer(&d);
 assert(!d.ScreenOffIdle&&d.DisplayState==1&&d.SensorNeedsReinit&&d.Duty==76&&timer_armed&&last_timer_ms==50);
 /* Rapid screen-off/wake oscillation with timer ticks never strands ScreenOffIdle. */
 init(&d);timer_armed=true;
 for(unsigned i=0;i<20;i++){
  ULONG off=0,on=1;
  assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&off,sizeof(off),&d)==0);
  assert(timer_armed);next=0;fire_timer(&d);assert(d.ScreenOffIdle&&!timer_armed);
  assert(SwFanDisplayStateCallback(&SwFanDisplayStateGuid,&on,sizeof(on),&d)==0);
  assert(!d.ScreenOffIdle&&d.SensorNeedsReinit&&d.Duty==76&&timer_armed&&last_timer_ms==50);
  next=0;fire_timer(&d);assert(!d.ScreenOffIdle&&!d.SensorNeedsReinit&&d.CpuApplied&&timer_armed&&last_timer_ms==2000);
 }
 /* Suspend publishes Stopping under BacklightLock before WdfTimerStop, blocking racing wakes and timer rearms. */
 init(&d);d.DisplayState=0;d.ScreenOffIdle=true;d.Duty=0;
 wake_during_timer_stop=true;run_timer_in_stop=true;timer_starts=0;next=0;
 assert(SwFanEvtSuspend(&d)==0);
 assert(stopping_set_under_lock&&d.Stopping&&!d.ScreenOffIdle&&!timer_armed&&timer_starts==0&&d.Duty==76&&!d.CpuApplied);
 /* Restart clears Stopping under BacklightLock and rearms the 500 ms startup timer; sensor failure fails safe. */
 sensor_config_fail=true;assert(SwFanEvtRestart(&d)<0&&d.Duty==FAILSAFE_DUTY&&d.Stopping&&!timer_armed);
 sensor_config_fail=false;assert(SwFanEvtRestart(&d)==0&&!d.Stopping&&!d.ScreenOffIdle&&!d.SensorNeedsReinit&&timer_armed&&last_timer_ms==500);
 /* ReleaseHardware unregisters DisplayCallback BEFORE Suspend/WdfTimerStop and BEFORE MmUnmapIoSpace,
    even if a draining display wake re-arms the 50 ms timer during unregister. */
 init(&d);d.DisplayState=0;d.ScreenOffIdle=true;d.Duty=0;d.Brightness=150;bl_reg=0x80000031;
 d.DisplayCallback=(void*)1;d.BacklightPwm=(UCHAR*)&bl_reg;d.BacklightLength=sizeof(ULONG);d.Car=(UCHAR*)&car_reg;
 wake_during_unregister=true;next=0;
 assert(SwFanEvtReleaseHardware(&d,NULL)==0);
 assert(!d.DisplayCallback&&!d.BacklightPwm&&!d.Pwm&&!d.Car&&!d.BacklightReady&&d.Stopping&&!timer_armed&&unmaps==3&&stopping_set_under_lock);
 char *pos_u=memchr(order,'U',next),*pos_t=memchr(order,'T',next),*pos_c=memchr(order,'c',next),*pos_m=memchr(order,'M',next);
 assert(pos_u&&pos_t&&pos_c&&pos_m&&pos_u<pos_t&&pos_t<pos_c&&pos_c<pos_m);
 /* ReleaseHardware still unregisters and unmaps everything while propagating PLLX restore failure. */
 init(&d);clock_fail=true;d.DisplayCallback=(void*)1;d.BacklightPwm=(UCHAR*)&bl_reg;d.BacklightLength=sizeof(ULONG);d.Car=(UCHAR*)&car_reg;
 assert(SwFanEvtReleaseHardware(&d,NULL)<0&&!d.DisplayCallback&&!d.Pwm&&!d.BacklightPwm&&!d.Car&&!timer_armed&&unmaps==3);
 /* SelfManagedIoCleanup unregisters DisplayCallback before Suspend/WdfTimerStop and before deleting targets. */
 init(&d);d.DisplayState=0;d.ScreenOffIdle=true;d.Duty=0;d.Brightness=150;bl_reg=0x80000031;
 d.DisplayCallback=(void*)1;d.BacklightPwm=(UCHAR*)&bl_reg;d.Pmic=(void*)1;d.Sensor=(void*)1;
 wake_during_unregister=true;next=0;
 SwFanEvtSelfManagedIoCleanup(&d);
 assert(!d.DisplayCallback&&!d.Pmic&&!d.Sensor&&!d.CpuConfigured&&!d.BacklightReady&&!timer_armed&&deletes==2);
 pos_u=memchr(order,'U',next);pos_t=memchr(order,'T',next);char *pos_d=memchr(order,'D',next);
 assert(pos_u&&pos_t&&pos_d&&pos_u<pos_t&&pos_t<pos_d);
 puts("PASS: screen-off cooling, CPU/fan ordering, thermal failures, D0 sleep/wake, suspend and teardown ordering");
}
'''
with tempfile.TemporaryDirectory(prefix='switchfan-test-') as tmp:
    c = Path(tmp) / 'screenoff.c'
    exe = Path(tmp) / 'screenoff'
    backlight = (Path(__file__).resolve().parents[1] / 'src/backlight.h').read_text()
    c.write_text(prefix + backlight + '\n'.join(function(n) for n in names) + tests)
    subprocess.run([os.environ.get('CC', 'clang'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
