#!/usr/bin/env python3
"""Fluid mechanics calculator — Reynolds, flow, head loss (Darcy-Weisbach +
Swamee-Jain), pump power, Bernoulli. Stdlib only. SI units. Water defaults
(rho=998 kg/m^3, nu=1.0e-6 m^2/s). Chat-ready markdown.

Usage:
    fluid.py reynolds --v 2 --d 0.05            (or --q instead of --v)
    fluid.py flow --q 0.004 --d 0.05
    fluid.py headloss --q 0.004 --d 0.05 --l 50 [--rough 4.5e-5] [--ksum 2.0]
    fluid.py pump --q 0.004 --head 25 [--eff 0.7]
    fluid.py bernoulli --p1 200000 --v1 1 --z1 0 --v2 5 --z2 3
"""
from __future__ import annotations

import argparse
import math

G = 9.80665


def area(d):
    return math.pi * d ** 2 / 4


def regime(re):
    if re < 2300:
        return "LAMINAR"
    if re < 4000:
        return "TRANSITIONAL"
    return "TURBULENT"


def friction(re, rough, d):
    if re < 2300:
        return 64 / re, "laminar 64/Re"
    # Swamee-Jain explicit approximation to Colebrook
    f = 0.25 / (math.log10(rough / (3.7 * d) + 5.74 / re ** 0.9)) ** 2
    return f, "Swamee-Jain"


def vel_from(a):
    if a.v is not None:
        return a.v
    if a.q is not None:
        return a.q / area(a.d)
    raise SystemExit("provide --v or --q")


def cmd_reynolds(a):
    v = vel_from(a)
    re = v * a.d / a.nu
    print(f"V = {v:.4g} m/s · Re = Vd/ν = {v:.4g}·{a.d}/{a.nu:.3g} = {re:.4g} → {regime(re)}")
    if a.q is not None:
        print(f"(from Q = {a.q} m³/s through d = {a.d} m, A = {area(a.d):.4g} m²)")


def cmd_flow(a):
    A = area(a.d)
    if a.q is not None:
        v = a.q / A
        print(f"A = πd²/4 = {A:.4g} m² · V = Q/A = {a.q}/{A:.4g} = {v:.4g} m/s")
    elif a.v is not None:
        q = a.v * A
        print(f"A = {A:.4g} m² · Q = V·A = {a.v}·{A:.4g} = {q:.4g} m³/s ({q*1000:.3g} L/s)")
    else:
        raise SystemExit("provide --q or --v")


def cmd_headloss(a):
    A = area(a.d)
    v = a.q / A
    re = v * a.d / a.nu
    f, fname = friction(re, a.rough, a.d)
    hf = f * (a.l / a.d) * v ** 2 / (2 * G)
    hm = a.ksum * v ** 2 / (2 * G)
    h = hf + hm
    dp = a.rho * G * h
    print(f"V = {v:.4g} m/s · Re = {re:.4g} → {regime(re)}")
    print(f"f = {f:.4g} ({fname}, ε={a.rough} m)")
    print(f"Major loss h_f = f(L/d)(V²/2g) = {hf:.4g} m")
    if a.ksum:
        print(f"Minor loss h_m = ΣK·V²/2g (ΣK={a.ksum}) = {hm:.4g} m")
    print(f"Total head loss = {h:.4g} m · ΔP = ρg·h = {dp:.4g} Pa ({dp/1000:.3g} kPa)")
    if v > 3:
        print("⚠️ velocity > 3 m/s — consider a larger pipe (noise/erosion/steep losses).")


def cmd_pump(a):
    p_hyd = a.rho * G * a.q * a.head
    p_shaft = p_hyd / a.eff
    print(f"Hydraulic power P = ρgQH = {a.rho}·{G:.3f}·{a.q}·{a.head} = {p_hyd:.4g} W")
    print(f"Shaft power = hydraulic/η = {p_hyd:.4g}/{a.eff} = {p_shaft:.4g} W ({p_shaft/1000:.3g} kW)")
    print("(Size the motor to shaft power → power-sizing. Check NPSH to avoid cavitation.)")


def cmd_bernoulli(a):
    # P1 + ½ρV1² + ρg z1 = P2 + ½ρV2² + ρg z2  -> solve P2
    rho = a.rho
    p2 = a.p1 + 0.5 * rho * (a.v1 ** 2 - a.v2 ** 2) + rho * G * (a.z1 - a.z2)
    print(f"Bernoulli (ideal, no loss): P2 = {p2:.6g} Pa ({p2/1000:.4g} kPa)")
    print(f"(ΔP from velocity {0.5*rho*(a.v1**2-a.v2**2):.4g} Pa, "
          f"from elevation {rho*G*(a.z1-a.z2):.4g} Pa. Add −ρg·h_loss for real flow.)")


def main():
    ap = argparse.ArgumentParser(description="Fluid mechanics calculator")
    ap.add_argument("--rho", type=float, default=998.0, help="density kg/m³ (water default)")
    ap.add_argument("--nu", type=float, default=1.0e-6, help="kinematic viscosity m²/s (water default)")
    ap.add_argument("--mu", type=float, default=None, help="dynamic viscosity Pa·s (overrides nu via nu=mu/rho)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("reynolds"); p.add_argument("--v", type=float); p.add_argument("--q", type=float); p.add_argument("--d", type=float, required=True); p.set_defaults(fn=cmd_reynolds)
    p = sub.add_parser("flow"); p.add_argument("--q", type=float); p.add_argument("--v", type=float); p.add_argument("--d", type=float, required=True); p.set_defaults(fn=cmd_flow)
    p = sub.add_parser("headloss"); p.add_argument("--q", type=float, required=True); p.add_argument("--d", type=float, required=True); p.add_argument("--l", type=float, required=True); p.add_argument("--rough", type=float, default=4.5e-5); p.add_argument("--ksum", type=float, default=0.0); p.set_defaults(fn=cmd_headloss)
    p = sub.add_parser("pump"); p.add_argument("--q", type=float, required=True); p.add_argument("--head", type=float, required=True); p.add_argument("--eff", type=float, default=0.7); p.set_defaults(fn=cmd_pump)
    p = sub.add_parser("bernoulli"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("p1", "v1", "z1", "v2", "z2")]; p.set_defaults(fn=cmd_bernoulli)
    a = ap.parse_args()
    if a.mu is not None:
        a.nu = a.mu / a.rho
    a.fn(a)


if __name__ == "__main__":
    main()
