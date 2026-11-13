#!/usr/bin/env python3
"""Control-systems calculator — Routh-Hurwitz stability, 2nd-order response
metrics, Ziegler-Nichols PID, complex poles. Stdlib only. Chat-ready markdown.

Usage:
    control_calc.py routh 1 6 11 6           # coeffs high->low: s^3+6s^2+11s+6
    control_calc.py response --wn 10 --zeta 0.5
    control_calc.py response --wn 10 --os 0.10   # derive zeta from target %overshoot
    control_calc.py pid --ku 6 --tu 0.5
    control_calc.py poles2 --wn 10 --zeta 0.5
"""
from __future__ import annotations

import argparse
import math


def routh(coeffs):
    """Return (stable, first_column, sign_changes) for poly with coeffs high->low."""
    n = len(coeffs)
    # build rows
    rows = [[], []]
    rows[0] = coeffs[0::2]
    rows[1] = coeffs[1::2]
    # pad
    width = max(len(rows[0]), len(rows[1]))
    rows[0] += [0] * (width - len(rows[0]))
    rows[1] += [0] * (width - len(rows[1]))
    for i in range(2, n):
        prev, prev2 = rows[i - 1], rows[i - 2]
        a = prev[0]
        if a == 0:
            a = 1e-12  # epsilon method for a zero in the first column
        new = []
        for j in range(width - 1):
            b1 = prev2[j + 1] if j + 1 < len(prev2) else 0
            b2 = prev[j + 1] if j + 1 < len(prev) else 0
            new.append((a * b1 - prev2[0] * b2) / a)
        new += [0] * (width - len(new))
        rows.append(new)
    first_col = [r[0] for r in rows]
    # sign changes among nonzero entries
    signs = [x for x in first_col if abs(x) > 1e-15]
    changes = sum(1 for i in range(1, len(signs)) if (signs[i] > 0) != (signs[i - 1] > 0))
    return changes == 0, first_col, changes


def cmd_routh(a):
    coeffs = a.coeffs
    print(f"**Routh-Hurwitz** — polynomial coeffs (high→low): {coeffs}\n")
    # necessary condition
    if any(c == 0 for c in coeffs) or not (all(c > 0 for c in coeffs) or all(c < 0 for c in coeffs)):
        print("Necessary condition fails (missing or sign-varying coefficient) → **UNSTABLE** ❌")
        # still show array for info
    stable, fc, changes = routh(coeffs)
    fc_str = ", ".join(f"{x:.4g}" for x in fc)
    print(f"First column: [{fc_str}]")
    if stable:
        print(f"No sign changes → **STABLE** ✅ (0 right-half-plane poles)")
    else:
        print(f"{changes} sign change(s) → **UNSTABLE** ❌ ({changes} pole(s) in the right-half plane)")


def zeta_from_os(os_frac):
    """Damping ratio from fractional overshoot (e.g. 0.10)."""
    if os_frac <= 0:
        return 1.0
    ln = math.log(os_frac)
    return -ln / math.sqrt(math.pi ** 2 + ln ** 2)


def cmd_response(a):
    wn = a.wn
    if a.zeta is not None:
        zeta = a.zeta
    elif a.os is not None:
        zeta = zeta_from_os(a.os)
    else:
        raise SystemExit("provide --zeta or --os")

    print(f"**Second-order response** — ωn={wn} rad/s, ζ={zeta:.3f}\n")
    if zeta < 1:
        regime = "underdamped"
    elif abs(zeta - 1) < 1e-9:
        regime = "critically damped"
    else:
        regime = "overdamped"
    print(f"Regime: {regime}")
    if zeta < 1:
        os = math.exp(-zeta * math.pi / math.sqrt(1 - zeta ** 2)) * 100
        wd = wn * math.sqrt(1 - zeta ** 2)
        tp = math.pi / wd
        print(f"Overshoot: {os:.1f}%")
        print(f"Damped frequency ωd: {wd:.3g} rad/s · Peak time: {tp:.3g} s")
    else:
        print("Overshoot: 0% (no oscillation)")
    ts = 4 / (zeta * wn)
    tr = 1.8 / wn
    print(f"Settling time (2%): ≈ {ts:.3g} s · Rise time: ≈ {tr:.3g} s")
    if zeta < 1:
        print(f"Poles: {-zeta*wn:.3g} ± {wn*math.sqrt(1-zeta**2):.3g}j")
    if 0.6 <= zeta <= 0.8:
        print("ζ is in the sweet spot (~0.7): responsive with modest overshoot.")


def cmd_pid(a):
    ku, tu = a.ku, a.tu
    print(f"**Ziegler-Nichols PID** — Ku={ku}, Tu={tu}s\n")
    print("| Controller | Kp | Ki | Kd |")
    print("|---|---|---|---|")
    p = 0.5 * ku
    print(f"| P | {p:.4g} | — | — |")
    kp = 0.45 * ku; ki = 1.2 * kp / tu
    print(f"| PI | {kp:.4g} | {ki:.4g} | — |")
    kp = 0.6 * ku; ki = 2 * kp / tu; kd = kp * tu / 8
    print(f"| PID | {kp:.4g} | {ki:.4g} | {kd:.4g} |")
    print("\n_Z-N is an aggressive starting point (~25% overshoot). Refine: P for speed, "
          "I to remove offset, D to tame overshoot — one at a time._")


def cmd_poles2(a):
    wn, zeta = a.wn, a.zeta
    if zeta < 1:
        wd = wn * math.sqrt(1 - zeta ** 2)
        print(f"Complex poles: {-zeta*wn:.4g} ± {wd:.4g}j (stable: real part < 0 ⇒ {'yes ✅' if zeta>0 else 'no'})")
    else:
        d = wn * math.sqrt(zeta ** 2 - 1)
        print(f"Real poles: {-zeta*wn + d:.4g}, {-zeta*wn - d:.4g}")


def main():
    ap = argparse.ArgumentParser(description="Control-systems calculator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("routh"); p.add_argument("coeffs", nargs="+", type=float); p.set_defaults(fn=cmd_routh)
    p = sub.add_parser("response"); p.add_argument("--wn", type=float, required=True)
    p.add_argument("--zeta", type=float); p.add_argument("--os", type=float, help="target fractional overshoot, e.g. 0.10")
    p.set_defaults(fn=cmd_response)
    p = sub.add_parser("pid"); p.add_argument("--ku", type=float, required=True); p.add_argument("--tu", type=float, required=True)
    p.set_defaults(fn=cmd_pid)
    p = sub.add_parser("poles2"); p.add_argument("--wn", type=float, required=True); p.add_argument("--zeta", type=float, required=True)
    p.set_defaults(fn=cmd_poles2)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
