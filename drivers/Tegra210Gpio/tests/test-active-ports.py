#!/usr/bin/env python3
"""Run the production Tegra GPIO interrupt query against instrumented registers."""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def definition(source, signature):
    start = source.index(signature)
    end = source.index("\n}", source.index("{", start))
    return source[start:end + 2]


def generate(driver, harness):
    source = driver.read_text()
    definitions = []
    for name in ("REG_INT_STA", "REG_INT_ENB"):
        definitions.append(next(line for line in source.splitlines()
                                if line.startswith("#define " + name + " ")))
    for signature in ("static volatile ULONG *TegraGpioReg(", "static ULONG64 TegraGpioReadBank(",
                      "static NTSTATUS TegraGpioQueryActiveInterrupts("):
        if signature.startswith("static ULONG64") and "TegraGpioReadBank(Ctx, Params->BankId, REG_INT_STA)" not in source:
            continue
        definitions.append(definition(source, signature))
    return harness.read_text().replace('#include "gpio_driver.inc"', "\n\n".join(definitions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--driver", type=Path, default=here.parent / "src/tegra210gpio.c")
    parser.add_argument("--emit", type=Path, help="write standalone C for another target compiler")
    parser.add_argument("--require-sparse", action="store_true", help="also verify the exact filtered register reads")
    args = parser.parse_args()
    code = generate(args.driver, here / "active-ports.c")
    if args.emit:
        args.emit.write_text(code)
        return 0
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "gpio.c"
        binary = Path(folder) / ("gpio.exe" if os.name == "nt" else "gpio")
        source.write_text(code)
        subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-O2", "-std=c11",
                        "-Wall", "-Wextra", "-Werror", str(source), "-o", str(binary)], check=True)
        return subprocess.run([str(binary)] + (["--require-sparse"] if args.require_sparse else [])).returncode


if __name__ == "__main__":
    raise SystemExit(main())
