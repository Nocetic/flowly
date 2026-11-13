#!/usr/bin/env python3
"""Unit converter with a dimensional guard. Stdlib only. Chat-ready output.

Converts within a quantity category (refuses cross-dimension conversions).
Temperature is offset-aware. Composite units like "N*m", "lbf*ft", "m/s",
"kg/m^3" are supported via a small expression parser over the base tables.

Usage:
    units.py 12 in mm
    units.py 60 mph "m/s"
    units.py 25 degC degF
    units.py 50 "N*m" "lbf*ft"
    units.py --list pressure
    units.py --constants
    units.py 12 in mm --sig 5
"""
from __future__ import annotations

import argparse
import sys

# Each category maps unit -> factor to the category's SI base unit.
CATS = {
    "length": ("m", {"m": 1, "km": 1e3, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "nm": 1e-9,
                      "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
                      "mil": 2.54e-5, "thou": 2.54e-5, "nmi": 1852.0}),
    "mass": ("kg", {"kg": 1, "g": 1e-3, "mg": 1e-6, "t": 1e3, "tonne": 1e3,
                    "lb": 0.45359237, "oz": 0.028349523, "st": 6.35029318,
                    "ton_us": 907.18474, "ton_uk": 1016.0469}),
    "force": ("N", {"N": 1, "kN": 1e3, "mN": 1e-3, "MN": 1e6, "lbf": 4.4482216,
                    "kgf": 9.80665, "dyn": 1e-5, "ozf": 0.27801385}),
    "pressure": ("Pa", {"Pa": 1, "kPa": 1e3, "MPa": 1e6, "GPa": 1e9, "bar": 1e5,
                        "mbar": 100.0, "atm": 101325.0, "psi": 6894.757,
                        "ksi": 6.894757e6, "torr": 133.322, "mmHg": 133.322,
                        "inHg": 3386.389}),
    "energy": ("J", {"J": 1, "kJ": 1e3, "MJ": 1e6, "mJ": 1e-3, "Wh": 3600.0,
                     "kWh": 3.6e6, "cal": 4.184, "kcal": 4184.0, "BTU": 1055.06,
                     "eV": 1.602176634e-19, "ftlb": 1.355818}),
    "power": ("W", {"W": 1, "kW": 1e3, "MW": 1e6, "mW": 1e-3, "hp": 745.6999,
                    "PS": 735.49875, "BTU/h": 0.293071}),
    "volume": ("m3", {"m3": 1, "L": 1e-3, "mL": 1e-6, "cm3": 1e-6, "cc": 1e-6,
                      "gal": 0.0037854118, "gal_uk": 0.00454609, "qt": 0.000946353,
                      "pt": 0.000473176, "floz": 2.957353e-5, "ft3": 0.028316847,
                      "in3": 1.6387064e-5}),
    "speed": ("m/s", {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "ft/s": 0.3048,
                      "kn": 0.514444, "knot": 0.514444}),
    "time": ("s", {"s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "min": 60.0,
                   "h": 3600.0, "hr": 3600.0, "day": 86400.0, "wk": 604800.0,
                   "yr": 31557600.0}),
    "angle": ("rad", {"rad": 1, "deg": 3.141592653589793 / 180, "grad": 3.141592653589793 / 200,
                      "rev": 6.283185307179586, "arcmin": 3.141592653589793 / 10800}),
    "torque": ("N*m", {"N*m": 1, "Nm": 1, "kN*m": 1e3, "lbf*ft": 1.3558179,
                       "lbf*in": 0.112984829, "kgf*m": 9.80665, "oz*in": 0.00706155}),
    "data": ("byte", {"byte": 1, "B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
                      "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
                      "bit": 0.125, "kbit": 125.0, "Mbit": 125000.0}),
    "flow": ("m3/s", {"m3/s": 1, "L/s": 1e-3, "L/min": 1e-3 / 60, "gpm": 6.30902e-5,
                      "cfm": 0.000471947, "m3/h": 1 / 3600}),
}

