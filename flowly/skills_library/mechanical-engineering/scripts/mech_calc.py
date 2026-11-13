#!/usr/bin/env python3
"""Mechanical engineering calculator — stress, FoS, beams, buckling, bolts,
gears, pressure vessels. Stdlib only. Chat-ready markdown.

SI units throughout (N, m, Pa) EXCEPT: `stress` takes area in mm^2 and reports
MPa (convenient); `fos` takes MPa. Beam/buckling/vessel use SI (m, Pa).

Usage:
    mech_calc.py stress --force 5000 --area 100            (N, mm^2 -> MPa)
    mech_calc.py fos --stress 120 --strength 250           (MPa)
    mech_calc.py beam --case cantilever-point --p 200 --length 0.5 \
        --b 0.02 --h 0.04 --e 200e9 [--yield 250e6]
    mech_calc.py beam --case ss-uniform --w 1000 --length 1.0 --d 0.05 --e 200e9
    mech_calc.py buckling --e 200e9 --i 8.3e-9 --length 1.0 --k 1.0
    mech_calc.py bolt --torque 10 --d 0.008 [--k 0.2]
    mech_calc.py gear --teeth-in 12 --teeth-out 36 --torque-in 2 --rpm-in 1500
    mech_calc.py vessel --pressure 1e6 --radius 0.05 --t 0.002 [--yield 250e6]
"""
from __future__ import annotations

import argparse
import math


def eng_pa(pa):
    """Format a pressure/stress in Pa with a sensible unit."""
    if abs(pa) >= 1e9:
        return f"{pa/1e9:.3g} GPa"
    if abs(pa) >= 1e6:
        return f"{pa/1e6:.3g} MPa"
    if abs(pa) >= 1e3:
        return f"{pa/1e3:.3g} kPa"
    return f"{pa:.3g} Pa"


def rect_I(b, h):
    return b * h ** 3 / 12.0


def circle_I(d):
    return math.pi * d ** 4 / 64.0


def fos_line(stress_pa, limit_pa):
    f = limit_pa / stress_pa if stress_pa else float("inf")
    mark = "✅" if f >= 1.5 else ("⚠️" if f >= 1.0 else "❌ FAILS")
    return f"FoS = {eng_pa(limit_pa)} / {eng_pa(stress_pa)} = {f:.2f} {mark}"


def cmd_stress(a):
    area_m2 = a.area * 1e-6
    sigma = a.force / area_m2
    print(f"σ = F/A = {a.force} N / {a.area} mm² = {eng_pa(sigma)}")
    if a.strength is not None:
        print(fos_line(sigma, a.strength * 1e6))


def cmd_fos(a):
    print(fos_line(a.stress * 1e6, a.strength * 1e6))


BEAM = {
    # case: (moment_fn(load,L), deflection_fn(load,L,E,I))  load is P (point) or w (per length)
    "cantilever-point": (lambda P, L: P * L, lambda P, L, E, I: P * L ** 3 / (3 * E * I)),
    "cantilever-uniform": (lambda w, L: w * L ** 2 / 2, lambda w, L, E, I: w * L ** 4 / (8 * E * I)),
    "ss-point": (lambda P, L: P * L / 4, lambda P, L, E, I: P * L ** 3 / (48 * E * I)),
    "ss-uniform": (lambda w, L: w * L ** 2 / 8, lambda w, L, E, I: 5 * w * L ** 4 / (384 * E * I)),
}


def cmd_beam(a):
    if a.case not in BEAM:
        raise SystemExit(f"--case must be one of: {', '.join(BEAM)}")
    load = a.p if a.p is not None else a.w
    if load is None:
        raise SystemExit("provide --p (point load N) or --w (uniform load N/m)")
    # section
    if a.b and a.h:
        I = rect_I(a.b, a.h); c = a.h / 2
        sec = f"rectangle {a.b}×{a.h} m, I={I:.3e} m⁴"
    elif a.d:
        I = circle_I(a.d); c = a.d / 2
        sec = f"circle d={a.d} m, I={I:.3e} m⁴"
    elif a.i:
        I = a.i; c = a.c if a.c else None
        sec = f"I={I:.3e} m⁴"
    else:
        raise SystemExit("provide a section: --b/--h, or --d, or --i (+--c)")

    mfn, dfn = BEAM[a.case]
    M = mfn(load, a.length)
    delta = dfn(load, a.length, a.e, I)
    print(f"**Beam: {a.case}** ({sec})")
    print(f"Max moment M = {M:.4g} N·m")
    print(f"Max deflection δ = {delta*1000:.4g} mm")
    if c is not None:
        sigma = M * c / I
        print(f"Bending stress σ = M·c/I = {eng_pa(sigma)}")
        if a.yield_ is not None:
            print(fos_line(sigma, a.yield_))


