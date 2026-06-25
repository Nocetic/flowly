#!/usr/bin/env python3
"""Thermodynamics & heat-transfer calculator. Stdlib only. SI units; Kelvin for
ratios/radiation. Chat-ready markdown.

Usage:
    thermo.py carnot --thot 800 --tcold 300
    thermo.py engine --qin 1000 --qout 600 [--win W]
    thermo.py cop --type fridge|heatpump --qcold Q --win W   (or --qhot for heatpump)
    thermo.py gas --p 101325 --v 0.0224 --t 273.15           (give any 3 of p,v,t,n)
    thermo.py conduction --k 0.04 --area 10 --dt 20 --l 0.1
    thermo.py convection --h 25 --area 0.5 --dt 30
    thermo.py radiation --emiss 0.9 --area 0.5 --t1 350 --t2 300
    thermo.py rnetwork --power 50 --rth 0.5 1.2 2.0 --tamb 25
"""
from __future__ import annotations

import argparse

SIGMA = 5.670374419e-8
R_GAS = 8.314462618


def warn_celsius(*temps):
    for t in temps:
        if t is not None and 0 < t < 200:
            return ("  ⚠️ temperature looks like °C — ratios/radiation need KELVIN "
                    "(K = °C + 273.15)")
    return ""


def cmd_carnot(a):
    eta = 1 - a.tcold / a.thot
    print(f"Carnot efficiency = 1 − T_c/T_h = 1 − {a.tcold}/{a.thot} = {eta*100:.1f}%"
          + warn_celsius(a.thot, a.tcold))
    print(f"(Max work per unit heat in. COP_fridge ≤ {a.tcold/(a.thot-a.tcold):.2f}, "
          f"COP_HP ≤ {a.thot/(a.thot-a.tcold):.2f}.)")


def cmd_engine(a):
    win = a.win if a.win is not None else (a.qin - a.qout)
    eta = win / a.qin
    print(f"Net work = {win:.4g} · η = W/Q_in = {win:.4g}/{a.qin:.4g} = {eta*100:.1f}%")
    print("(Compare to the Carnot limit between your reservoirs — must be below it.)")


def cmd_cop(a):
    if a.type == "fridge":
        cop = a.qcold / a.win
        print(f"Refrigerator COP = Q_cold/W = {a.qcold:.4g}/{a.win:.4g} = {cop:.2f}")
    else:
        qhot = a.qhot if a.qhot is not None else (a.qcold + a.win if a.qcold is not None else None)
        if qhot is None:
            raise SystemExit("heatpump needs --qhot or (--qcold and --win)")
        cop = qhot / a.win
        print(f"Heat-pump COP = Q_hot/W = {qhot:.4g}/{a.win:.4g} = {cop:.2f}")
    print("(COP > 1 is normal — you move more heat than the work input.)")


def cmd_gas(a):
    p, v, t, n = a.p, a.v, a.t, a.n
    known = sum(x is not None for x in (p, v, t, n))
    if known != 3:
        raise SystemExit("give exactly 3 of --p --v --t --n")
    if p is None:
        p = n * R_GAS * t / v; print(f"P = nRT/V = {p:.6g} Pa")
    elif v is None:
        v = n * R_GAS * t / p; print(f"V = nRT/P = {v:.6g} m³")
    elif t is None:
        t = p * v / (n * R_GAS); print(f"T = PV/nR = {t:.6g} K")
    else:
        n = p * v / (R_GAS * t); print(f"n = PV/RT = {n:.6g} mol" + warn_celsius(t))


def cmd_conduction(a):
    q = a.k * a.area * a.dt / a.l
    rth = a.l / (a.k * a.area)
    print(f"Conduction Q = kAΔT/L = {a.k}·{a.area}·{a.dt}/{a.l} = {q:.4g} W")
    print(f"Thermal resistance R = L/(kA) = {rth:.4g} K/W")


def cmd_convection(a):
    q = a.h * a.area * a.dt
    rth = 1 / (a.h * a.area)
    print(f"Convection Q = hAΔT = {a.h}·{a.area}·{a.dt} = {q:.4g} W")
    print(f"Thermal resistance R = 1/(hA) = {rth:.4g} K/W")


def cmd_radiation(a):
    q = a.emiss * SIGMA * a.area * (a.t1 ** 4 - a.t2 ** 4)
    print(f"Radiation Q = εσA(T₁⁴−T₂⁴) = {q:.4g} W" + warn_celsius(a.t1, a.t2))


def cmd_rnetwork(a):
    total = sum(a.rth)
    dt = a.power * total
    tj = a.tamb + dt
    print(f"Series R_th = {' + '.join(str(r) for r in a.rth)} = {total:.4g} K/W")
    print(f"ΔT = P·ΣR = {a.power}·{total:.4g} = {dt:.4g} K")
    print(f"Junction/hot-end temp = T_ambient + ΔT = {a.tamb} + {dt:.4g} = {tj:.4g} °C")
    if a.tjmax is not None:
        margin = a.tjmax - tj
        print(f"vs Tj_max {a.tjmax}°C → margin {margin:+.1f}°C "
              + ("✅" if margin > 0 else "❌ OVER LIMIT"))


def main():
    ap = argparse.ArgumentParser(description="Thermodynamics & heat-transfer calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("carnot"); p.add_argument("--thot", type=float, required=True); p.add_argument("--tcold", type=float, required=True); p.set_defaults(fn=cmd_carnot)
    p = sub.add_parser("engine"); p.add_argument("--qin", type=float, required=True); p.add_argument("--qout", type=float, required=True); p.add_argument("--win", type=float); p.set_defaults(fn=cmd_engine)
    p = sub.add_parser("cop"); p.add_argument("--type", choices=["fridge", "heatpump"], required=True); p.add_argument("--qcold", type=float); p.add_argument("--qhot", type=float); p.add_argument("--win", type=float, required=True); p.set_defaults(fn=cmd_cop)
    p = sub.add_parser("gas"); [p.add_argument(f"--{x}", type=float) for x in ("p", "v", "t", "n")]; p.set_defaults(fn=cmd_gas)
    p = sub.add_parser("conduction"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("k", "area", "dt", "l")]; p.set_defaults(fn=cmd_conduction)
    p = sub.add_parser("convection"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("h", "area", "dt")]; p.set_defaults(fn=cmd_convection)
    p = sub.add_parser("radiation"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("emiss", "area", "t1", "t2")]; p.set_defaults(fn=cmd_radiation)
    p = sub.add_parser("rnetwork"); p.add_argument("--power", type=float, required=True); p.add_argument("--rth", type=float, nargs="+", required=True); p.add_argument("--tamb", type=float, required=True); p.add_argument("--tjmax", type=float); p.set_defaults(fn=cmd_rnetwork)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
