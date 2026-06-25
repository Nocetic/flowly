#!/usr/bin/env python3
"""OpenSCAD render helper — compile a .scad to a mesh and/or preview PNG.

Stdlib only. Wraps the `openscad` CLI. If the binary is missing it prints the
install hint and the exact command it *would* run, so the user can render the
script you wrote.

Usage:
    render.py model.scad --out part.stl
    render.py model.scad --out part.3mf --png preview.png
    render.py model.scad --out part.stl -D width=40 -D height=15
    render.py model.scad --check          # parse/compile only, no output file
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def find_openscad():
    for name in ("openscad", "openscad-nightly", "OpenSCAD"):
        p = shutil.which(name)
        if p:
            return p
    # common macOS app bundle location
    mac = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    if os.path.exists(mac):
        return mac
    return None


def build_cmd(binary, scad, out, defines):
    cmd = [binary or "openscad"]
    if out:
        cmd += ["-o", out]
    for d in defines:
        cmd += ["-D", d]
    cmd.append(scad)
    return cmd


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "render timed out (600s)"


def main():
    ap = argparse.ArgumentParser(description="OpenSCAD render helper")
    ap.add_argument("scad", help="input .scad file")
    ap.add_argument("--out", help="output mesh (.stl/.3mf/.off/.amf)")
    ap.add_argument("--png", help="also render a preview PNG")
    ap.add_argument("-D", "--define", action="append", default=[],
                    help="override a parameter, e.g. -D width=40 (repeatable)")
    ap.add_argument("--check", action="store_true", help="compile/parse only (no output)")
    a = ap.parse_args()

    if not os.path.exists(a.scad):
        sys.exit(f"no such file: {a.scad}")
    if not (a.out or a.png or a.check):
        sys.exit("specify --out, --png, or --check")

    binary = find_openscad()
    jobs = []
    if a.check:
        # Compile to a throwaway STL to validate, discard via null output is not
        # portable; render to a temp path instead.
        tmp = a.scad + ".checktmp.stl"
        jobs.append(("check", build_cmd(binary, a.scad, tmp, a.define), tmp))
    if a.out:
        jobs.append(("mesh", build_cmd(binary, a.scad, a.out, a.define), a.out))
    if a.png:
        cmd = build_cmd(binary, a.scad, a.png, a.define)
        cmd += ["--colorscheme=Tomorrow", "--imgsize=800,600"]
        jobs.append(("png", cmd, a.png))

    if binary is None:
        print("⚠️  OpenSCAD not found on this machine.")
        print("    Install: macOS `brew install --cask openscad` · "
              "Linux `apt install openscad` · https://openscad.org/downloads.html")
        print("\n    Then run the command(s) below (the .scad above is ready):")
        for _, cmd, _ in jobs:
            print("    " + " ".join(_quote(c) for c in cmd))
        sys.exit(3)

    ok = True
    for kind, cmd, target in jobs:
        code, out, err = run(cmd)
        if code == 0:
            size = os.path.getsize(target) if os.path.exists(target) else 0
            if kind == "check":
                print(f"✅ compiles cleanly ({size} bytes test mesh)")
                try:
                    os.remove(target)
                except OSError:
                    pass
            else:
                print(f"✅ {kind}: {target} ({size:,} bytes)")
        else:
            ok = False
            print(f"❌ {kind} failed (exit {code})")
            if err.strip():
                print("   " + err.strip().replace("\n", "\n   "))
    sys.exit(0 if ok else 1)


def _quote(s):
    return f'"{s}"' if " " in s else s


if __name__ == "__main__":
    main()
