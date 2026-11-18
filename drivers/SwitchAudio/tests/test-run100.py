#!/usr/bin/env python3
"""Check the passive stream-start diagnostics with register and clock substitutes."""
import os
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / "src/switchaudio.cpp").read_text()
policy_start = source.index("static ULONG SaRun100CaptureTime(")
policy_end = source.index("\n}", policy_start) + 2
state = source.index("CSaStream::SetState(")
start = source.index('SaSnapshot(L"Run0");', state)
end = source.index("} else if (State == KSSTATE_STOP", start)
policy = source[policy_start:policy_end]
capture = source[start:end]

preamble = r"""
#include <cassert>
#include <cstdint>
#include <cwchar>

using ULONG = uint32_t;
using VOID = void;
struct LARGE_INTEGER { int64_t QuadPart; };
static ULONG until, now = 1800000000, early, late, pcm, slept, recorded;
static bool clock_ok;

static ULONG SaReadParam(const wchar_t *key) {
    assert(std::wcscmp(key, L"Run100UntilUtc") == 0);
    return until;
}
static void KeQuerySystemTime(LARGE_INTEGER *t) { t->QuadPart = now; }
static bool RtlTimeToSecondsSince1970(const LARGE_INTEGER *t, ULONG *seconds) {
    *seconds = static_cast<ULONG>(t->QuadPart);
    return clock_ok;
}
static void SaSnapshot(const wchar_t *name) {
    if (std::wcscmp(name, L"Run0") == 0) ++early;
    if (std::wcscmp(name, L"Run100") == 0) ++late;
}
static void SnapshotPcm(const wchar_t *name) {
    assert(std::wcscmp(name, L"Run100") == 0);
    ++pcm;
}
static void SaSleepMs(ULONG ms) { slept += ms; }
static void SaTrace(const wchar_t *key, ULONG value) {
    assert(std::wcscmp(key, L"Run100CapturedUtc") == 0);
    recorded = value;
}
"""
checks = r"""
static void check(ULONG deadline, bool valid_clock, bool expect_capture) {
    until = deadline;
    clock_ok = valid_clock;
    early = late = pcm = slept = recorded = 0;
    start_stream();
    assert(early == 1);
    assert(late == expect_capture && pcm == expect_capture);
    assert(slept == (expect_capture ? 100u : 0u));
    assert(recorded == (expect_capture ? now : 0u));
}
int main() {
    check(0, true, false);
    check(now - 1, true, false);
    check(now, true, false);
    check(now + 1, true, true);
    check(now + 30, true, true);
    check(now + 31, true, false);
    check(now + 30, false, false);
}
"""

with tempfile.TemporaryDirectory() as tmp:
    cpp = Path(tmp) / "run100.cpp"
    binary = Path(tmp) / "run100"
    cpp.write_text(preamble + policy + "\nstatic void start_stream() {\n" + capture + "}\n" + checks)
    subprocess.run([os.environ.get("CXX", "clang++"), "-std=c++11", "-Wall", "-Wextra",
                    "-Werror", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print("PASS: ordinary playback and bounded Run100 capture")