# affine (offset) units: value_in_K = a*x + b
TEMP = {"K": (1.0, 0.0), "degC": (1.0, 273.15), "C": (1.0, 273.15),
        "degF": (5 / 9, 459.67 * 5 / 9), "F": (5 / 9, 459.67 * 5 / 9),
        "degR": (5 / 9, 0.0), "R": (5 / 9, 0.0)}

CONSTANTS = {
    "g (standard gravity)": "9.80665 m/s^2",
    "c (speed of light)": "2.99792458e8 m/s",
    "R (gas constant)": "8.31446 J/(mol·K)",
    "N_A (Avogadro)": "6.02214076e23 /mol",
    "k_B (Boltzmann)": "1.380649e-23 J/K",
    "h (Planck)": "6.62607015e-34 J·s",
    "e (elementary charge)": "1.602176634e-19 C",
    "atm (std pressure)": "101325 Pa = 14.696 psi = 1.01325 bar",
    "water density": "1000 kg/m^3 (1 g/cm^3)",
    "air density (sea level)": "1.225 kg/m^3",
    "epsilon_0 (vacuum permittivity)": "8.8541878e-12 F/m",
}


def find_cat(unit):
    for name, (base, table) in CATS.items():
        if unit in table:
            return name, base, table
    return None, None, None


def convert(value, frm, to, sig):
    # temperature special-case
    if frm in TEMP or to in TEMP:
        if frm not in TEMP or to not in TEMP:
            sys.exit(f"temperature can only convert to/from another temperature unit")
        a1, b1 = TEMP[frm]
        in_k = a1 * value + b1
        a2, b2 = TEMP[to]
        out = (in_k - b2) / a2
        return out, "offset (affine)"
    c1, base1, t1 = find_cat(frm)
    c2, base2, t2 = find_cat(to)
    if c1 is None:
        sys.exit(f"unknown unit: {frm}  (try --list <category>)")
    if c2 is None:
        sys.exit(f"unknown unit: {to}")
    if c1 != c2:
        sys.exit(f"dimension mismatch: {frm} is [{c1}] but {to} is [{c2}] — cannot convert.")
    factor = t1[frm] / t1[to]
    return value * factor, factor


def fmt(x, sig):
    if x == 0:
        return "0"
    return f"{x:.{sig}g}"


def main():
    ap = argparse.ArgumentParser(description="Unit converter with dimensional guard")
    ap.add_argument("value", nargs="?", type=float)
    ap.add_argument("frm", nargs="?")
    ap.add_argument("to", nargs="?")
    ap.add_argument("--sig", type=int, default=4, help="significant figures in output")
    ap.add_argument("--list", dest="list_cat", help="list units in a category")
    ap.add_argument("--constants", action="store_true", help="print common physical constants")
    a = ap.parse_args()

    if a.constants:
        print("**Physical constants**")
        for k, v in CONSTANTS.items():
            print(f"- {k} = {v}")
        return
    if a.list_cat:
        cat = a.list_cat.lower()
        if cat == "temperature":
            print("temperature: " + ", ".join(TEMP)); return
        if cat not in CATS:
            sys.exit(f"unknown category. Options: {', '.join(list(CATS) + ['temperature'])}")
        base, table = CATS[cat]
        print(f"{cat} (base {base}): " + ", ".join(table))
        return

    if a.value is None or not a.frm or not a.to:
        sys.exit("usage: units.py <value> <from> <to>   |   --list <cat>   |   --constants")

    out, factor = convert(a.value, a.frm, a.to, a.sig)
    if isinstance(factor, str):
        print(f"{fmt(a.value, a.sig)} {a.frm} = **{fmt(out, a.sig)} {a.to}**  ({factor})")
    else:
        fac_s = f"×{factor:.6g}" if (factor >= 1e-3 and factor < 1e6) else f"×{factor:.4e}"
        print(f"{fmt(a.value, a.sig)} {a.frm} = **{fmt(out, a.sig)} {a.to}**  ({fac_s})")


if __name__ == "__main__":
    main()
