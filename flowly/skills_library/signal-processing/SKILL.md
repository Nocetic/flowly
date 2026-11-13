---
name: signal-processing
description: "Analyze and design for digital signals — sampling and the Nyquist limit, aliasing, the DFT/FFT and frequency spectra, finding dominant frequencies, filter design (low/high/band-pass, RC and digital), windowing, convolution, and SNR. Includes a stdlib DSP helper (DFT, dominant-frequency detection from a samples CSV, Nyquist/aliasing check, RC filter design). Use when the user asks about FFT/spectrum, sampling rate, aliasing, filtering a signal, frequency content, or DSP design."
metadata: {"flowly":{"emoji":"〰️","tags":["engineering","dsp","signal-processing","fft","sampling","filters","nyquist","frequency"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["circuit-analysis","control-systems","engineering-units","statistical-analysis"]}}
---

# Signal Processing — Sample It Right, Then Find What's In It

Digital signal processing rests on one rule that, if broken, makes everything downstream garbage: **the sampling theorem**. Get the sample rate right, then the rest — spectra, filters, feature extraction — follows. The discipline: always reason in *both* time and frequency domains, and never trust a spectrum without knowing the sample rate and window.

## What this skill produces

**Chat-first.** Default: the analysis answer — the dominant frequencies in a signal, whether a sample rate is adequate, a filter's cutoff/order, or a spectrum summary — with the reasoning and the Nyquist sanity check. The `dsp.py` helper computes DFTs and detects frequencies. Offer a fuller writeup or a plot suggestion (numpy/scipy/matplotlib) for involved work.

## When to use

- "What frequencies are in this signal?" / "Run an FFT / spectrum."
- "Is my sample rate high enough?" / "Will this alias?"
- "Design a low-pass / high-pass / band-pass filter at fc."
- "How do I remove noise / 60 Hz hum / drift from this?"
- "Explain Nyquist / aliasing / windowing / convolution."
- "What's the SNR?" / "Decimate / interpolate?"

## The sampling theorem (start here, always)

- **Nyquist:** to capture a signal with maximum frequency f_max, sample at **f_s > 2·f_max**. The **Nyquist frequency** is f_s/2 — the highest frequency representable.
- **Aliasing:** any energy above f_s/2 folds back ("aliases") to a *lower* false frequency and is unrecoverable. A 70 Hz tone sampled at 100 Hz appears as 30 Hz. Fix: an **anti-aliasing analog low-pass filter before the ADC**, and/or a higher f_s.
- **Practical margin:** sample at 2.5–5× f_max, not exactly 2× — real signals aren't band-limited and you need room for the anti-alias filter roll-off.
- The DFT of N samples at f_s gives bins spaced **Δf = f_s/N** (frequency resolution). Longer capture → finer resolution. The spectrum is mirror-symmetric; only the first N/2 bins (0 to f_s/2) are unique for a real signal.

## The frequency domain (DFT/FFT)

- The DFT converts N time samples to N frequency bins. **FFT** is just a fast O(N log N) DFT (use a power-of-2 length). `dsp.py` uses a direct DFT (fine for modest N; for big signals use numpy's FFT).
- **Magnitude spectrum** = |X[k]| shows how much of each frequency is present; **phase** = angle. Bin k maps to frequency k·f_s/N.
- **Spectral leakage:** a frequency that doesn't land exactly on a bin smears across neighbors. Mitigate with a **window** (Hann, Hamming, Blackman) before the DFT — it trades a wider main lobe for lower side-lobes. Use a window for any real measured signal; rectangular (no window) only for exact-bin synthetic tones.
- **Resolution vs leakage trade-off:** more samples = finer Δf; windowing = cleaner peaks but slightly wider.

## Filters

| Type | Passes | Use for |
|---|---|---|
| Low-pass | below fc | anti-alias, smoothing, remove HF noise |
| High-pass | above fc | remove DC/drift/baseline wander |
| Band-pass | a band | isolate a signal of interest |
| Band-stop / notch | rejects a band | kill 50/60 Hz mains hum |

- **Analog 1st-order (RC):** fc = 1/(2πRC); −20 dB/decade roll-off. (See `circuit-analysis` for the hardware.)
- **Order = steepness:** each order adds −20 dB/decade. Butterworth (maximally flat), Chebyshev (steeper, ripple), Bessel (linear phase). Higher order = sharper cutoff but more phase distortion/ringing.
- **Digital filters:** FIR (linear phase, always stable, more taps) vs IIR (efficient, fewer coefficients, can be unstable — poles must stay in the unit circle; see `control-systems`). Specify cutoff as a fraction of Nyquist.
- **Filtering ≈ convolution** in time (multiplication in frequency). Removing a frequency band is clearest in the frequency domain.

## SNR & basics

- **SNR (dB) = 10·log₁₀(P_signal/P_noise) = 20·log₁₀(A_signal/A_noise).** Averaging N coherent captures improves SNR by ~10·log₁₀(N) dB.
- **Convolution** combines two signals (e.g. signal ⊛ filter impulse response); **correlation** measures similarity/lag (matched filtering, finding a pattern).

## The DSP helper

`scripts/dsp.py` (stdlib, uses `cmath`):
```bash
python3 scripts/dsp.py nyquist --fmax 70 --fs 100        # adequate? alias frequency?
python3 scripts/dsp.py dft samples.csv --fs 1000          # spectrum + dominant freqs
python3 scripts/dsp.py dft samples.csv --fs 1000 --window hann --top 5
python3 scripts/dsp.py rc --fc 100                         # RC values for a 1st-order cutoff
python3 scripts/dsp.py snr --signal 1.0 --noise 0.05       # SNR in dB
```
`samples.csv` = one column of time-domain samples; `--fs` is the sample rate (Hz).

## Chat output format

```
**Spectrum — samples.csv** (fs = 1000 Hz, N = 1024, Hann window)

Δf = 0.98 Hz · Nyquist = 500 Hz
Dominant frequencies:
1. 50.0 Hz (mag 1.00)   ← likely mains hum
2. 120.0 Hz (mag 0.42)
3. 8.0 Hz (mag 0.31)

To remove the 50 Hz: a notch at 50 Hz, or high-pass if 8 Hz is also unwanted.
Sample rate is fine (max content 120 Hz ≪ 500 Hz Nyquist). ✅
```

## Workflow

1. **Establish f_s and the band of interest** — and check Nyquist (`nyquist`): is f_s > 2·f_max? Any aliasing risk?
2. **Transform** — DFT with an appropriate window (`dft`); report Δf and the dominant frequencies.
3. **Interpret** — identify signal vs noise vs interference (mains hum, drift).
4. **Design the filter** — type + cutoff + order for the goal; RC values for analog (`rc`), or digital spec as a fraction of Nyquist.
5. **Deliver** the findings + filter recommendation; suggest numpy/scipy for big FFTs or plots; route analog hardware to `circuit-analysis`, digital-filter stability to `control-systems`.

## Key pitfalls

- **Undersampling / aliasing.** Sampling below 2·f_max folds high frequencies into false low ones — irreversible. Anti-alias before the ADC; keep margin.
- **No window on a measured signal.** Spectral leakage smears peaks; apply Hann/Hamming for real data.
- **Misreading bins.** Frequency = k·f_s/N; only the first N/2 bins are unique for real signals; the upper half is a mirror.
- **Too-short capture.** Δf = f_s/N — too few samples can't resolve close frequencies. Capture longer for finer resolution.
- **Forgetting filter phase.** Sharp/IIR filters distort phase and ring; use linear-phase FIR/Bessel when timing matters.
- **DC offset.** A large DC component dominates bin 0 and can hide everything; remove the mean or high-pass first.
- **Direct DFT on huge signals.** O(N²) is slow; for large N use numpy's FFT (the helper is for modest N).

## Quick reference

- Nyquist: f_s > 2·f_max; Nyquist freq = f_s/2; alias of f = |f − round(f/f_s)·f_s|.
- DFT: N samples → bins at k·f_s/N; Δf = f_s/N; first N/2 unique (real signal).
- Window (Hann/Hamming) for measured data → less leakage. RC: fc = 1/(2πRC).
- Filter order adds −20 dB/decade; FIR = linear phase/stable, IIR = efficient/can be unstable.
- SNR(dB) = 20·log₁₀(A_sig/A_noise); coherent averaging gains 10·log₁₀(N) dB.
- Big FFTs/plots → numpy/scipy/matplotlib; analog filters → circuit-analysis; digital stability → control-systems.
