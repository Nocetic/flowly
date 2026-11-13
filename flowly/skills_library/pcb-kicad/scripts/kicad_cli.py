#!/usr/bin/env python3
"""kicad-cli wrapper — one-word actions for common KiCad headless operations.

Stdlib only. Wraps `kicad-cli`. If it's not installed, prints the install hint
and the exact command(s) it would run, so the user can run them locally.

Actions:
    erc <file.kicad_sch>            electrical rules check
    drc <file.kicad_pcb>            design rules check
    gerbers <file.kicad_pcb>        export Gerbers
    drill <file.kicad_pcb>          export drill files
    pos <file.kicad_pcb>            pick-and-place / position file
    bom <file.kicad_sch>            bill of materials (CSV)
    pdf <file.kicad_sch>            schematic to PDF
    netlist <file.kicad_sch>        export netlist
    step <file.kicad_pcb>           3D STEP model
    fab <file.kicad_pcb>            gerbers + drill + pos into one --out dir

Usage:
    kicad_cli.py drc board.kicad_pcb
    kicad_cli.py fab board.kicad_pcb --out fab/
    kicad_cli.py bom board.kicad_sch --out bom.csv
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def find_cli():
    for name in ("kicad-cli", "kicad-cli-nightly"):
        p = shutil.which(name)
        if p:
            return p
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if os.path.exists(mac):
        return mac
    return None


# action -> list of argv templates (excluding the binary). {f}=input, {o}=output
ACTIONS = {
    "erc":     [["sch", "erc", "{f}", "-o", "{o}"]],
    "drc":     [["pcb", "drc", "{f}", "-o", "{o}"]],
    "gerbers": [["pcb", "export", "gerbers", "{f}", "-o", "{o}"]],
    "drill":   [["pcb", "export", "drill", "{f}", "-o", "{o}"]],
    "pos":     [["pcb", "export", "pos", "{f}", "-o", "{o}"]],
    "bom":     [["sch", "export", "bom", "{f}", "-o", "{o}"]],
    "pdf":     [["sch", "export", "pdf", "{f}", "-o", "{o}"]],
    "netlist": [["sch", "export", "netlist", "{f}", "-o", "{o}"]],
    "step":    [["pcb", "export", "step", "{f}", "-o", "{o}"]],
    "fab":     [["pcb", "export", "gerbers", "{f}", "-o", "{o}"],
                ["pcb", "export", "drill", "{f}", "-o", "{o}"],
                ["pcb", "export", "pos", "{f}", "-o", "{o}/pos.csv"]],
}

DEFAULT_OUT = {
    "erc": "erc.rpt", "drc": "drc.rpt", "gerbers": "gerbers/", "drill": "gerbers/",
    "pos": "pos.csv", "bom": "bom.csv", "pdf": "schematic.pdf", "netlist": "board.net",
    "step": "board.step", "fab": "fab/",
}

DIR_OUTPUTS = {"gerbers", "drill", "fab"}


def main():
    ap = argparse.ArgumentParser(description="kicad-cli wrapper")
    ap.add_argument("action", choices=list(ACTIONS))
    ap.add_argument("file")
    ap.add_argument("--out", help="output file or directory (sensible default per action)")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        # don't hard-fail if binary is also missing; still show the command
        print(f"⚠️ input not found: {a.file} (showing the command anyway)")

    out = a.out or DEFAULT_OUT[a.action]
    binary = find_cli()

    cmds = []
    for tmpl in ACTIONS[a.action]:
        parts = []
        for part in tmpl:
            p = part.replace("{f}", a.file).replace("{o}", out)
            p = p.replace("//", "/") if "{o}" in part else p
            parts.append(p)
        cmds.append([binary or "kicad-cli"] + parts)

    if binary is None:
        print("⚠️  kicad-cli not found (ships with KiCad 7+).")
        print("    Install: macOS `brew install --cask kicad` · "
              "Linux `apt install kicad` · https://kicad.org/download/")
        print("\n    Run these locally:")
        for cmd in cmds:
            print("    " + " ".join(_q(c) for c in cmd))
        sys.exit(3)

    # make output dir if needed
    if a.action in DIR_OUTPUTS:
        os.makedirs(out, exist_ok=True)
    else:
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)

    ok = True
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print(f"❌ timed out: {' '.join(cmd)}")
            ok = False
            continue
        label = " ".join(cmd[1:4])
        if r.returncode == 0:
            print(f"✅ {label} → {out}")
            if r.stdout.strip():
                print("   " + r.stdout.strip().replace("\n", "\n   ")[:600])
        else:
            ok = False
            print(f"❌ {label} (exit {r.returncode})")
            msg = (r.stderr or r.stdout).strip()
            if msg:
                print("   " + msg.replace("\n", "\n   ")[:600])
    sys.exit(0 if ok else 1)


def _q(s):
    return f'"{s}"' if " " in s else s


if __name__ == "__main__":
    main()
