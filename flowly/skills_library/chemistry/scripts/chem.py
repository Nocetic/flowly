#!/usr/bin/env python3
"""Chemistry calculator — molar mass, equation balancing, stoichiometry,
molarity, dilution. Stdlib only (fractions for exact balancing). Chat-ready.

Usage:
    chem.py mass H2SO4
    chem.py mass "Ca(OH)2"
    chem.py moles --mass 10 --formula NaCl
    chem.py balance "H2 + O2 -> H2O"
    chem.py stoich "N2 + H2 -> NH3" --given N2 --grams 28 --want NH3
    chem.py molarity --moles 0.5 --liters 2
    chem.py dilute --c1 6 --v1 0.05 --c2 1
"""
from __future__ import annotations

import argparse
import re
import sys
from fractions import Fraction

# common atomic masses (g/mol)
A = {"H": 1.008, "He": 4.003, "Li": 6.94, "Be": 9.012, "B": 10.81, "C": 12.011,
     "N": 14.007, "O": 15.999, "F": 18.998, "Ne": 20.18, "Na": 22.990, "Mg": 24.305,
     "Al": 26.982, "Si": 28.085, "P": 30.974, "S": 32.06, "Cl": 35.45, "Ar": 39.948,
     "K": 39.098, "Ca": 40.078, "Sc": 44.956, "Ti": 47.867, "V": 50.942, "Cr": 51.996,
     "Mn": 54.938, "Fe": 55.845, "Co": 58.933, "Ni": 58.693, "Cu": 63.546, "Zn": 65.38,
     "Ga": 69.723, "Ge": 72.63, "As": 74.922, "Se": 78.971, "Br": 79.904, "Kr": 83.798,
     "Rb": 85.468, "Sr": 87.62, "Y": 88.906, "Zr": 91.224, "Nb": 92.906, "Mo": 95.95,
     "Ag": 107.868, "Cd": 112.414, "Sn": 118.71, "Sb": 121.76, "I": 126.904, "Xe": 131.293,
     "Cs": 132.905, "Ba": 137.327, "Pt": 195.084, "Au": 196.967, "Hg": 200.592,
     "Pb": 207.2, "Bi": 208.98, "U": 238.029, "W": 183.84, "Pd": 106.42}

TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)|(\()|(\))(\d*)")


def parse_formula(f):
    """Return {element: count} for a chemical formula with parentheses."""
    f = f.strip()
    stack = [{}]
    i = 0
    while i < len(f):
        c = f[i]
        if c == "(":
            stack.append({})
            i += 1
        elif c == ")":
            i += 1
            num = ""
            while i < len(f) and f[i].isdigit():
                num += f[i]; i += 1
            mult = int(num) if num else 1
            top = stack.pop()
            for el, n in top.items():
                stack[-1][el] = stack[-1].get(el, 0) + n * mult
        elif c.isalpha():
            m = re.match(r"[A-Z][a-z]?", f[i:])
            el = m.group(0); i += len(el)
            num = ""
            while i < len(f) and f[i].isdigit():
                num += f[i]; i += 1
            cnt = int(num) if num else 1
            if el not in A:
                sys.exit(f"unknown element '{el}'")
            stack[-1][el] = stack[-1].get(el, 0) + cnt
        else:
            i += 1  # skip stray chars (·, spaces)
    return stack[0]


def molar_mass(f):
    return sum(A[el] * n for el, n in parse_formula(f).items())


def cmd_mass(a):
    comp = parse_formula(a.formula)
    mm = molar_mass(a.formula)
    parts = " + ".join(f"{n}×{el}({A[el]})" for el, n in comp.items())
    print(f"Molar mass of {a.formula} = {parts} = **{mm:.3f} g/mol**")


def cmd_moles(a):
    mm = molar_mass(a.formula)
    n = a.mass / mm
    print(f"{a.mass} g / {mm:.3f} g/mol ({a.formula}) = **{n:.4g} mol**")


def split_eq(eq):
    if "->" in eq:
        l, r = eq.split("->")
    elif "=" in eq:
        l, r = eq.split("=")
    else:
        sys.exit("equation needs -> or =")
    L = [s.strip() for s in l.split("+") if s.strip()]
    R = [s.strip() for s in r.split("+") if s.strip()]
    return L, R


def null_space_vector(mat):
    """Integer null-space basis vector for matrix of Fractions (rows=elements,
    cols=species). Returns list of Fractions (one solution)."""
    m = [row[:] for row in mat]
    rows = len(m); cols = len(m[0])
    pivots = []
    r = 0
    for c in range(cols):
        piv = next((i for i in range(r, rows) if m[i][c] != 0), None)
        if piv is None:
            continue
        m[r], m[piv] = m[piv], m[r]
        inv = m[r][c]
        m[r] = [x / inv for x in m[r]]
        for i in range(rows):
            if i != r and m[i][c] != 0:
                factor = m[i][c]
                m[i] = [a - factor * b for a, b in zip(m[i], m[r])]
        pivots.append(c)
        r += 1
        if r == rows:
            break
    free = [c for c in range(cols) if c not in pivots]
    if not free:
        return None
    fcol = free[0]
    vec = [Fraction(0)] * cols
    vec[fcol] = Fraction(1)
    for ri, pc in enumerate(pivots):
        vec[pc] = -m[ri][fcol]
    return vec