def cmd_buckling(a):
    pcr = math.pi ** 2 * a.e * a.i / (a.k * a.length) ** 2
    print(f"Euler P_cr = π²EI/(KL)² = {pcr:.4g} N ({pcr/1000:.3g} kN)")
    if a.load is not None:
        f = pcr / a.load
        mark = "✅" if f >= 2 else ("⚠️" if f >= 1 else "❌ buckles")
        print(f"Applied {a.load} N → buckling FoS = {f:.2f} {mark}")
    print("_Valid for slender columns; stocky columns are governed by yield (σ=P/A) instead._")


def cmd_bolt(a):
    F = a.torque / (a.k * a.d)
    print(f"Preload F ≈ T/(K·d) = {a.torque} / ({a.k}·{a.d}) = {F:.4g} N ({F/1000:.3g} kN)")
    print(f"(K≈{a.k} dry-steel rule of thumb; varies a lot with lube/finish. "
          f"Target preload ≈75% of bolt proof load.)")


def cmd_gear(a):
    ratio = a.teeth_out / a.teeth_in
    print(f"Gear ratio = {a.teeth_out}/{a.teeth_in} = {ratio:.3f}:1")
    if a.torque_in is not None:
        print(f"Output torque ≈ {a.torque_in*ratio:.4g} N·m (ideal, before efficiency)")
    if a.rpm_in is not None:
        rpm_out = a.rpm_in / ratio
        print(f"Output speed = {rpm_out:.4g} RPM")
        if a.torque_in is not None:
            P = a.torque_in * a.rpm_in * 2 * math.pi / 60
            print(f"Power ≈ {P:.4g} W (conserved through the train, minus losses)")


def cmd_vessel(a):
    hoop = a.pressure * a.radius / a.t
    axial = a.pressure * a.radius / (2 * a.t)
    print(f"Thin-wall cylinder (p={eng_pa(a.pressure)}, r={a.radius} m, t={a.t} m):")
    print(f"Hoop stress σ_h = p·r/t = {eng_pa(hoop)}  ← governs")
    print(f"Axial stress σ_a = p·r/2t = {eng_pa(axial)}")
    if a.yield_ is not None:
        print(fos_line(hoop, a.yield_))
    if a.t > a.radius / 10:
        print("⚠️ t > r/10 — thin-wall assumption is weak; use thick-wall (Lamé) equations.")


def main():
    ap = argparse.ArgumentParser(description="Mechanical engineering calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stress"); p.add_argument("--force", type=float, required=True)
    p.add_argument("--area", type=float, required=True, help="mm^2"); p.add_argument("--strength", type=float, help="MPa limit")
    p.set_defaults(fn=cmd_stress)

    p = sub.add_parser("fos"); p.add_argument("--stress", type=float, required=True, help="MPa")
    p.add_argument("--strength", type=float, required=True, help="MPa"); p.set_defaults(fn=cmd_fos)

    p = sub.add_parser("beam"); p.add_argument("--case", required=True)
    p.add_argument("--p", type=float, help="point load N"); p.add_argument("--w", type=float, help="uniform load N/m")
    p.add_argument("--length", type=float, required=True, help="m")
    p.add_argument("--b", type=float); p.add_argument("--h", type=float); p.add_argument("--d", type=float)
    p.add_argument("--i", type=float, help="I directly (m^4)"); p.add_argument("--c", type=float, help="dist to fiber (m)")
    p.add_argument("--e", type=float, required=True, help="Young's modulus Pa")
    p.add_argument("--yield", dest="yield_", type=float, help="yield strength Pa"); p.set_defaults(fn=cmd_beam)

    p = sub.add_parser("buckling"); p.add_argument("--e", type=float, required=True); p.add_argument("--i", type=float, required=True)
    p.add_argument("--length", type=float, required=True); p.add_argument("--k", type=float, default=1.0)
    p.add_argument("--load", type=float, help="applied compressive load N"); p.set_defaults(fn=cmd_buckling)

    p = sub.add_parser("bolt"); p.add_argument("--torque", type=float, required=True, help="N·m")
    p.add_argument("--d", type=float, required=True, help="nominal dia m"); p.add_argument("--k", type=float, default=0.2)
    p.set_defaults(fn=cmd_bolt)

    p = sub.add_parser("gear"); p.add_argument("--teeth-in", type=int, required=True); p.add_argument("--teeth-out", type=int, required=True)
    p.add_argument("--torque-in", type=float); p.add_argument("--rpm-in", type=float); p.set_defaults(fn=cmd_gear)

    p = sub.add_parser("vessel"); p.add_argument("--pressure", type=float, required=True, help="Pa")
    p.add_argument("--radius", type=float, required=True, help="m"); p.add_argument("--t", type=float, required=True, help="m")
    p.add_argument("--yield", dest="yield_", type=float, help="yield Pa"); p.set_defaults(fn=cmd_vessel)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
