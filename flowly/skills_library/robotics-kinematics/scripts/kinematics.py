#!/usr/bin/env python3
"""Robot kinematics calculator — 2-link planar FK/IK/Jacobian and general
DH-chain forward kinematics. Stdlib only. Angles in DEGREES by default (--rad
for radians). Chat-ready markdown.

Usage:
    kinematics.py fk2 --l1 1 --l2 0.5 --t1 30 --t2 45
    kinematics.py ik2 --l1 1 --l2 0.5 --x 1.2 --y 0.5
    kinematics.py jacobian2 --l1 1 --l2 0.5 --t1 30 --t2 45
    kinematics.py dh --row "30 0 1 0" --row "45 0 0.5 0"   # theta d a alpha (deg for angles)
"""
from __future__ import annotations

import argparse
import math


def deg2rad(x, is_rad):
    return x if is_rad else math.radians(x)


def rad2deg(x, is_rad):
    return x if is_rad else math.degrees(x)


def cmd_fk2(a):
    t1 = deg2rad(a.t1, a.rad); t2 = deg2rad(a.t2, a.rad)
    x = a.l1 * math.cos(t1) + a.l2 * math.cos(t1 + t2)
    y = a.l1 * math.sin(t1) + a.l2 * math.sin(t1 + t2)
    phi = t1 + t2
    print(f"**2-link FK** (L1={a.l1}, L2={a.l2}, θ1={a.t1}, θ2={a.t2}{'rad' if a.rad else '°'})\n")
    print(f"Tip position: x = {x:.4f}, y = {y:.4f}")
    print(f"Tip orientation φ = θ1+θ2 = {rad2deg(phi, a.rad):.3f}{'rad' if a.rad else '°'}")
    print(f"Reach used: {math.hypot(x, y):.4f} / {a.l1+a.l2:.4f} max")


def cmd_ik2(a):
    x, y = a.x, a.y
    r = math.hypot(x, y)
    rmin, rmax = abs(a.l1 - a.l2), a.l1 + a.l2
    print(f"**2-link IK** (target ({x}, {y}), L1={a.l1}, L2={a.l2})\n")
    print(f"r = {r:.4f} · reachable range [{rmin:.4f}, {rmax:.4f}]", end=" ")
    if not (rmin - 1e-9 <= r <= rmax + 1e-9):
        print("→ ❌ UNREACHABLE")
        return
    print("→ reachable ✅")
    c2 = (r * r - a.l1 ** 2 - a.l2 ** 2) / (2 * a.l1 * a.l2)
    c2 = max(-1.0, min(1.0, c2))
    for sign, label in ((1, "elbow-down"), (-1, "elbow-up")):
        t2 = sign * math.acos(c2)
        t1 = math.atan2(y, x) - math.atan2(a.l2 * math.sin(t2), a.l1 + a.l2 * math.cos(t2))
        # FK verify
        xv = a.l1 * math.cos(t1) + a.l2 * math.cos(t1 + t2)
        yv = a.l1 * math.sin(t1) + a.l2 * math.sin(t1 + t2)
        ok = abs(xv - x) < 1e-6 and abs(yv - y) < 1e-6
        u = "rad" if a.rad else "°"
        print(f"  {label}: θ1 = {rad2deg(t1, a.rad):.3f}{u}, θ2 = {rad2deg(t2, a.rad):.3f}{u}  "
              f"(FK check {'✅' if ok else '❌'})")
    if r / rmax > 0.98:
        print("⚠️ near full stretch — close to a singularity (θ2 ≈ 0).")


def cmd_jacobian2(a):
    t1 = deg2rad(a.t1, a.rad); t2 = deg2rad(a.t2, a.rad)
    l1, l2 = a.l1, a.l2
    j11 = -l1 * math.sin(t1) - l2 * math.sin(t1 + t2)
    j12 = -l2 * math.sin(t1 + t2)
    j21 = l1 * math.cos(t1) + l2 * math.cos(t1 + t2)
    j22 = l2 * math.cos(t1 + t2)
    det = j11 * j22 - j12 * j21
    # det simplifies to l1*l2*sin(t2)
    print(f"**2-link Jacobian** (θ1={a.t1}, θ2={a.t2}{'rad' if a.rad else '°'})\n")
    print(f"J = [[{j11:.4f}, {j12:.4f}],")
    print(f"     [{j21:.4f}, {j22:.4f}]]")
    print(f"det(J) = {det:.4f}  (= L1·L2·sin θ2)")
    if abs(det) < 1e-3:
        print("🚩 SINGULAR (or near) — arm loses a DOF; IK ill-conditioned. Avoid this pose.")
    else:
        print("✅ non-singular — well-conditioned here.")


def dh_transform(theta, d, a_, alpha):
    ct, st = math.cos(theta), math.sin(theta)
    ca, sa = math.cos(alpha), math.sin(alpha)
    return [
        [ct, -st * ca, st * sa, a_ * ct],
        [st, ct * ca, -ct * sa, a_ * st],
        [0, sa, ca, d],
        [0, 0, 0, 1],
    ]


def matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def cmd_dh(a):
    T = [[1 if i == j else 0 for j in range(4)] for i in range(4)]
    print("**DH-chain forward kinematics** (standard DH)\n")
    for idx, row in enumerate(a.row, 1):
        parts = row.replace(",", " ").split()
        if len(parts) != 4:
            raise SystemExit(f"row {idx}: need 'theta d a alpha', got '{row}'")
        theta, d, a_, alpha = (float(x) for x in parts)
        th = deg2rad(theta, a.rad); al = deg2rad(alpha, a.rad)
        T = matmul(T, dh_transform(th, d, a_, al))
        print(f"  joint {idx}: θ={theta}, d={d}, a={a_}, α={alpha}")
    print(f"\nTip position: x={T[0][3]:.4f}, y={T[1][3]:.4f}, z={T[2][3]:.4f}")
    print("Rotation (tip frame):")
    for i in range(3):
        print("  [" + ", ".join(f"{T[i][j]:+.4f}" for j in range(3)) + "]")


def main():
    ap = argparse.ArgumentParser(description="Robot kinematics calculator")
    ap.add_argument("--rad", action="store_true", help="angles in radians (default degrees)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fk2"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("l1", "l2", "t1", "t2")]; p.set_defaults(fn=cmd_fk2)
    p = sub.add_parser("ik2"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("l1", "l2", "x", "y")]; p.set_defaults(fn=cmd_ik2)
    p = sub.add_parser("jacobian2"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("l1", "l2", "t1", "t2")]; p.set_defaults(fn=cmd_jacobian2)
    p = sub.add_parser("dh"); p.add_argument("--row", action="append", required=True, help="'theta d a alpha' per joint"); p.set_defaults(fn=cmd_dh)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
