#!/usr/bin/env python3
"""Power-system sizing — motor, battery, solar PV, wire gauge. Stdlib only.
Chat-ready markdown. SI-ish units (W, V, A, Ah, m). Round up to standard parts.

Usage:
    sizing.py motor --torque 2 --rpm 1500 [--eff 0.85]
    sizing.py motor --force 200 --radius 0.05 --rpm 1500
    sizing.py battery --voltage 12 --load 60 --hours 5 [--dod 0.8] [--eff 0.9]
    sizing.py battery --voltage 48 --ah 100 --load 500           # -> runtime
    sizing.py solar --daily-wh 2000 --sun-hours 4.5 --panel 400
    sizing.py wire --current 20 --length 10 --voltage 12 [--vdrop 0.03]
"""
from __future__ import annotations

import argparse
import math

RHO_CU = 1.72e-8  # ohm·m

# AWG -> cross-sectional area (mm^2) and ~ampacity (A, chassis/conservative power wiring)
AWG = {
    0: (53.5, 150), 2: (33.6, 115), 4: (21.2, 85), 6: (13.3, 65), 8: (8.37, 50),
    10: (5.26, 35), 12: (3.31, 25), 14: (2.08, 20), 16: (1.31, 13), 18: (0.823, 10),
    20: (0.518, 7), 22: (0.326, 5), 24: (0.205, 3.5),
}


def cmd_motor(a):
    if a.torque is not None:
        tau = a.torque
    elif a.force is not None and a.radius is not None:
        tau = a.force * a.radius
    else:
        raise SystemExit("give --torque, or --force and --radius")
    omega = a.rpm * 2 * math.pi / 60
    p_mech = tau * omega
    p_elec = p_mech / a.eff
    print(f"**Motor sizing** (τ={tau:.3g} N·m, {a.rpm} RPM, η={a.eff})\n")
    print(f"ω = {omega:.3g} rad/s")
    print(f"Mechanical power P = τ·ω = {p_mech:.4g} W ({p_mech/745.7:.3g} hp)")
    print(f"Electrical input = P/η = {p_elec:.4g} W")
    print(f"→ pick a motor ≥ {p_elec*1.25:.0f} W (with ~25% margin); verify continuous (thermal) rating "
          f"and peak/stall torque.")


def cmd_battery(a):
    if a.hours is not None and a.load is not None:
        # size required capacity
        usable_wh = a.load * a.hours
        nameplate_wh = usable_wh / (a.dod * a.eff)
        ah = nameplate_wh / a.voltage
        peak_c = (a.load / a.voltage) / ah
        print(f"**Battery sizing** ({a.load} W for {a.hours} h @ {a.voltage} V)\n")
        print(f"Usable energy = {usable_wh:.4g} Wh")
        print(f"Nameplate (DoD {a.dod}, η {a.eff}) = {nameplate_wh:.4g} Wh → {ah:.3g} Ah")
        print(f"→ pick ≥ {ah*1.2:.1f} Ah {a.voltage} V (margin). Peak draw {a.load/a.voltage:.2g} A "
              f"= {peak_c:.2g}C — ensure cells support it.")
    elif a.ah is not None and a.load is not None:
        usable_wh = a.voltage * a.ah * a.dod * a.eff
        runtime = usable_wh / a.load
        print(f"**Battery runtime** ({a.voltage} V × {a.ah} Ah, {a.load} W load)\n")
        print(f"Usable energy = V·Ah·DoD·η = {usable_wh:.4g} Wh")
        print(f"Runtime ≈ {runtime:.3g} h ({runtime*60:.0f} min)")
        print(f"Peak current {a.load/a.voltage:.2g} A = {(a.load/a.voltage)/a.ah:.2g}C.")
    else:
        raise SystemExit("give (--hours and --load) to size, or (--ah and --load) for runtime")


def cmd_solar(a):
    req_wp = a.daily_wh / (a.sun_hours * a.sys_eff)
    panels = math.ceil(req_wp / a.panel)
    print(f"**Solar array sizing** ({a.daily_wh} Wh/day, {a.sun_hours} peak-sun-h, "
          f"sys eff {a.sys_eff})\n")
    print(f"Required array = daily Wh /(PSH × η) = {req_wp:.0f} Wp")
    print(f"→ {panels} × {a.panel} W panels = {panels*a.panel} W")
    print(f"Size for worst-month sun + ~0.5%/yr degradation + load growth. "
          f"For off-grid add 2–5 days battery autonomy.")


def cmd_wire(a):
    # voltage-drop constraint
    allowed_drop_v = a.vdrop * a.voltage
    # R_max = allowed_drop / I ; R = rho*2L/A -> A_min = rho*2L*I/allowed_drop
    a_min_m2 = RHO_CU * 2 * a.length * a.current / allowed_drop_v
    a_min_mm2 = a_min_m2 * 1e6
    # choose smallest AWG meeting both area (drop) and ampacity
    choice_drop = None
    choice_amp = None
    for awg in sorted(AWG, reverse=True):  # from thin (high AWG) to thick
        area, amp = AWG[awg]
        if area >= a_min_mm2 and choice_drop is None:
            choice_drop = (awg, area)
        if amp >= a.current and choice_amp is None:
            choice_amp = (awg, amp)
    # the binding (thicker = lower awg number) requirement
    print(f"**Wire sizing** ({a.current} A, {a.length} m one-way, {a.voltage} V, "
          f"≤{a.vdrop*100:.0f}% drop)\n")
    print(f"Voltage-drop budget = {allowed_drop_v:.3g} V → min area {a_min_mm2:.3g} mm²")
    if choice_drop:
        print(f"  drop-limited: ≥ AWG {choice_drop[0]} ({choice_drop[1]} mm²)")
    if choice_amp:
        print(f"  ampacity-limited: ≥ AWG {choice_amp[0]} (rated {choice_amp[1]} A)")
    # binding = lower awg number
    cands = [c[0] for c in (choice_drop, choice_amp) if c]
    if cands:
        binding = min(cands)
        why = "voltage drop" if choice_drop and binding == choice_drop[0] else "ampacity"
        print(f"\n→ Use AWG {binding} or thicker (binding constraint: {why}). Round up; "
              f"check insulation temp rating and breaker coordination.")


def main():
    ap = argparse.ArgumentParser(description="Power-system sizing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("motor"); p.add_argument("--torque", type=float); p.add_argument("--force", type=float); p.add_argument("--radius", type=float); p.add_argument("--rpm", type=float, required=True); p.add_argument("--eff", type=float, default=0.85); p.set_defaults(fn=cmd_motor)
    p = sub.add_parser("battery"); p.add_argument("--voltage", type=float, required=True); p.add_argument("--load", type=float, required=True); p.add_argument("--hours", type=float); p.add_argument("--ah", type=float); p.add_argument("--dod", type=float, default=0.8); p.add_argument("--eff", type=float, default=0.9); p.set_defaults(fn=cmd_battery)
    p = sub.add_parser("solar"); p.add_argument("--daily-wh", type=float, required=True); p.add_argument("--sun-hours", type=float, required=True); p.add_argument("--panel", type=float, required=True); p.add_argument("--sys-eff", type=float, default=0.75); p.set_defaults(fn=cmd_solar)
    p = sub.add_parser("wire"); p.add_argument("--current", type=float, required=True); p.add_argument("--length", type=float, required=True); p.add_argument("--voltage", type=float, required=True); p.add_argument("--vdrop", type=float, default=0.03); p.set_defaults(fn=cmd_wire)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
