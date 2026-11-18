#!/usr/bin/env python3
"""Build and exercise the production power-button and RTC callbacks with host I/O."""

import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    for end in range(brace + 1, len(source)):
        depth += (source[end] == "{") - (source[end] == "}")
        if not depth:
            return source[start:end + 1]
    raise ValueError("Unterminated function " + signature)


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=here.parents[1],
                        help="Windows-for-Switch-Drivers source tree")
    args = parser.parse_args()
    button_dir = args.source / "drivers/SwitchPowerButton/src"
    button = (button_dir / "switchpwrbtn.c").read_text()
    rtc = (args.source / "drivers/SwitchRtc/src/switchrtc.c").read_text()
    register_names = ("POLL_MS", "MAX77620_ONOFFIRQ", "MAX77620_ONOFFSTAT", "ONOFFIRQ_EN0_R",
                      "ONOFFSTAT_EN0", "TICKS_PER_SECOND", "OFFSET_FORMAT")
    registers = []
    for name in register_names:
        match = re.search(rf"^#define {name}\s+[^\n]+", button + "\n" + rtc, re.MULTILINE)
        if not match:
            raise ValueError("Missing driver constant " + name)
        registers.append(match[0])
    callbacks = [
        function(button, sign + "\n{") for sign in
        ("VOID SwPwrEvtTimer(WDFTIMER Timer)",
         "NTSTATUS SwPwrEvtSuspend(WDFDEVICE Device)",
         "NTSTATUS SwPwrEvtRestart(WDFDEVICE Device)")]
    init = function(button, "NTSTATUS SwPwrEvtSelfManagedIoInit(WDFDEVICE Device)\n{")
    first = init.index("    Status = SwPwrReadReg(Dev->I2c, MAX77620_ONOFFSTAT")
    end = init.index("    if (Dev->Vhf == NULL)", first)
    callbacks.append("static NTSTATUS initial_reads(PSWPWR_DEVICE Dev) {\n"
                     "    UCHAR Stat = 0, Irq = 0;\n    NTSTATUS Status;\n" +
                     init[first:end] + "\n    return STATUS_SUCCESS;\n}")
    callbacks.append(function(rtc, "VOID SwRtcEvtLearnWork(WDFWORKITEM WorkItem)\n{"))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "board_registers.inc").write_text("\n".join(registers) + "\n")
        (out / "board_callbacks.inc").write_text("\n\n".join(callbacks) + "\n")
        binary = out / ("board-controls.exe" if os.name == "nt" else "board-controls")
        subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-std=c11", "-O2",
                        "-Wall", "-Wextra", "-Werror", "-I", str(button_dir), "-I", str(out),
                        str(here / "pmic-button-clock.c"), "-o", str(binary)], check=True)
        return subprocess.run([str(binary)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
