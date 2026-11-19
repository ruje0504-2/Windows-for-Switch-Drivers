#!/usr/bin/env python3
"""Check real screen-driven WDF idle-reference lifetime and asynchronous wake."""
from pathlib import Path
import re, subprocess, tempfile

s = (Path(__file__).resolve().parents[1] / "display_idle.h").read_text()
parts = []
for name in ["SwDisplayIdleAssignSettings", "SwDisplayIdleWorker", "SwDisplayIdleChanged",
             "SwDisplayIdleConfigureTimeout", "SwDisplayIdleStart", "SwDisplayIdleStop"]:
    m = re.search(r"^static (?:VOID|NTSTATUS) " + name + r"\(", s, re.M)
    assert m, name
    b = s.index("{", m.start())
    e = b + 1
    n = 1
    while n:
        n += (s[e] == "{") - (s[e] == "}")
        e += 1
    parts.append(s[m.start():e])

prefix = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
typedef void VOID,*PVOID;typedef int32_t LONG,NTSTATUS;typedef uint32_t ULONG,*PULONG;
typedef bool BOOLEAN;typedef void *WDFDEVICE;typedef const int *LPCGUID;
typedef enum {WdfFalse=0,WdfTrue=1,WdfUseDefault=2} WDF_TRI_STATE;
typedef enum {IdleCannotWakeFromS0=0} WDF_POWER_POLICY_S0_IDLE_CAPABILITIES;
typedef enum {PowerDeviceD3=4} DEVICE_POWER_STATE;
typedef enum {SystemManagedIdleTimeoutWithHint=2} WDF_POWER_POLICY_IDLE_TIMEOUT_TYPE;
typedef enum {IdleDoNotAllowUserControl=0} WDF_POWER_POLICY_S0_IDLE_USER_CONTROL;
typedef struct {
 WDF_POWER_POLICY_S0_IDLE_CAPABILITIES IdleCaps;
 DEVICE_POWER_STATE DxState;
 ULONG IdleTimeout;
 WDF_POWER_POLICY_IDLE_TIMEOUT_TYPE IdleTimeoutType;
 WDF_TRI_STATE ExcludeD3Cold;
 WDF_TRI_STATE PowerUpIdleDeviceOnSystemWake;
 WDF_POWER_POLICY_S0_IDLE_USER_CONTROL UserControlOfIdleSettings;
 WDF_TRI_STATE Enabled;
} WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS;
typedef struct Work *WDFWORKITEM;
typedef struct Lock *WDFWAITLOCK;
typedef struct {void (*EvtWorkItemFunc)(WDFWORKITEM);BOOLEAN AutomaticSerialization;} WDF_WORKITEM_CONFIG;
typedef struct {WDFDEVICE ParentObject;} WDF_OBJECT_ATTRIBUTES;
#define FALSE false
#define TRUE true
#define NT_SUCCESS(s) ((s)>=0)
#define STATUS_SUCCESS 0
#define STATUS_INVALID_DEVICE_STATE (-2)
#define IsEqualGUID(a,b) ((a)==(b))
#define WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS_INIT(s,c) (memset((s),0,sizeof(*(s))),(s)->IdleCaps=(c))
#define WDF_WORKITEM_CONFIG_INIT(c,fn) (memset((c),0,sizeof(*(c))),(c)->EvtWorkItemFunc=(fn))
#define WDF_OBJECT_ATTRIBUTES_INIT(a) memset((a),0,sizeof(*(a)))
#define WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(a,t) ((void)sizeof(t),memset((a),0,sizeof(*(a))))
typedef struct {
 WDFDEVICE Device;WDFWORKITEM Work;WDFWAITLOCK Lock;PVOID volatile Callback;ULONG IdleTimeoutMs;
 volatile LONG DisplayOn,Closing;BOOLEAN Held;BOOLEAN S0IdleDisabled;NTSTATUS LastStatus;
} SW_DISPLAY_IDLE;
typedef struct {SW_DISPLAY_IDLE *Idle;} SW_DISPLAY_IDLE_WORK;
struct Lock {pthread_mutex_t mu;bool alive;};
struct Work {
 SW_DISPLAY_IDLE_WORK ctx;
 void (*fn)(WDFWORKITEM);
 pthread_mutex_t mu;
 pthread_cond_t cv;
 bool queued;
 unsigned active;
 bool alive;
};
static const int SwDisplayIdleGuid=1;
static struct Lock g_lock;
static struct Work g_work;
static _Thread_local bool holding_lock;
static unsigned references,stops,resumes,unregistered,flushes,assign_calls,hook_hits;
static WDF_TRI_STATE s0_enabled=WdfUseDefault;
static NTSTATUS stop_result,register_result,assign_result,lock_create_result,work_create_result;
static ULONG initial_reg_state=1;
static NTSTATUS (*callback)(LPCGUID,PVOID,ULONG,PVOID);static PVOID cb_context;
static pthread_mutex_t step_mu=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t step_cv=PTHREAD_COND_INITIALIZER;
static bool pause_stop,pause_resume,in_hook,allow_hook,unreg_fire_wake,flush_saw_unregistered,flush_entered,osc_stop;
static void wait_flag(bool *flag){
 struct timespec until;assert(clock_gettime(CLOCK_REALTIME,&until)==0);until.tv_sec+=5;
 while(!*flag)assert(pthread_cond_timedwait(&step_cv,&step_mu,&until)==0);
}
static void run(WDFWORKITEM w);
static SW_DISPLAY_IDLE_WORK *SwDisplayIdleWork(WDFWORKITEM w){return &w->ctx;}
static LONG InterlockedCompareExchange(volatile LONG *p,LONG v,LONG c){
 LONG expected=c;
 __atomic_compare_exchange_n(p,&expected,v,false,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);
 return expected;
}
static LONG InterlockedExchange(volatile LONG *p,LONG v){return __atomic_exchange_n(p,v,__ATOMIC_SEQ_CST);}
static PVOID InterlockedExchangePointer(PVOID volatile *p,PVOID v){return __atomic_exchange_n(p,v,__ATOMIC_SEQ_CST);}
static PVOID InterlockedCompareExchangePointer(PVOID volatile *p,PVOID v,PVOID c){
 PVOID expected=c;
 __atomic_compare_exchange_n(p,&expected,v,false,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);
 return expected;
}
static NTSTATUS WdfWaitLockCreate(WDF_OBJECT_ATTRIBUTES *a,WDFWAITLOCK *out){
 (void)a;if(lock_create_result<0)return lock_create_result;
 assert(!g_lock.alive);assert(pthread_mutex_init(&g_lock.mu,NULL)==0);g_lock.alive=true;*out=&g_lock;return 0;
}
static void WdfWaitLockAcquire(WDFWAITLOCK l,void *t){
 (void)t;assert(l&&l->alive&&!holding_lock);assert(pthread_mutex_lock(&l->mu)==0);holding_lock=true;
}
static void WdfWaitLockRelease(WDFWAITLOCK l){
 assert(l&&l->alive&&holding_lock);holding_lock=false;assert(pthread_mutex_unlock(&l->mu)==0);
}
static NTSTATUS WdfWorkItemCreate(WDF_WORKITEM_CONFIG *c,WDF_OBJECT_ATTRIBUTES *a,WDFWORKITEM *out){
 (void)a;if(work_create_result<0)return work_create_result;
 assert(!g_work.alive);memset(&g_work,0,sizeof(g_work));g_work.fn=c->EvtWorkItemFunc;
 assert(pthread_mutex_init(&g_work.mu,NULL)==0);assert(pthread_cond_init(&g_work.cv,NULL)==0);
 g_work.alive=true;*out=&g_work;return 0;
}
static void WdfObjectDelete(PVOID o){
 if(o==&g_lock){assert(g_lock.alive);g_lock.alive=false;assert(pthread_mutex_destroy(&g_lock.mu)==0);}
 else if(o==&g_work){assert(g_work.alive&&!g_work.active);g_work.alive=false;
  assert(pthread_cond_destroy(&g_work.cv)==0);assert(pthread_mutex_destroy(&g_work.mu)==0);}
 else assert(0);
}
static NTSTATUS WdfDeviceAssignS0IdleSettings(WDFDEVICE d,WDF_DEVICE_POWER_POLICY_IDLE_SETTINGS *s){
 (void)d;assert(!holding_lock&&s->IdleCaps==IdleCannotWakeFromS0&&s->IdleTimeoutType==SystemManagedIdleTimeoutWithHint);
 assign_calls++;if(assign_result<0)return assign_result;s0_enabled=s->Enabled;return 0;
}
static NTSTATUS WdfDeviceStopIdle(WDFDEVICE d,BOOLEAN wait){
 (void)d;assert(!wait&&holding_lock);
 pthread_mutex_lock(&step_mu);
 if(pause_stop){in_hook=true;hook_hits++;pthread_cond_broadcast(&step_cv);wait_flag(&allow_hook);}
 pthread_mutex_unlock(&step_mu);
 stops++;if(NT_SUCCESS(stop_result))references++;return stop_result;
}
static void WdfDeviceResumeIdle(WDFDEVICE d){
 (void)d;assert(holding_lock&&references);
 pthread_mutex_lock(&step_mu);
 if(pause_resume){in_hook=true;hook_hits++;pthread_cond_broadcast(&step_cv);wait_flag(&allow_hook);}
 pthread_mutex_unlock(&step_mu);
 references--;resumes++;
}
static void WdfWorkItemEnqueue(WDFWORKITEM w){
 assert(w&&w->alive);pthread_mutex_lock(&w->mu);w->queued=true;pthread_cond_broadcast(&w->cv);pthread_mutex_unlock(&w->mu);
}
static void WdfWorkItemFlush(WDFWORKITEM w){
 assert(w&&w->alive&&!holding_lock);flushes++;flush_saw_unregistered=(callback==NULL);
 pthread_mutex_lock(&step_mu);flush_entered=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 pthread_mutex_lock(&w->mu);
 while(w->active){
  struct timespec until;assert(clock_gettime(CLOCK_REALTIME,&until)==0);until.tv_sec+=5;
  assert(pthread_cond_timedwait(&w->cv,&w->mu,&until)==0);
 }
 pthread_mutex_unlock(&w->mu);
 run(w);
}
static WDFDEVICE WdfDeviceWdmGetDeviceObject(WDFDEVICE d){return d;}
static NTSTATUS PoRegisterPowerSettingCallback(WDFDEVICE d,LPCGUID g,NTSTATUS (*fn)(LPCGUID,PVOID,ULONG,PVOID),PVOID c,PVOID *h){
 (void)d;assert(!holding_lock);if(register_result<0)return register_result;
 callback=fn;cb_context=c;*h=c;ULONG on=initial_reg_state;fn(g,&on,4,c);return 0;
}
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID h){
 assert(!holding_lock&&h==cb_context);
 if(unreg_fire_wake&&callback){unreg_fire_wake=false;ULONG on=1;callback(&SwDisplayIdleGuid,&on,4,cb_context);}
 callback=NULL;unregistered++;return 0;
}
'''

tests = r'''
static void run(WDFWORKITEM w){
 for(;;){
  pthread_mutex_lock(&w->mu);
  if(!w->queued){pthread_mutex_unlock(&w->mu);return;}
  w->queued=false;w->active++;
  pthread_mutex_unlock(&w->mu);
  w->fn(w);
  pthread_mutex_lock(&w->mu);
  w->active--;pthread_cond_broadcast(&w->cv);
  pthread_mutex_unlock(&w->mu);
 }
}
static void state(WDFWORKITEM w,ULONG n){assert(callback);callback(&SwDisplayIdleGuid,&n,4,cb_context);run(w);}
struct worker_arg{WDFWORKITEM w;bool started;};
static void *direct_worker(void *arg){
 struct worker_arg *wa=arg;
 pthread_mutex_lock(&wa->w->mu);wa->w->active++;pthread_mutex_unlock(&wa->w->mu);
 pthread_mutex_lock(&step_mu);wa->started=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 wa->w->fn(wa->w);
 pthread_mutex_lock(&wa->w->mu);wa->w->active--;pthread_cond_broadcast(&wa->w->cv);pthread_mutex_unlock(&wa->w->mu);
 return NULL;
}
static void *stop_thread(void *arg){SwDisplayIdleStop(arg);return NULL;}
static void *osc_worker(void *arg){
 WDFWORKITEM w=arg;
 for(;;){
  pthread_mutex_lock(&w->mu);
  while(!w->queued&&!osc_stop){
   struct timespec until;assert(clock_gettime(CLOCK_REALTIME,&until)==0);until.tv_sec+=5;
   assert(pthread_cond_timedwait(&w->cv,&w->mu,&until)==0);
  }
  if(!w->queued&&osc_stop){pthread_mutex_unlock(&w->mu);return NULL;}
  w->queued=false;w->active++;
  pthread_mutex_unlock(&w->mu);
  w->fn(w);
  pthread_mutex_lock(&w->mu);
  w->active--;pthread_cond_broadcast(&w->cv);
  pthread_mutex_unlock(&w->mu);
 }
}
int main(void){
 SW_DISPLAY_IDLE d={0};
 /* Unconfigured idle state refuses Start cleanly and Configure rolls back child objects on failure. */
 assert(SwDisplayIdleStart(&d)==STATUS_INVALID_DEVICE_STATE);
 lock_create_result=-3;assert(SwDisplayIdleConfigureTimeout(&d,&d,1500)==-3&&!g_lock.alive&&assign_calls==0);lock_create_result=0;
 work_create_result=-4;assert(SwDisplayIdleConfigureTimeout(&d,&d,1500)==-4&&!g_lock.alive&&!g_work.alive&&assign_calls==0);work_create_result=0;
 assign_result=-5;assert(SwDisplayIdleConfigureTimeout(&d,&d,1500)==-5&&!g_lock.alive&&!g_work.alive&&s0_enabled==WdfUseDefault);assign_result=0;
 assert(SwDisplayIdleConfigureTimeout(&d,&d,1500)==0&&g_lock.alive&&g_work.alive&&s0_enabled==WdfTrue);

 assert(SwDisplayIdleStart(&d)==0);run(d.Work);assert(references==1&&d.Held&&stops==1&&s0_enabled==WdfTrue);
 /* Second Start while active is a no-op. */
 assert(SwDisplayIdleStart(&d)==0&&references==1&&stops==1);
 for(unsigned n=0;n<50;n++){
  state(d.Work,0);assert(references==0&&!d.Held);state(d.Work,0);assert(references==0);
  stop_result=0x103;state(d.Work,1);assert(references==1&&d.Held);state(d.Work,2);assert(references==1);
 }
 /* A failed wake obtains no power reference and cleanup must not release one. */
 state(d.Work,0);stop_result=-1;state(d.Work,1);assert(!d.Held&&references==0);
 stop_result=0;state(d.Work,1);assert(d.Held&&references==1);

 /* Concurrent double-0 worker re-entry must call WdfDeviceResumeIdle only once. */
 pthread_t t1,t2;
 struct worker_arg wa1={d.Work,false},wa2={d.Work,false};
 pause_resume=true;in_hook=allow_hook=false;hook_hits=0;
 ULONG v0=0,v1=1;
 callback(&SwDisplayIdleGuid,&v0,4,cb_context);
 assert(pthread_create(&t1,NULL,direct_worker,&wa1)==0);
 pthread_mutex_lock(&step_mu);wait_flag(&in_hook);
 assert(pthread_create(&t2,NULL,direct_worker,&wa2)==0);
 wait_flag(&wa2.started);allow_hook=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 pause_resume=false;run(d.Work);
 assert(hook_hits==1&&references==0&&!d.Held);

 /* Concurrent double-1 worker re-entry must call WdfDeviceStopIdle only once. */
 wa1=(struct worker_arg){d.Work,false};wa2=(struct worker_arg){d.Work,false};
 pause_stop=true;in_hook=allow_hook=false;hook_hits=0;
 callback(&SwDisplayIdleGuid,&v1,4,cb_context);
 assert(pthread_create(&t1,NULL,direct_worker,&wa1)==0);
 pthread_mutex_lock(&step_mu);wait_flag(&in_hook);
 assert(pthread_create(&t2,NULL,direct_worker,&wa2)==0);
 wait_flag(&wa2.started);allow_hook=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 pause_stop=false;run(d.Work);
 assert(hook_hits==1&&references==1&&d.Held);

 /* Concurrent 1->0 oscillation while StopIdle is in flight must converge to 0 (no SC7 leak). */
 state(d.Work,0);assert(references==0&&!d.Held);
 wa1=(struct worker_arg){d.Work,false};wa2=(struct worker_arg){d.Work,false};
 pause_stop=true;in_hook=allow_hook=false;hook_hits=0;
 callback(&SwDisplayIdleGuid,&v1,4,cb_context);
 assert(pthread_create(&t1,NULL,direct_worker,&wa1)==0);
 pthread_mutex_lock(&step_mu);wait_flag(&in_hook);
 callback(&SwDisplayIdleGuid,&v0,4,cb_context);
 assert(pthread_create(&t2,NULL,direct_worker,&wa2)==0);
 wait_flag(&wa2.started);allow_hook=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 pause_stop=false;run(d.Work);
 assert(references==0&&!d.Held);

 /* Concurrent 0->1 oscillation while ResumeIdle is in flight must converge to 1 (no lost wake). */
 state(d.Work,1);assert(references==1&&d.Held);
 wa1=(struct worker_arg){d.Work,false};wa2=(struct worker_arg){d.Work,false};
 pause_resume=true;in_hook=allow_hook=false;
 callback(&SwDisplayIdleGuid,&v0,4,cb_context);
 assert(pthread_create(&t1,NULL,direct_worker,&wa1)==0);
 pthread_mutex_lock(&step_mu);wait_flag(&in_hook);
 callback(&SwDisplayIdleGuid,&v1,4,cb_context);
 assert(pthread_create(&t2,NULL,direct_worker,&wa2)==0);
 wait_flag(&wa2.started);allow_hook=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 pause_resume=false;run(d.Work);
 assert(references==1&&d.Held);

 /* High-frequency multi-threaded idle oscillation converges cleanly with balanced references. */
 osc_stop=false;
 assert(pthread_create(&t1,NULL,osc_worker,d.Work)==0&&pthread_create(&t2,NULL,osc_worker,d.Work)==0);
 for(unsigned i=0;i<400;i++){ULONG st=i%3;callback(&SwDisplayIdleGuid,&st,4,cb_context);}
 pthread_mutex_lock(&d.Work->mu);osc_stop=true;pthread_cond_broadcast(&d.Work->cv);pthread_mutex_unlock(&d.Work->mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 LONG final_on=InterlockedCompareExchange(&d.DisplayOn,0,0);
 assert(d.Held==(final_on!=0)&&references==(d.Held?1u:0u));

 /* Teardown racing an active StopIdle worker and last-instant callback wake drains safely. */
 state(d.Work,0);assert(references==0&&!d.Held);
 wa1=(struct worker_arg){d.Work,false};
 pause_stop=true;in_hook=allow_hook=flush_entered=flush_saw_unregistered=false;unreg_fire_wake=true;
 callback(&SwDisplayIdleGuid,&v1,4,cb_context);
 assert(pthread_create(&t1,NULL,direct_worker,&wa1)==0);
 pthread_mutex_lock(&step_mu);wait_flag(&in_hook);
 assert(pthread_create(&t2,NULL,stop_thread,&d)==0);
 wait_flag(&flush_entered);
 allow_hook=true;pthread_cond_broadcast(&step_cv);pthread_mutex_unlock(&step_mu);
 assert(pthread_join(t1,NULL)==0&&pthread_join(t2,NULL)==0);
 pause_stop=false;
 assert(!references&&!d.Held&&!d.Callback&&d.Closing&&unregistered==1&&flush_saw_unregistered&&!d.Work->queued);
 /* Second Stop is idempotent. */
 SwDisplayIdleStop(&d);assert(!references&&!d.Held&&unregistered==1);

 /* Registration failure balances the initial reference and disables S0 idle. */
 register_result=-7;assert(SwDisplayIdleStart(&d)==-7&&!references&&!d.Held&&s0_enabled==WdfFalse&&d.S0IdleDisabled);
 register_result=0;
 /* Initial StopIdle failure disables S0 idle so callers ignoring status do not idle while lit. */
 stop_result=-9;assert(SwDisplayIdleStart(&d)==-9&&!references&&!d.Held&&s0_enabled==WdfFalse&&d.S0IdleDisabled);
 stop_result=0;
 /* Subsequent Start re-enables S0 idle; screen-off on registration releases initial reference. */
 initial_reg_state=0;assert(SwDisplayIdleStart(&d)==0&&s0_enabled==WdfTrue&&!d.S0IdleDisabled&&d.Callback!=NULL);
 run(d.Work);assert(!references&&!d.Held);
 SwDisplayIdleStop(&d);assert(!references&&!d.Held&&!d.Callback&&unregistered==2&&flushes>=3);
 /* Caller lifecycle: SelfManagedIoInit -> S0 idle wake + RestartIo no-op -> rebalance Release/RestartIo -> CleanupIo/Release. */
 initial_reg_state=1;
 assert(SwDisplayIdleStart(&d)==0);run(d.Work);assert(references==1&&d.Held&&d.Callback!=NULL);
 state(d.Work,0);assert(references==0&&!d.Held);
 state(d.Work,1);assert(references==1&&d.Held);
 unsigned stops_before=stops;
 assert(SwDisplayIdleStart(&d)==0&&stops==stops_before&&references==1&&d.Held);
 SwDisplayIdleStop(&d);assert(references==0&&!d.Held&&!d.Callback); /* rebalance Release */
 assert(SwDisplayIdleStart(&d)==0);run(d.Work);assert(references==1&&d.Held&&d.Callback!=NULL); /* rebalance RestartIo */
 SwDisplayIdleStop(&d);assert(references==0&&!d.Held&&!d.Callback); /* remove CleanupIo */
 SwDisplayIdleStop(&d);assert(references==0&&!d.Held&&!d.Callback); /* remove Release (idempotent) */
 assert(resumes+2==stops); /* two failed StopIdle calls never owned a reference */
 WdfObjectDelete(d.Work);WdfObjectDelete(d.Lock);
 puts("PASS: display transitions, concurrency, oscillation, failure rollback and teardown races");}
'''

with tempfile.TemporaryDirectory(prefix="display-idle-") as d:
    p = Path(d)
    (p / "test.c").write_text(prefix + "\n".join(parts) + tests)
    subprocess.run(["clang", "-std=c11", "-pthread", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", str(p / "test.c"), "-o", str(p / "test")], check=True)
    subprocess.run([str(p / "test")], check=True)

def body(text, func):
    m = re.search(r"^(?:static\s+)?(?:VOID|NTSTATUS)\s+" + re.escape(func) + r"\([^;]*?\)\s*\{", text, re.M)
    assert m, func
    i = text.index("{", m.start())
    j = i + 1
    depth = 1
    while depth:
        depth += (text[j] == "{") - (text[j] == "}")
        j += 1
    return text[i:j]

drivers_root = Path(__file__).resolve().parents[2]
callers = sorted(p for p in drivers_root.glob("*/src/*.c") if "display_idle.h" in p.read_text())
assert [p.relative_to(drivers_root).as_posix() for p in callers] == [
    "SwitchBattery/src/switchbattery.c",
    "SwitchLiteTouch/src/switchtouch.c",
    "SwitchRtc/src/switchrtc.c",
    "SwitchSensor/src/switchsio.c",
], callers

for path in callers:
    text = path.read_text()
    hooks = dict(re.findall(r"\.(EvtDevice\w+)\s*=\s*(\w+)\s*;", text))
    assert "EvtDeviceSelfManagedIoInit" in hooks, path
    assert "EvtDeviceSelfManagedIoCleanup" in hooks, path
    assert "SwDisplayIdleStart(" in body(text, hooks["EvtDeviceSelfManagedIoInit"]), path
    assert "SwDisplayIdleStop(" in body(text, hooks["EvtDeviceSelfManagedIoCleanup"]), path
    for d0_slot in ("EvtDeviceD0Entry", "EvtDeviceD0EntryPostInterruptsEnabled"):
        if d0_slot in hooks:
            d0_body = body(text, hooks[d0_slot])
            assert "SwDisplayIdleStart(" not in d0_body, (path, d0_slot)
            assert "SwDisplayIdleStop(" not in d0_body, (path, d0_slot)
    if "EvtDeviceReleaseHardware" in hooks and "SwDisplayIdleStop(" in body(text, hooks["EvtDeviceReleaseHardware"]):
        assert "EvtDeviceSelfManagedIoRestart" in hooks, path
        assert "SwDisplayIdleStart(" in body(text, hooks["EvtDeviceSelfManagedIoRestart"]), path
print("PASS: all display_idle callers start/stop in SelfManagedIoInit/Cleanup outside D0Entry")
