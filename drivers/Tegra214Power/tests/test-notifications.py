#!/usr/bin/env python3
"""Run the actual display/worker teardown path with deterministic late callbacks."""
from pathlib import Path
import re
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/tegra214pep.c").read_text()
functions = []
for name in ["QueueSnapshot", "DisplayChanged", "Snapshot", "StopNotifications"]:
    start = re.search(r"^(?:static )?(?:VOID|NTSTATUS) " + name + r"\(", source, re.M).start()
    brace = source.index("{", start)
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    functions.append(source[start:end])

prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
typedef void VOID,*PVOID,*PDEVICE_OBJECT;
typedef uint32_t ULONG,*PULONG;typedef int32_t LONG,NTSTATUS;
typedef int KIRQL;typedef unsigned BOOLEAN;typedef wchar_t WCHAR;
typedef struct {int held;} KSPIN_LOCK;
typedef struct {int set;} KEVENT;
typedef const int *LPCGUID;
typedef struct {
 PVOID Lower,Work,DisplayCallback;KSPIN_LOCK Lock;KEVENT Idle;
 BOOLEAN Queued,Stopping;
} T214_DEVICE;
typedef struct {LONG Attempts,Returns,LastStatus;BOOLEAN Enumerated;} CPU;
static CPU Cpus[4];static volatile LONG DisplayOffState;
static WCHAR Parameters[16];static NTSTATUS RegisterStatus;
static int DisplayGuid,queued,completed,unregistered,registry_calls;
static T214_DEVICE *pending;
#define TRUE 1
#define FALSE 0
#define STATUS_SUCCESS 0
#define CPU_COUNT 4
#define IO_NO_INCREMENT 0
#define DelayedWorkQueue 1
#define Executive 1
#define KernelMode 0
#define RTL_REGISTRY_ABSOLUTE 0
#define UNREFERENCED_PARAMETER(x) ((void)(x))
#define IsEqualGUID(a,b) ((a)==(b))
static VOID Snapshot(PDEVICE_OBJECT,PVOID);
static NTSTATUS DisplayChanged(LPCGUID,PVOID,ULONG,PVOID);
static ULONG OnlineCount(void){return 4;}
static LONG InterlockedCompareExchange(volatile LONG*p,LONG v,LONG c){LONG o=*p;if(o==c)*p=v;return o;}
static LONG InterlockedExchange(volatile LONG*p,LONG v){LONG o=*p;*p=v;return o;}
static void KeAcquireSpinLock(KSPIN_LOCK*l,KIRQL*i){assert(!l->held);l->held=1;*i=0;}
static void KeReleaseSpinLock(KSPIN_LOCK*l,KIRQL i){assert(l->held&&i==0);l->held=0;}
static void KeClearEvent(KEVENT*e){e->set=0;}
static void KeSetEvent(KEVENT*e,int increment,int wait){assert(!increment&&!wait);e->set=1;}
static void Trace(const WCHAR*n,ULONG v){(void)n;(void)v;registry_calls++;}
static NTSTATUS RtlCreateRegistryKey(int type,const WCHAR*path){(void)type;(void)path;registry_calls++;return 0;}
static void IoQueueWorkItem(PVOID work,void(*fn)(PDEVICE_OBJECT,PVOID),int queue,PVOID context){
 T214_DEVICE*d=context;
 assert(work==d->Work&&fn==Snapshot&&queue==1&&d->Lock.held&&d->Queued&&!d->Idle.set);
 assert(!pending);pending=d;queued++;
}
static void run_worker(void){T214_DEVICE*d=pending;assert(d);pending=NULL;Snapshot(NULL,d);completed++;}
static NTSTATUS PoUnregisterPowerSettingCallback(PVOID callback){
 T214_DEVICE*d=callback;ULONG off=0;assert(d->Stopping&&!d->Lock.held);
 /* A callback already in flight finishes while unregister is draining it. */
 DisplayChanged(&DisplayGuid,&off,sizeof(off),d);unregistered++;return 0;
}
static NTSTATUS KeWaitForSingleObject(KEVENT*e,int reason,int mode,int alert,PVOID timeout){
 assert(reason==1&&mode==0&&!alert&&!timeout);
 if(!e->set)run_worker();
 assert(e->set);return 0;
}
'''
tests = r'''
int main(void){
 T214_DEVICE d={0};d.Work=&d;d.Idle.set=1;d.DisplayCallback=&d;
 ULONG off=0,on=1;
 DisplayChanged(&DisplayGuid,&off,sizeof(off),&d);
 assert(DisplayOffState&&queued==1&&!completed&&!registry_calls);
 for(int i=0;i<20;i++)DisplayChanged(&DisplayGuid,&on,sizeof(on),&d);
 assert(!DisplayOffState&&queued==1&&!d.Idle.set);
 StopNotifications(&d);
 assert(d.Stopping&&!d.Queued&&d.Idle.set&&!pending&&!d.DisplayCallback);
 assert(!DisplayOffState&&unregistered==1&&completed==1&&registry_calls>0);
 QueueSnapshot(&d);assert(queued==1);
 /* Restart can subscribe again after the old callback and worker drained. */
 d.Stopping=0;d.DisplayCallback=&d;
 DisplayChanged(&DisplayGuid,&off,sizeof(off),&d);
 assert(queued==2);run_worker();
 assert(!d.Queued&&d.Idle.set);
 StopNotifications(&d);assert(completed==2&&unregistered==2&&!DisplayOffState);
 puts("PASS: notification coalescing, deferred registry work, in-flight callback drain and restart");
}
'''
with tempfile.TemporaryDirectory(prefix="t214-pep-notify-") as temp:
    path = Path(temp)
    (path / "test.c").write_text(prefix + "\n".join(functions) + tests)
    subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(path / "test.c"),
                    "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)
