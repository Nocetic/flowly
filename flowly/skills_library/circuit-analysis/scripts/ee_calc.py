#!/usr/bin/env python3
"""EE calculator — Ohm's law, dividers, equivalent R/C/L, RC, op-amps, dB,
E-series snapping, resistor color codes. Stdlib only. Chat-ready output.

Accepts engineering suffixes: k M G m u(µ) n p  (e.g. 3k3 -> 3300, 100n -> 1e-7).

Usage:
    ee_calc.py ohm --v 5 --r 220        (give any two of --v --i --r --p)
    ee_calc.py divider --vin 12 --r1 10k --r2 3k3
    ee_calc.py req --parallel 10k 22k 47k     |  --series 10k 22k
    ee_calc.py ceq --series 100n 220n         |  --parallel ...
    ee_calc.py rc --r 10k --c 100n
    ee_calc.py led --vs 5 --vf 2.0 --i 20m
    ee_calc.py opamp --type inverting|noninverting --rf 100k --rin 10k
    ee_calc.py db --vout 2 --vin 0.5          |  --pout --pin
    ee_calc.py eseries 150 --series E24
    ee_calc.py rcolor 220                      (value->bands)  |  rcolor red red brown (bands->value)
"""
from __future__ import annotations

import argparse
import math
import sys

