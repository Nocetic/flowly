#!/usr/bin/env python3
"""Materials selection helper — property database + Ashby-index ranking.
Stdlib only. Chat-ready markdown. Property values are typical mid-range
guidance; verify the exact grade against a datasheet.

Units: density rho kg/m^3, E (Young's) GPa, yield MPa, max service temp °C,
cost relative $/kg (approx).

Usage:
    materials.py list
    materials.py show steel-mild aluminium-6061 titanium-ti6al4v cfrp
    materials.py compare aluminium-6061 steel-mild [--props density,E,yield,cost]
    materials.py rank --index stiff-beam [--top 5]
    materials.py rank --index "E/rho"
"""
from __future__ import annotations

import argparse
import sys

# name: (rho kg/m3, E GPa, yield MPa, Tmax C, cost $/kg approx)
MAT = {
    "steel-mild":        (7850, 210, 250, 450, 1.0),
    "steel-stainless316":(8000, 193, 290, 800, 4.5),
    "aluminium-6061":    (2700, 69, 276, 150, 3.0),
    "aluminium-7075":    (2810, 72, 503, 120, 5.0),
    "titanium-ti6al4v":  (4430, 114, 880, 400, 35.0),
    "magnesium-az31":    (1770, 45, 200, 120, 6.0),
    "copper":            (8960, 117, 70, 200, 9.0),
    "brass":             (8500, 100, 200, 250, 7.0),
    "cast-iron":         (7200, 130, 130, 500, 0.8),
    "abs":               (1050, 2.3, 40, 80, 2.5),
    "pla":               (1240, 3.5, 50, 55, 3.0),
    "nylon-66":          (1140, 2.8, 80, 100, 4.0),
    "pc-polycarbonate":  (1200, 2.4, 62, 120, 5.0),
    "peek":              (1300, 3.6, 95, 250, 90.0),
    "cfrp":              (1600, 70, 600, 180, 40.0),   # quasi-isotropic typical
    "gfrp":              (1900, 25, 350, 150, 8.0),
    "alumina-ceramic":   (3900, 370, 300, 1500, 20.0),  # 'yield' = flexural strength
    "glass":             (2500, 70, 50, 500, 2.0),
    "wood-oak":          (700, 11, 40, 100, 2.0),
    "concrete":          (2400, 30, 5, 800, 0.1),
}

PROPS = ["density", "E", "yield", "Tmax", "cost"]
IDX = {0: "density", 1: "E", 2: "yield", 3: "Tmax", 4: "cost"}

# index name -> function(rho_SI, E_Pa, yield_Pa) -> value (higher = better)
def _make_indices():
    return {
        "stiff-tie":   ("E/ρ (specific stiffness)", lambda r, E, s: E / r),
        "strong-tie":  ("σ/ρ (specific strength)",  lambda r, E, s: s / r),
        "stiff-beam":  ("E^½/ρ (light stiff beam)", lambda r, E, s: E ** 0.5 / r * 1e3),
        "strong-beam": ("σ^⅔/ρ (light strong beam)", lambda r, E, s: s ** (2/3) / r * 1e3),
        "stiff-panel": ("E^⅓/ρ (light stiff panel)", lambda r, E, s: E ** (1/3) / r * 1e4),
        "spring":      ("σ²/E (resilience)",         lambda r, E, s: s ** 2 / E),
    }


def props_si(name):
    rho, E, y, t, c = MAT[name]
    return rho, E * 1e9, y * 1e6, t, c


def cmd_list(a):
    print("**Materials** (" + str(len(MAT)) + "):")
    print(", ".join(MAT))
    print("\n**Indices:** " + ", ".join(_make_indices()) + " (or a custom expr like \"E/rho\", \"yield/rho\")")


def cmd_show(a):
    names = a.names
    bad = [n for n in names if n not in MAT]
    if bad:
        sys.exit(f"unknown: {', '.join(bad)} — see `materials.py list`")
    print("| Material | ρ (kg/m³) | E (GPa) | yield (MPa) | Tmax (°C) | cost ($/kg) |")
    print("|---|---|---|---|---|---|")
    for n in names:
        rho, E, y, t, c = MAT[n]
        print(f"| {n} | {rho} | {E} | {y} | {t} | {c} |")


def cmd_compare(a):
    cmd_show(argparse.Namespace(names=a.names))
    # specific-property quick read
    print("\nSpecific properties (per density):")
    print("| Material | E/ρ | σ/ρ |")
    print("|---|---|---|")
    for n in a.names:
        rho, E, y, t, c = MAT[n]
        print(f"| {n} | {E*1e9/rho:,.0f} | {y*1e6/rho:,.0f} |")


def eval_custom(expr, rho, E, y):
    # allow rho, density, E, yield, sigma; SI units
    env = {"rho": rho, "density": rho, "E": E, "yield": y, "sigma": y, "__builtins__": {}}
    try:
        return eval(expr, env)  # noqa: S307 — fixed safe env, no builtins
    except Exception as e:
        sys.exit(f"bad index expression '{expr}': {e}")


def cmd_rank(a):
    indices = _make_indices()
    rows = []
    if a.index in indices:
        label, fn = indices[a.index]
        for n in MAT:
            rho, E, y, t, c = props_si(n)
            rows.append((fn(rho, E, y), n))
    else:
        label = f"custom: {a.index}"
        for n in MAT:
            rho, E, y, t, c = props_si(n)
            rows.append((eval_custom(a.index, rho, E, y), n))
    rows.sort(reverse=True)
    mx = rows[0][0] if rows else 1
    print(f"**Material ranking — index {label}** (higher = better)\n")
    print("| # | Material | index | rel |")
    print("|---|---|---|---|")
    for i, (v, n) in enumerate(rows[:a.top], 1):
        print(f"| {i} | {n} | {v:.4g} | {v/mx:.2f} |")
    print("\n_Typical values — verify the exact grade; then screen by temp/corrosion/toughness/cost/fatigue._")


def main():
    ap = argparse.ArgumentParser(description="Materials selection helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("names", nargs="+"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("compare"); p.add_argument("names", nargs="+"); p.add_argument("--props", default=",".join(PROPS)); p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("rank"); p.add_argument("--index", required=True); p.add_argument("--top", type=int, default=8); p.set_defaults(fn=cmd_rank)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
