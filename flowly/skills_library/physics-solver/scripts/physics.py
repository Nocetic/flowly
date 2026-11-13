#!/usr/bin/env python3
"""Physics solver — kinematics (SUVAT), projectile, energy, momentum.
Stdlib only. SI units, g=9.81 m/s^2. Chat-ready markdown.

Usage:
    physics.py kinematics --u 0 --a 9.81 --t 3        (give any 3 of u,v,a,t,s)
    physics.py projectile --v0 30 --angle 40
    physics.py energy --mass 2 --height 10
    physics.py momentum --m1 2 --v1 3 --m2 1 --v2 -1 --type inelastic
"""
from __future__ import annotations

import argparse
import math

G = 9.81


def cmd_kinematics(a):
    u, v, acc, t, s = a.u, a.v, a.a, a.t, a.s
    known = sum(x is not None for x in (u, v, acc, t, s))
    if known < 3:
        raise SystemExit("give any 3 of --u --v --a --t --s")
    # iteratively solve using SUVAT relations
    for _ in range(6):
        if v is None and None not in (u, acc, t): v = u + acc * t
        if s is None and None not in (u, acc, t): s = u * t + 0.5 * acc * t * t
        if v is None and None not in (u, acc, s):
            disc = u * u + 2 * acc * s
            if disc >= 0: v = math.sqrt(disc)
        if u is None and None not in (v, acc, t): u = v - acc * t
        if u is None and None not in (v, acc, s):
            disc = v * v - 2 * acc * s
            if disc >= 0: u = math.sqrt(disc)
        if acc is None and None not in (u, v, t) and t: acc = (v - u) / t
        if acc is None and None not in (u, v, s) and s: acc = (v * v - u * u) / (2 * s)
        if t is None and None not in (u, v, acc) and acc: t = (v - u) / acc
        if t is None and None not in (u, v, s) and (u + v): t = 2 * s / (u + v)
        if s is None and None not in (u, v, t): s = 0.5 * (u + v) * t
    print("**Kinematics (constant acceleration)**\n")
    for name, val, unit in (("u (initial v)", u, "m/s"), ("v (final v)", v, "m/s"),
                            ("a", acc, "m/s²"), ("t", t, "s"), ("s (displacement)", s, "m")):
        flag = "" if val is not None else " (unresolved)"
        print(f"  {name} = {f'{val:.4g} {unit}' if val is not None else '?'}{flag}")


def cmd_projectile(a):
    th = math.radians(a.angle)
    vx = a.v0 * math.cos(th)
    vy = a.v0 * math.sin(th)
    h0 = a.height
    # time to land (y = h0 + vy t - 0.5 g t^2 = 0)
    disc = vy * vy + 2 * G * h0
    t = (vy + math.sqrt(disc)) / G
    rng = vx * t
    hmax = h0 + vy * vy / (2 * G)
    print(f"**Projectile** (v0={a.v0} m/s, angle={a.angle}°, launch height {h0} m)\n")
    print(f"vx = {vx:.4g} m/s, vy = {vy:.4g} m/s")
    print(f"Time of flight = {t:.4g} s")
    print(f"Range = {rng:.4g} m")
    print(f"Max height = {hmax:.4g} m")
    if h0 == 0:
        print("(45° maximizes range for level ground.)")


def cmd_energy(a):
    pe = a.mass * G * a.height
    print(f"**Energy** (m={a.mass} kg, h={a.height} m)\n")
    print(f"Gravitational PE = mgh = {pe:.4g} J")
    if a.v is not None:
        ke = 0.5 * a.mass * a.v ** 2
        print(f"KE at v={a.v} m/s = ½mv² = {ke:.4g} J · total = {pe+ke:.4g} J")
    else:
        v_bottom = math.sqrt(2 * G * a.height)
        print(f"If it falls from rest: speed at bottom = √(2gh) = {v_bottom:.4g} m/s "
              f"(KE = {pe:.4g} J, energy conserved, no friction)")


def cmd_momentum(a):
    p_init = a.m1 * a.v1 + a.m2 * a.v2
    print(f"**Momentum / collision** ({a.type})\n")
    print(f"Initial p = m1v1 + m2v2 = {a.m1*a.v1:.4g} + {a.m2*a.v2:.4g} = {p_init:.4g} kg·m/s")
    if a.type == "inelastic":
        vf = p_init / (a.m1 + a.m2)
        ke_i = 0.5 * a.m1 * a.v1 ** 2 + 0.5 * a.m2 * a.v2 ** 2
        ke_f = 0.5 * (a.m1 + a.m2) * vf ** 2
        print(f"Perfectly inelastic (stick): vf = p/(m1+m2) = {vf:.4g} m/s")
        print(f"KE: {ke_i:.4g} → {ke_f:.4g} J (lost {ke_i-ke_f:.4g} J to deformation/heat)")
    else:
        # elastic 1D
        m1, m2, u1, u2 = a.m1, a.m2, a.v1, a.v2
        v1 = ((m1 - m2) * u1 + 2 * m2 * u2) / (m1 + m2)
        v2 = ((m2 - m1) * u2 + 2 * m1 * u1) / (m1 + m2)
        print(f"Elastic (KE conserved): v1' = {v1:.4g} m/s, v2' = {v2:.4g} m/s")
        print(f"Check p after = {m1*v1 + m2*v2:.4g} (= initial ✅)")


def main():
    ap = argparse.ArgumentParser(description="Physics solver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("kinematics"); [p.add_argument(f"--{x}", type=float) for x in ("u", "v", "a", "t", "s")]; p.set_defaults(fn=cmd_kinematics)
    p = sub.add_parser("projectile"); p.add_argument("--v0", type=float, required=True); p.add_argument("--angle", type=float, required=True); p.add_argument("--height", type=float, default=0.0); p.set_defaults(fn=cmd_projectile)
    p = sub.add_parser("energy"); p.add_argument("--mass", type=float, required=True); p.add_argument("--height", type=float, required=True); p.add_argument("--v", type=float); p.set_defaults(fn=cmd_energy)
    p = sub.add_parser("momentum"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("m1", "v1", "m2", "v2")]; p.add_argument("--type", choices=["elastic", "inelastic"], default="inelastic"); p.set_defaults(fn=cmd_momentum)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