SUFFIX = {"G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "": 1,
          "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12}

E_SERIES = {
    "E6": [10, 15, 22, 33, 47, 68],
    "E12": [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82],
    "E24": [10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 43, 47, 51, 56, 62, 68, 75, 82, 91],
}

COLORS = ["black", "brown", "red", "orange", "yellow", "green", "blue", "violet", "grey", "white"]
MULT = {0: "black", 1: "brown", 2: "red", 3: "orange", 4: "yellow", 5: "green",
        -1: "gold", -2: "silver"}


def eng(x):
    """Format a number with an engineering suffix."""
    if x == 0:
        return "0"
    neg = x < 0
    x = abs(x)
    for suf, mul in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if x >= mul:
            v = x / mul
            s = f"{v:.4g}{suf}"
            return ("-" + s) if neg else s
    return f"{x:.4g}"


def parse_val(s):
    s = str(s).strip()
    # forms like 3k3 (suffix in the middle) or 100n or 4.7k
    for suf in ("G", "M", "k", "K", "m", "u", "µ", "n", "p"):
        if suf in s:
            if s.endswith(suf):
                return float(s[:-1]) * SUFFIX[suf]
            a, _, b = s.partition(suf)
            if a and b and a.replace(".", "").isdigit() and b.isdigit():
                return float(a + "." + b) * SUFFIX[suf]
            if a:
                return float(a) * SUFFIX[suf]
    return float(s)


def cmd_ohm(a):
    v, i, r, p = a.v, a.i, a.r, a.p
    known = sum(x is not None for x in (v, i, r, p))
    if known < 2:
        sys.exit("provide any two of --v --i --r --p")
    if v is not None and i is not None:
        r = v / i; p = v * i
    elif v is not None and r is not None:
        i = v / r; p = v * v / r
    elif i is not None and r is not None:
        v = i * r; p = i * i * r
    elif v is not None and p is not None:
        i = p / v; r = v * v / p
    elif i is not None and p is not None:
        v = p / i; r = p / (i * i)
    elif r is not None and p is not None:
        v = math.sqrt(p * r); i = math.sqrt(p / r)
    print(f"V = {eng(v)}V · I = {eng(i)}A · R = {eng(r)}Ω · P = {eng(p)}W")


def cmd_divider(a):
    vout = a.vin * a.r2 / (a.r1 + a.r2)
    i = a.vin / (a.r1 + a.r2)
    print(f"Vout = {eng(vout)}V (Vin {eng(a.vin)}V, R1 {eng(a.r1)}Ω / R2 {eng(a.r2)}Ω)")
    print(f"Divider current = {eng(i)}A · note: loading the output lowers Vout — buffer if load R isn't ≫ R2")


def cmd_req(a):
    vals = [parse_val(x) for x in (a.series or a.parallel)]
    if a.series:
        r = sum(vals); print(f"Series R_eq = {eng(r)}Ω")
    else:
        r = 1 / sum(1 / v for v in vals); print(f"Parallel R_eq = {eng(r)}Ω")


def cmd_ceq(a):
    vals = [parse_val(x) for x in (a.series or a.parallel)]
    if a.parallel:
        c = sum(vals); print(f"Parallel C_eq = {eng(c)}F (caps add in parallel)")
    else:
        c = 1 / sum(1 / v for v in vals); print(f"Series C_eq = {eng(c)}F (caps combine reciprocally in series)")


def cmd_rc(a):
    tau = a.r * a.c
    fc = 1 / (2 * math.pi * a.r * a.c)
    print(f"τ = RC = {eng(tau)}s (settles in ~{eng(5*tau)}s) · cutoff fc = {eng(fc)}Hz")


def cmd_led(a):
    r = (a.vs - a.vf) / a.i
    p = (a.vs - a.vf) * a.i
    nearest = nearest_eseries(r, "E24")
    print(f"R = (Vs−Vf)/I = ({a.vs}−{a.vf})/{eng(a.i)} = {eng(r)}Ω")
    print(f"→ nearest E24 ≥: {eng(nearest)}Ω · power in R = {eng(p)}W "
          f"(use ≥ {eng(p*2)}W rating for margin)")


def cmd_opamp(a):
    if a.type.startswith("inv"):
        g = -a.rf / a.rin
        print(f"Inverting gain = −Rf/Rin = {g:.3f} ({20*math.log10(abs(g)):.1f} dB) · "
              f"input impedance ≈ Rin {eng(a.rin)}Ω")
    else:
        rg = a.rin
        g = 1 + a.rf / rg
        print(f"Non-inverting gain = 1+Rf/Rg = {g:.3f} ({20*math.log10(abs(g)):.1f} dB) · "
              f"very high input impedance")
    print("Check: output must stay within the rails; gain×freq must stay under the op-amp GBW.")


def cmd_db(a):
    if a.vout is not None and a.vin is not None:
        db = 20 * math.log10(a.vout / a.vin)
        print(f"Voltage gain = {a.vout/a.vin:.4g}× = {db:.2f} dB")
    elif a.pout is not None and a.pin is not None:
        db = 10 * math.log10(a.pout / a.pin)
        print(f"Power gain = {a.pout/a.pin:.4g}× = {db:.2f} dB")
    else:
        sys.exit("give --vout/--vin or --pout/--pin")


def nearest_eseries(value, series):
    if value <= 0:
        return value
    base = E_SERIES[series]
    decade = math.floor(math.log10(value))
    candidates = []
    for d in (decade - 1, decade, decade + 1):
        candidates += [b * 10 ** d / 10 for b in base]  # base is x10
    # base values are 10..91 representing 1.0..9.1
    candidates = []
    for d in (decade - 1, decade, decade + 1):
        candidates += [b / 10 * 10 ** d for b in base]
    return min(candidates, key=lambda c: abs(c - value))


def cmd_eseries(a):
    val = parse_val(a.value)
    series = a.series
    n = nearest_eseries(val, series)
    err = (n - val) / val * 100
    print(f"{eng(val)} → nearest {series}: {eng(n)} ({err:+.1f}%)")


def cmd_rcolor(a):
    toks = a.tokens
    if len(toks) == 1 and toks[0].replace(".", "").replace("k", "").replace("M", "").replace("n", "").isalnum() and any(c.isdigit() for c in toks[0]):
        # value -> bands (4-band)
        val = parse_val(toks[0])
        if val <= 0:
            sys.exit("value must be positive")
        decade = math.floor(math.log10(val))
        mult = decade - 1
        sig = round(val / 10 ** mult)
        d1, d2 = sig // 10, sig % 10
        mcolor = MULT.get(mult, f"10^{mult}")
        print(f"{eng(val)}Ω ≈ bands: {COLORS[d1]}, {COLORS[d2]}, {mcolor} (multiplier ×10^{mult}), gold (±5%)")
    else:
        # bands -> value
        try:
            d1 = COLORS.index(toks[0]); d2 = COLORS.index(toks[1]); m = COLORS.index(toks[2])
        except (ValueError, IndexError):
            sys.exit("give 3 color bands (digit, digit, multiplier), e.g. red red brown")
        val = (d1 * 10 + d2) * 10 ** m
        print(f"{toks[0]}, {toks[1]}, {toks[2]} = {eng(val)}Ω")


def main():
    ap = argparse.ArgumentParser(description="EE calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def fv(x):
        return parse_val(x)

    p = sub.add_parser("ohm"); [p.add_argument(f"--{x}", type=fv) for x in ("v", "i", "r", "p")]; p.set_defaults(fn=cmd_ohm)
    p = sub.add_parser("divider"); p.add_argument("--vin", type=fv, required=True); p.add_argument("--r1", type=fv, required=True); p.add_argument("--r2", type=fv, required=True); p.set_defaults(fn=cmd_divider)
    p = sub.add_parser("req"); g = p.add_mutually_exclusive_group(required=True); g.add_argument("--series", nargs="+"); g.add_argument("--parallel", nargs="+"); p.set_defaults(fn=cmd_req)
    p = sub.add_parser("ceq"); g = p.add_mutually_exclusive_group(required=True); g.add_argument("--series", nargs="+"); g.add_argument("--parallel", nargs="+"); p.set_defaults(fn=cmd_ceq)
    p = sub.add_parser("rc"); p.add_argument("--r", type=fv, required=True); p.add_argument("--c", type=fv, required=True); p.set_defaults(fn=cmd_rc)
    p = sub.add_parser("led"); p.add_argument("--vs", type=fv, required=True); p.add_argument("--vf", type=fv, required=True); p.add_argument("--i", type=fv, required=True); p.set_defaults(fn=cmd_led)
    p = sub.add_parser("opamp"); p.add_argument("--type", required=True); p.add_argument("--rf", type=fv, required=True); p.add_argument("--rin", type=fv, required=True); p.set_defaults(fn=cmd_opamp)
    p = sub.add_parser("db"); [p.add_argument(f"--{x}", type=fv) for x in ("vout", "vin", "pout", "pin")]; p.set_defaults(fn=cmd_db)
    p = sub.add_parser("eseries"); p.add_argument("value"); p.add_argument("--series", default="E24", choices=list(E_SERIES)); p.set_defaults(fn=cmd_eseries)
    p = sub.add_parser("rcolor"); p.add_argument("tokens", nargs="+"); p.set_defaults(fn=cmd_rcolor)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
