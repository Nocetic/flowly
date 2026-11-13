#!/usr/bin/env python3
"""Epidemiology — compartmental models and outbreak metrics. Stdlib only.

SIR / SEIR simulation (RK4), basic reproduction number R0 and the derived
herd-immunity threshold and final epidemic size, and growth/doubling-time math.
These are teaching/planning models — deterministic, well-mixed, closed
population; not a calibrated forecast.

Usage:
    epi.py sir  --beta 0.4 --gamma 0.1 --N 1000000 --I0 10 --days 160
    epi.py seir --beta 0.6 --sigma 0.2 --gamma 0.1 --N 1000000 --I0 10 --days 200
    epi.py r0   --beta 0.4 --gamma 0.1          # or: --r0 2.5  /  --doubling 3 --gamma 0.1
    epi.py doubling --rate 0.23                 # per-day growth rate  → doubling time
    epi.py doubling --c1 100 --c2 800 --days 6  # two case counts N days apart
"""
from __future__ import annotations

import argparse
import math


def _rk4(deriv, y0, t_end, dt=0.1):
    """Fixed-step RK4. Returns list of (t, state) at whole-day marks."""
    y = list(y0)
    t = 0.0
    out = [(0.0, list(y))]
    steps = int(round(t_end / dt))
    for i in range(1, steps + 1):
        k1 = deriv(y)
        k2 = deriv([y[j] + dt / 2 * k1[j] for j in range(len(y))])
        k3 = deriv([y[j] + dt / 2 * k2[j] for j in range(len(y))])
        k4 = deriv([y[j] + dt * k3[j] for j in range(len(y))])
        y = [y[j] + dt / 6 * (k1[j] + 2 * k2[j] + 2 * k3[j] + k4[j]) for j in range(len(y))]
        t = i * dt
        if abs(t - round(t)) < dt / 2:
            out.append((round(t), list(y)))
    return out


def _final_size(r0):
    """Solve the final-size equation Z = 1 - exp(-R0 Z) for attack fraction Z."""
    if r0 <= 1:
        return 0.0
    z = 0.5
    for _ in range(100):
        z = 1 - math.exp(-r0 * z)
    return z


def cmd_sir(a):
    N, beta, gamma = a.N, a.beta, a.gamma

    def deriv(y):
        S, I, R = y
        inf = beta * S * I / N
        rec = gamma * I
        return [-inf, inf - rec, rec]

    traj = _rk4(deriv, [N - a.I0, a.I0, 0.0], a.days)
    peak_t, peak = max(((t, y[1]) for t, y in traj), key=lambda p: p[1])
    r0 = beta / gamma
    final_R = traj[-1][1][2]
    print(f"**SIR model** — β={beta}, γ={gamma}, N={N:,}, I₀={a.I0}, {a.days} days\n")
    print(f"R₀ = β/γ = {r0:.2f}   (infectious period ≈ {1/gamma:.1f} days)")
    print(f"Peak infected ≈ {peak:,.0f} on day {int(peak_t)} "
          f"({peak/N*100:.1f}% infected at once)")
    print(f"Cumulative infected by day {a.days} ≈ {final_R:,.0f} ({final_R/N*100:.1f}%)")
    print(f"Predicted final size (attack rate) ≈ {_final_size(r0)*100:.1f}%")
    if r0 > 1:
        print(f"Herd-immunity threshold = 1 − 1/R₀ = {(1-1/r0)*100:.1f}% immune to halt growth")
    else:
        print("R₀ ≤ 1 — the outbreak dies out without taking off.")