def cmd_balance(a):
    L, R = split_eq(a.equation)
    species = L + R
    comps = [parse_formula(s) for s in species]
    elements = sorted({el for comp in comps for el in comp})
    # matrix rows=elements; reactants +, products -
    mat = []
    for el in elements:
        row = []
        for j, comp in enumerate(comps):
            sign = 1 if j < len(L) else -1
            row.append(Fraction(sign * comp.get(el, 0)))
        mat.append(row)
    vec = null_space_vector(mat)
    if vec is None:
        sys.exit("could not balance (check the formulas / reaction)")
    # scale to positive integers
    from math import gcd
    dens = [v.denominator for v in vec]
    lcm = 1
    for d in dens:
        lcm = lcm * d // gcd(lcm, d)
    ints = [int(v * lcm) for v in vec]
    g = 0
    for x in ints:
        g = gcd(g, abs(x))
    ints = [x // g for x in ints] if g else ints
    if any(x < 0 for x in ints[:len(L)]) or sum(ints) < 0:
        ints = [-x for x in ints]
    if any(x <= 0 for x in ints):
        ints = [abs(x) for x in ints]

    def fmt(side, offset):
        return " + ".join((f"{ints[offset+i]} " if ints[offset+i] != 1 else "") + s
                          for i, s in enumerate(side))
    print(f"Balanced: **{fmt(L, 0)} → {fmt(R, len(L))}**")
    coeffs = ", ".join(f"{s}:{ints[i]}" for i, s in enumerate(species))
    print(f"Coefficients — {coeffs}")


def cmd_stoich(a):
    L, R = split_eq(a.equation)
    species = L + R
    comps = [parse_formula(s) for s in species]
    elements = sorted({el for comp in comps for el in comp})
    mat = []
    for el in elements:
        row = [Fraction((1 if j < len(L) else -1) * comp.get(el, 0)) for j, comp in enumerate(comps)]
        mat.append(row)
    vec = null_space_vector(mat)
    from math import gcd
    lcm = 1
    for v in vec:
        lcm = lcm * v.denominator // gcd(lcm, v.denominator)
    ints = [abs(int(v * lcm)) for v in vec]
    g = 0
    for x in ints:
        g = gcd(g, x)
    ints = [x // g for x in ints] if g else ints
    idx = {s: i for i, s in enumerate(species)}
    if a.given not in idx or a.want not in idx:
        sys.exit("--given/--want must be species in the equation")
    coef_g, coef_w = ints[idx[a.given]], ints[idx[a.want]]
    moles_given = a.grams / molar_mass(a.given)
    moles_want = moles_given * coef_w / coef_g
    grams_want = moles_want * molar_mass(a.want)
    print(f"Balanced ratio {a.given}:{a.want} = {coef_g}:{coef_w}")
    print(f"Moles {a.given} = {a.grams}/{molar_mass(a.given):.3f} = {moles_given:.4g} mol")
    print(f"Moles {a.want} = {moles_given:.4g} × {coef_w}/{coef_g} = {moles_want:.4g} mol")
    print(f"Mass {a.want} = {moles_want:.4g} × {molar_mass(a.want):.3f} = **{grams_want:.4g} g**")
    print("(Assumes the other reactant(s) are in excess — check the limiting reagent if not.)")


def cmd_molarity(a):
    moles = a.moles if a.moles is not None else (a.mass / molar_mass(a.formula))
    M = moles / a.liters
    print(f"Molarity = {moles:.4g} mol / {a.liters} L = **{M:.4g} M**")


def cmd_dilute(a):
    vals = [a.c1, a.v1, a.c2, a.v2]
    if vals.count(None) != 1:
        sys.exit("give exactly 3 of --c1 --v1 --c2 --v2 (C1V1=C2V2)")
    if a.v2 is None:
        print(f"V2 = C1·V1/C2 = {a.c1*a.v1/a.c2:.4g} (same vol units) → add solvent to this total")
    elif a.c2 is None:
        print(f"C2 = C1·V1/V2 = {a.c1*a.v1/a.v2:.4g} M")
    elif a.v1 is None:
        print(f"V1 = C2·V2/C1 = {a.c2*a.v2/a.c1:.4g} (take this much stock)")
    else:
        print(f"C1 = C2·V2/V1 = {a.c2*a.v2/a.v1:.4g} M")


def main():
    ap = argparse.ArgumentParser(description="Chemistry calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("mass"); p.add_argument("formula"); p.set_defaults(fn=cmd_mass)
    p = sub.add_parser("moles"); p.add_argument("--mass", type=float, required=True); p.add_argument("--formula", required=True); p.set_defaults(fn=cmd_moles)
    p = sub.add_parser("balance"); p.add_argument("equation"); p.set_defaults(fn=cmd_balance)
    p = sub.add_parser("stoich"); p.add_argument("equation"); p.add_argument("--given", required=True); p.add_argument("--grams", type=float, required=True); p.add_argument("--want", required=True); p.set_defaults(fn=cmd_stoich)
    p = sub.add_parser("molarity"); p.add_argument("--moles", type=float); p.add_argument("--mass", type=float); p.add_argument("--formula"); p.add_argument("--liters", type=float, required=True); p.set_defaults(fn=cmd_molarity)
    p = sub.add_parser("dilute"); [p.add_argument(f"--{x}", type=float) for x in ("c1", "v1", "c2", "v2")]; p.set_defaults(fn=cmd_dilute)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
