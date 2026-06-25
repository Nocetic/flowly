#!/usr/bin/env python3
"""CNC helper — feeds & speeds, material/tool table, simple G-code generation,
and G-code sanity check (bounding box + safety scan). Stdlib only. Metric.
Chat-ready markdown. Generated G-code is a STARTING POINT — verify on machine.

Usage:
    cnc.py speeds --material aluminium --tool carbide --dia 6 --flutes 2
    cnc.py speeds --vc 100 --dia 6 --flutes 2 --chipload 0.04
    cnc.py materials
    cnc.py face --width 50 --length 80 --dia 6 --stepover 0.5 --rpm 8000 --feed 1200
    cnc.py drill --holes "10,10 30,10 50,10" --depth 5 --rpm 5000 --feed 200
    cnc.py check program.nc
"""
from __future__ import annotations

import argparse
import math
import re
import sys

# material -> tool -> (Vc m/min low-high, chipload mm/tooth for ~6mm tool)
DATA = {
    "aluminium": {"hss": (70, 0.05), "carbide": (200, 0.06)},
    "brass":     {"hss": (60, 0.05), "carbide": (180, 0.06)},
    "mild-steel": {"hss": (25, 0.03), "carbide": (120, 0.04)},
    "stainless": {"hss": (18, 0.025), "carbide": (90, 0.035)},
    "plastic":   {"hss": (150, 0.08), "carbide": (300, 0.1)},
    "wood":      {"hss": (200, 0.1), "carbide": (400, 0.12)},
    "titanium":  {"hss": (12, 0.02), "carbide": (50, 0.03)},
}


def cmd_materials(a):
    print("**Cutting data (generic starting points — use the tool datasheet)**\n")
    print("| Material | Tool | Vc (m/min) | chip load (mm/tooth, ~6mm) |")
    print("|---|---|---|---|")
    for mat, tools in DATA.items():
        for tool, (vc, cl) in tools.items():
            print(f"| {mat} | {tool} | {vc} | {cl} |")


def cmd_speeds(a):
    if a.vc is not None:
        vc, cl = a.vc, (a.chipload if a.chipload else 0.04)
        src = "explicit"
    else:
        if a.material not in DATA:
            sys.exit(f"unknown material. Options: {', '.join(DATA)}")
        tool = a.tool or "carbide"
        if tool not in DATA[a.material]:
            sys.exit(f"tool must be one of {list(DATA[a.material])}")
        vc, cl = DATA[a.material][tool]
        if a.chipload:
            cl = a.chipload
        src = f"{a.material}/{tool} (generic)"
    rpm = vc * 1000 / (math.pi * a.dia)
    feed = rpm * a.flutes * cl
    rpm_use = round(rpm / 100) * 100
    print(f"**Feeds & speeds — {a.dia}mm {a.flutes}-flute** [{src}]\n")
    print(f"Vc = {vc} m/min → RPM = Vc·1000/(π·D) = {rpm:.0f} → use ~{rpm_use} RPM")
    print(f"Chip load {cl} mm/tooth → Feed = RPM·flutes·f_z = {feed:.0f} mm/min")
    print(f"Suggested DOC ≤ {a.dia:.0f}mm axial, WOC ≤ {a.dia*0.4:.1f}mm radial (lighter when slotting/hard).")
    print("⚠️ Starting point — confirm with the tool datasheet; conservative first pass; verify on machine.")


HEADER = ["G21 ; mm", "G90 ; absolute", "G17 ; XY plane", "G54 ; work offset"]
CLEAR = 5.0


def emit(lines):
    print("```gcode")
    for l in lines:
        print(l)
    print("```")
    print("⚠️ Verify work offset (G54), tool length (G43), and dry-run/single-block before cutting.")


def cmd_face(a):
    g = list(HEADER)
    g += [f"M03 S{a.rpm} ; spindle on", f"G00 Z{CLEAR}"]
    passes = max(1, int(math.ceil(a.width / (a.dia * a.stepover))))
    step = a.width / passes
    g.append(f"G00 X0 Y0")
    g.append(f"G01 Z0 F{int(a.feed/2)} ; to surface")
    y = 0.0
    for i in range(passes + 1):
        g.append(f"G01 X{a.length:.3f} Y{y:.3f} F{a.rpm and a.feed}")
        y2 = min(y + step, a.width)
        if i < passes:
            g.append(f"G01 X{a.length:.3f} Y{y2:.3f}")
            g.append(f"G01 X0 Y{y2:.3f}")
            y3 = min(y2 + step, a.width)
            g.append(f"G01 X0 Y{y3:.3f}")
            y = y3
    g += [f"G00 Z{CLEAR}", "M05 ; spindle off", "G28 ; home", "M30"]
    print(f"**Facing {a.length}×{a.width} mm, {a.dia}mm tool, {a.stepover*100:.0f}% stepover**\n")
    emit(g)


