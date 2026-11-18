#!/usr/bin/env python3
"""Check deferred Wi-Fi report persistence with real writer code and a host registry shim."""
import argparse
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cc", default="clang")
args = parser.parse_args()
windows = Path(__file__).resolve().parents[1] / "windows"
source = (windows / "report.c").read_text()
writer = source[source.index("static VOID WriteReport("):source.index("VOID SwifiReportOpen(")]
header = (windows / "report.h").read_text()
types = header[header.index("typedef enum SWIFI_BOOT_STEP"):header.index("VOID SwifiReportOpen(")]

prefix = r'''

#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
typedef void VOID;
typedef int32_t NTSTATUS;
typedef uint32_t ULONG;
typedef bool BOOLEAN;
typedef int KSPIN_LOCK, KIRQL, WDFKEY;
typedef void *WDFWORKITEM;
typedef unsigned WDFDEVICE;
typedef wchar_t WCHAR;
typedef const WCHAR *UNICODE_STRING;
#define TRUE true
#define FALSE false
#define RTL_NUMBER_OF(a) (sizeof(a) / sizeof((a)[0]))
#define NT_SUCCESS(s) ((s) >= 0)
#define PLUGPLAY_REGKEY_DEVICE 0
#define KEY_SET_VALUE 1
#define WDF_NO_OBJECT_ATTRIBUTES NULL
'''

mocks = r'''

typedef struct {
    WDFDEVICE Device;
    SWIFI_REPORT *Report;
} REPORT_WORK;
static REPORT_WORK *ReportWork(void *p) {
    return p;
}
static unsigned writes, closes, failures;
static bool update_during_write;
static SWIFI_REPORT report;
static WCHAR saved[512];
static void KeAcquireSpinLock(KSPIN_LOCK *l, KIRQL *k) {
    assert(!*l);
    *l = 1;
    *k = 0;
}
static void KeReleaseSpinLock(KSPIN_LOCK *l, KIRQL k) {
    assert(*l && k == 0);
    *l = 0;
}
static NTSTATUS WdfDeviceOpenRegistryKey(WDFDEVICE dev, int kind, int access, void *attrs,
                                         WDFKEY *key) {
    assert(dev == 7 && kind == 0 && access == 1 && !attrs);
    *key = 1;
    return 0;
}
static void WdfRegistryClose(WDFKEY key) {
    assert(key == 1);
    ++closes;
}
static void RtlInitUnicodeString(UNICODE_STRING *d, const WCHAR *s) {
    *d = s;
}
static NTSTATUS RtlStringCchPrintfW(WCHAR *out, size_t len, const WCHAR *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int size = vswprintf(out, len, fmt, ap);
    va_end(ap);
    return size >= 0 ? 0 : -1;
}
static NTSTATUS WdfRegistryAssignUnicodeString(WDFKEY key, UNICODE_STRING *name,
                                               UNICODE_STRING *value) {
    assert(key == 1 && !wcscmp(*name, L"SwitchWifiReport") && report.Lock == 0);
    ++writes;
    if (update_during_write) {
        update_during_write = false;
        report.Data.Session = 2;
        report.Dirty = true;
    }
    if (failures) {
        --failures;
        return -1;
    }
    wcscpy(saved, *value);
    return 0;
}
'''

test = r'''

static void prepare(unsigned fail, bool concurrent) {
    memset(&report, 0, sizeof(report));
    report.Scheduled = report.Dirty = true;
    report.Data.Session = 1;
    writes = closes = 0;
    failures = fail;
    saved[0] = 0;
    update_during_write = concurrent;
}
int main(void) {
    REPORT_WORK work = {7, &report};
    prepare(0, false);
    WriteReport(&work);
    bool normal = writes == 1 && closes == 1 && wcsstr(saved, L"session=1 ") && !report.Scheduled &&
                  !report.Dirty;
    prepare(1, true);
    WriteReport(&work);
    bool concurrent =
        wcsstr(saved, L"session=2 ") && !report.Dirty && !report.Scheduled && closes == 1;
    prepare(1, false);
    WriteReport(&work);
    bool transient =
        writes == 2 && wcsstr(saved, L"session=1 ") && !report.Dirty && !report.Scheduled;
    prepare(4, false);
    WriteReport(&work);
    bool retry = writes == 2 && !saved[0] && report.Dirty && !report.Scheduled;
    unsigned persistent_calls = writes;
    failures = 0;
    report.Scheduled = true;
    WriteReport(&work);
    bool recovered = wcsstr(saved, L"session=1 ") && !report.Dirty && !report.Scheduled;
    printf("normal=%d concurrent=%d transient=%d retry_pending=%d bounded_calls=%u recovered=%d\n",
           normal, concurrent, transient, retry, persistent_calls, recovered);
    return !(normal && concurrent && transient && retry && recovered);
}
'''

with tempfile.TemporaryDirectory(prefix="switch-wifi-report-") as temporary:
    folder = Path(temporary)
    fixture = folder / "report.c"
    fixture.write_text(prefix + types + mocks + writer + test)
    executable = folder / "test-report"
    subprocess.run([args.cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    fixture, "-o", executable], check=True)
    subprocess.run([executable], check=True)
