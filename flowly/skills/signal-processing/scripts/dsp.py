#!/usr/bin/env python3
"""DSP helper — Nyquist/aliasing check, DFT + dominant frequencies, RC filter,
SNR. Stdlib only (uses cmath). Direct DFT is O(N^2): fine for modest N (<~4096);
for large signals use numpy's FFT. Chat-ready markdown.

Usage:
    dsp.py nyquist --fmax 70 --fs 100
    dsp.py dft samples.csv --fs 1000 [--window hann] [--top 5]
    dsp.py rc --fc 100
    dsp.py snr --signal 1.0 --noise 0.05
"""
from __future__ import annotations

import argparse
import cmath
import csv
import math
import sys


def cmd_nyquist(a):
    nyq = a.fs / 2
    ok = a.fs > 2 * a.fmax
    print(f"Sample rate f_s = {a.fs} Hz · Nyquist = {nyq} Hz · signal max = {a.fmax} Hz")
    if ok:
        margin = a.fs / a.fmax
        print(f"✅ Adequate (f_s > 2·f_max). Oversampling ratio {margin:.1f}×."
              + ("" if margin >= 2.5 else " ⚠️ thin margin — aim for 2.5–5× for the anti-alias filter."))
    else:
        # alias frequency
        alias = abs(a.fmax - round(a.fmax / a.fs) * a.fs)
        print(f"❌ UNDERSAMPLED — {a.fmax} Hz will alias to {alias:.1f} Hz (irrecoverable).")
        print(f"   Need f_s > {2*a.fmax} Hz, or anti-alias filter below {nyq} Hz before sampling.")


def load_samples(path):
    vals = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            for cell in row:
                cell = cell.strip()
                if not cell:
                    continue
                try:
                    vals.append(float(cell))
                except ValueError:
                    pass  # skip header text
    if len(vals) < 4:
        sys.exit("need >= 4 numeric samples")
    return vals


def window(vals, kind):
    n = len(vals)
    if kind == "none" or kind is None:
        return vals
    out = []
    for i, v in enumerate(vals):
        if kind == "hann":
            w = 0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))
        elif kind == "hamming":
            w = 0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1))
        elif kind == "blackman":
            w = 0.42 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) + 0.08 * math.cos(4 * math.pi * i / (n - 1))
        else:
            w = 1.0
        out.append(v * w)
    return out


def dft_mag(vals):
    n = len(vals)
    mags = []
    for k in range(n // 2 + 1):
        acc = 0j
        for t, x in enumerate(vals):
            acc += x * cmath.exp(-2j * math.pi * k * t / n)
        mags.append(abs(acc) * 2 / n)
    return mags


def cmd_dft(a):
    vals = load_samples(a.csv)
    n = len(vals)
    mean = sum(vals) / n
    vals = [v - mean for v in vals]  # remove DC so bin 0 doesn't dominate
    w = window(vals, a.window)
    mags = dft_mag(w)
    df = a.fs / n
    nyq = a.fs / 2
    print(f"**Spectrum — {a.csv}** (fs={a.fs} Hz, N={n}, window={a.window or 'none'})\n")
    print(f"Δf = {df:.3g} Hz · Nyquist = {nyq:.4g} Hz · DC removed (mean {mean:.4g})\n")
    # peak picking: local maxima, sorted by magnitude, skip bin 0
    peaks = []
    for k in range(1, len(mags) - 1):
        if mags[k] > mags[k - 1] and mags[k] >= mags[k + 1]:
            peaks.append((mags[k], k * df))
    if not peaks:
        peaks = [(m, i * df) for i, m in enumerate(mags) if i > 0]
    peaks.sort(reverse=True)
    mmax = peaks[0][0] if peaks else 1.0
    print("Dominant frequencies:")
    for i, (m, f) in enumerate(peaks[:a.top], 1):
        print(f"{i}. {f:.3g} Hz  (rel mag {m/mmax:.2f})")


def cmd_rc(a):
    # pick a round C, solve R
    import math as _m
    fc = a.fc
    print(f"1st-order RC cutoff fc = {fc} Hz → fc = 1/(2πRC)")
    for c, clabel in [(1e-6, "1µF"), (100e-9, "100nF"), (10e-9, "10nF")]:
        r = 1 / (2 * _m.pi * fc * c)
        print(f"  C = {clabel} → R = {r:,.0f} Ω")
    print("(−20 dB/decade roll-off. Higher order = steeper; see circuit-analysis/control-systems.)")


def cmd_snr(a):
    snr = 20 * math.log10(a.signal / a.noise)
    print(f"SNR = 20·log₁₀({a.signal}/{a.noise}) = {snr:.1f} dB "
          f"(power ratio {(a.signal/a.noise)**2:.1f}×)")


def main():
    ap = argparse.ArgumentParser(description="DSP helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("nyquist"); p.add_argument("--fmax", type=float, required=True); p.add_argument("--fs", type=float, required=True); p.set_defaults(fn=cmd_nyquist)
    p = sub.add_parser("dft"); p.add_argument("csv"); p.add_argument("--fs", type=float, required=True)
    p.add_argument("--window", choices=["none", "hann", "hamming", "blackman"], default="hann"); p.add_argument("--top", type=int, default=5); p.set_defaults(fn=cmd_dft)
    p = sub.add_parser("rc"); p.add_argument("--fc", type=float, required=True); p.set_defaults(fn=cmd_rc)
    p = sub.add_parser("snr"); p.add_argument("--signal", type=float, required=True); p.add_argument("--noise", type=float, required=True); p.set_defaults(fn=cmd_snr)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