def cmd_drill(a):
    holes = []
    for tok in a.holes.replace(";", " ").split():
        x, _, y = tok.partition(",")
        holes.append((float(x), float(y)))
    g = list(HEADER)
    g += [f"M03 S{a.rpm}", f"G00 Z{CLEAR}"]
    g.append(f"G98 G81 Z-{a.depth} R{CLEAR} F{a.feed} ; drill canned cycle")
    for x, y in holes:
        g.append(f"X{x:.3f} Y{y:.3f}")
    g += ["G80 ; cancel cycle", f"G00 Z{CLEAR}", "M05", "G28", "M30"]
    print(f"**Drilling {len(holes)} holes, depth {a.depth}mm (G81)**\n")
    emit(g)


def cmd_check(a):
    text = open(a.file, encoding="utf-8", errors="replace").read()
    xs, ys, zs = [], [], []
    units = None; absolute = None; has_spindle = False; rapids_below_clear = 0
    last_z = None
    for line in text.splitlines():
        line = line.split(";")[0].strip().upper()
        if not line:
            continue
        if "G21" in line: units = "mm"
        if "G20" in line: units = "inch"
        if "G90" in line: absolute = True
        if "G91" in line: absolute = False
        if re.search(r"M0?3|M0?4", line): has_spindle = True
        for axis, store in (("X", xs), ("Y", ys), ("Z", zs)):
            m = re.search(rf"{axis}(-?\d+\.?\d*)", line)
            if m:
                store.append(float(m.group(1)))
                if axis == "Z": last_z = float(m.group(1))
        if ("G00" in line or "G0 " in line.replace("G00","")) and last_z is not None and last_z < 0:
            rapids_below_clear += 1
    print(f"**G-code check — {a.file}**\n")
    if xs and ys:
        print(f"Bounding box: X[{min(xs):.2f}, {max(xs):.2f}]  Y[{min(ys):.2f}, {max(ys):.2f}]  "
              f"Z[{min(zs):.2f}, {max(zs):.2f}]" if zs else "")
        print(f"Travel: {max(xs)-min(xs):.2f} × {max(ys)-min(ys):.2f} mm")
    flags = []
    if units is None: flags.append("no G20/G21 — units not set (crash risk)")
    if absolute is None: flags.append("no G90/G91 — abs/inc not set")
    if not has_spindle: flags.append("no spindle start (M03/M04) found")
    if rapids_below_clear: flags.append(f"{rapids_below_clear} rapid move(s) at Z<0 — possible rapid through stock")
    if flags:
        print("\n🚩 Issues:")
        for f in flags: print(f"- {f}")
    else:
        print("\n✅ Basic checks pass (units, abs/inc, spindle set; no obvious rapids through stock).")
    print("\n_Static scan only — always simulate and dry-run on the real machine._")


def main():
    ap = argparse.ArgumentParser(description="CNC feeds/speeds + G-code helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("materials"); p.set_defaults(fn=cmd_materials)
    p = sub.add_parser("speeds"); p.add_argument("--material"); p.add_argument("--tool"); p.add_argument("--vc", type=float); p.add_argument("--dia", type=float, required=True); p.add_argument("--flutes", type=int, default=2); p.add_argument("--chipload", type=float); p.set_defaults(fn=cmd_speeds)
    p = sub.add_parser("face"); p.add_argument("--width", type=float, required=True); p.add_argument("--length", type=float, required=True); p.add_argument("--dia", type=float, required=True); p.add_argument("--stepover", type=float, default=0.5); p.add_argument("--rpm", type=int, required=True); p.add_argument("--feed", type=int, required=True); p.set_defaults(fn=cmd_face)
    p = sub.add_parser("drill"); p.add_argument("--holes", required=True, help="'x,y x,y ...'"); p.add_argument("--depth", type=float, required=True); p.add_argument("--rpm", type=int, required=True); p.add_argument("--feed", type=int, required=True); p.set_defaults(fn=cmd_drill)
    p = sub.add_parser("check"); p.add_argument("file"); p.set_defaults(fn=cmd_check)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