def cmd_seir(a):
    N, beta, sigma, gamma = a.N, a.beta, a.sigma, a.gamma

    def deriv(y):
        S, E, I, R = y
        inf = beta * S * I / N
        prog = sigma * E
        rec = gamma * I
        return [-inf, inf - prog, prog - rec, rec]

    traj = _rk4(deriv, [N - a.I0, 0.0, a.I0, 0.0], a.days)
    peak_t, peak = max(((t, y[2]) for t, y in traj), key=lambda p: p[1])
    r0 = beta / gamma
    final_R = traj[-1][1][3]
    print(f"**SEIR model** — β={beta}, σ={sigma}, γ={gamma}, N={N:,}, I₀={a.I0}, {a.days} days\n")
    print(f"R₀ = β/γ = {r0:.2f}   (latent ≈ {1/sigma:.1f} d, infectious ≈ {1/gamma:.1f} d)")
    print(f"Peak infectious ≈ {peak:,.0f} on day {int(peak_t)} ({peak/N*100:.1f}%)")
    print(f"Cumulative infected by day {a.days} ≈ {final_R:,.0f} ({final_R/N*100:.1f}%)")
    print(f"Predicted final size ≈ {_final_size(r0)*100:.1f}%")
    print("(SEIR's latent period delays and slightly flattens the peak vs SIR.)")


def cmd_r0(a):
    if a.r0 is not None:
        r0 = a.r0
        basis = f"given R₀ = {r0}"
    elif a.doubling is not None and a.gamma is not None:
        growth = math.log(2) / a.doubling
        r0 = 1 + growth / a.gamma
        basis = f"from doubling time {a.doubling} d and γ={a.gamma}: R₀ = 1 + r/γ"
    elif a.beta is not None and a.gamma is not None:
        r0 = a.beta / a.gamma
        basis = f"β/γ = {a.beta}/{a.gamma}"
    else:
        raise SystemExit("give --beta+--gamma, or --r0, or --doubling+--gamma")
    print("**Reproduction number**\n")
    print(f"R₀ = {r0:.2f}   ({basis})")
    if r0 > 1:
        print(f"Herd-immunity threshold = 1 − 1/R₀ = {(1-1/r0)*100:.1f}% must be immune")
        print(f"Final attack rate (unmitigated) ≈ {_final_size(r0)*100:.1f}%")
        print(f"Effective R drops below 1 once susceptibles fall below {1/r0*100:.1f}%")
    else:
        print("R₀ ≤ 1 — each case replaces less than itself; the outbreak fades.")


def cmd_doubling(a):
    if a.rate is not None:
        r = a.rate
        src = f"growth rate {r}/day"
    elif None not in (a.c1, a.c2, a.days):
        r = math.log(a.c2 / a.c1) / a.days
        src = f"{a.c1} → {a.c2} over {a.days} days"
    else:
        raise SystemExit("give --rate, or --c1 --c2 --days")
    print(f"**Doubling time** — {src}\n")
    if r == 0:
        print("Cases flat (r = 0/day) — neither doubling nor halving.")
        return
    if r < 0:
        print(f"Cases falling (r = {r:.3f}/day); halving time = "
              f"{math.log(2)/abs(r):.1f} days.")
        return
    print(f"Growth rate r = {r:.3f}/day")
    print(f"Doubling time = ln2 / r = {math.log(2)/r:.1f} days")
    print(f"That's ×{math.exp(r*7):.1f} per week if the rate holds.")


def main():
    ap = argparse.ArgumentParser(description="Epidemiology models (stdlib)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sir"); p.add_argument("--beta", type=float, required=True); p.add_argument("--gamma", type=float, required=True); p.add_argument("--N", type=float, default=1e6); p.add_argument("--I0", type=float, default=10); p.add_argument("--days", type=float, default=160); p.set_defaults(fn=cmd_sir)
    p = sub.add_parser("seir"); p.add_argument("--beta", type=float, required=True); p.add_argument("--sigma", type=float, required=True); p.add_argument("--gamma", type=float, required=True); p.add_argument("--N", type=float, default=1e6); p.add_argument("--I0", type=float, default=10); p.add_argument("--days", type=float, default=200); p.set_defaults(fn=cmd_seir)
    p = sub.add_parser("r0"); p.add_argument("--beta", type=float); p.add_argument("--gamma", type=float); p.add_argument("--r0", type=float); p.add_argument("--doubling", type=float); p.set_defaults(fn=cmd_r0)
    p = sub.add_parser("doubling"); p.add_argument("--rate", type=float); p.add_argument("--c1", type=float); p.add_argument("--c2", type=float); p.add_argument("--days", type=float); p.set_defaults(fn=cmd_doubling)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
