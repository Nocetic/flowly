---
name: control-systems
description: "Analyze and design feedback control systems — transfer functions, poles/zeros, stability (Routh-Hurwitz), second-order response (natural frequency, damping, overshoot, settling time, rise time), PID control and tuning (Ziegler-Nichols), steady-state error, and Bode/frequency-response basics. Includes a stdlib calculator for stability, response metrics, and PID tuning. Use when the user asks about a control loop, PID tuning, system stability, step response, damping/overshoot, a transfer function, or feedback design."
metadata: {"flowly":{"emoji":"🎛️","tags":["engineering","control-systems","pid","stability","transfer-function","feedback","dynamics","routh-hurwitz"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["circuit-analysis","mechanical-engineering","engineering-units","statistical-analysis"]}}
---

# Control Systems — Make It Stable, Then Make It Good

Control design has a strict priority order: **stability first** (does it blow up?), **then performance** (fast enough? accurate enough? not too oscillatory?). A beautifully tuned loop that's marginally unstable is worthless. The core tools are the transfer function (the system's input→output behavior), the pole locations (which decide stability and character), and a handful of metrics that turn "good response" into numbers.

## What this skill produces

**Chat-first.** Default: the analysis answer — stability verdict, the response metrics (ωn, ζ, overshoot, settling time), or PID gains — each with the reasoning and a sanity check. The `control_calc.py` helper handles stability tests, second-order metrics, and PID tuning. Offer a fuller writeup or simulation suggestion (python-control/MATLAB) for involved designs.

## When to use

- "Is this system stable?" / "Check stability of \<characteristic polynomial\>."
- "Tune a PID for this." / "What PID gains?" / "Ziegler-Nichols."
- "What's the overshoot / settling time / rise time?" / "How damped is it?"
- "Find the poles/zeros." / "What does this transfer function do?"
- "Why is my loop oscillating / sluggish / steady-state-error?"
- "Explain Bode / phase margin / gain margin."

## Transfer functions, poles & zeros

A linear system is G(s) = N(s)/D(s) (Laplace domain). The **poles** (roots of D(s)) determine stability and dynamic character; **zeros** (roots of N(s)) shape the response.
- **Stability (continuous):** ALL poles must have **negative real parts** (left-half s-plane). One pole in the right half → unstable (grows without bound). Poles on the imaginary axis → marginally stable (sustained oscillation).
- **Pole character:** real poles → exponential modes (1/|pole| ≈ time constant); complex-conjugate pairs → oscillatory modes (their damping ratio sets the ringing).
- Closed loop with unity feedback: T(s) = G/(1+G); the **characteristic equation is 1+G(s)=0** — its roots are the closed-loop poles. Stability is about *those*.

## Stability without finding roots: Routh-Hurwitz

For a characteristic polynomial aₙsⁿ + … + a₀, you can determine stability **without computing roots**:
- **Necessary:** all coefficients present and same sign (a missing or sign-flipped coefficient ⇒ unstable immediately).
- **Sufficient:** build the Routh array; the system is stable iff **all entries in the first column are the same sign** (no sign changes). The number of first-column sign changes = number of right-half-plane poles.
`control_calc.py routh` builds the array and gives the verdict (and can find the gain range for stability in simple cases).

## Second-order response (the standard model)

A huge fraction of systems are well-approximated by a second-order system: ωn²/(s² + 2ζωn·s + ωn²), characterized by **natural frequency ωn** and **damping ratio ζ**. From these you get the whole step response:

| Metric | Formula | Meaning |
|---|---|---|
| Damping regime | ζ<1 under, ζ=1 critical, ζ>1 over | Oscillatory vs not |
| % Overshoot | exp(−ζπ/√(1−ζ²))·100 | Peak above final (ζ only) |
| Settling time (2%) | ≈ 4/(ζωn) | Time to stay within 2% |
| Peak time | π/(ωn√(1−ζ²)) | Time to first peak |
| Rise time | ≈ 1.8/ωn (rough) | Speed of initial response |
| Damped freq ωd | ωn√(1−ζ²) | Ringing frequency |

**ζ ≈ 0.7** is the classic sweet spot — fast with ~5% overshoot. ζ too low → ringy; too high → sluggish. `control_calc.py response` computes all of these.

## PID control

PID output = Kp·e + Ki·∫e dt + Kd·de/dt. Each term has a job:
- **P (Kp):** speed/stiffness; raises responsiveness but alone leaves steady-state error and, too high, causes oscillation.
- **I (Ki):** kills steady-state error by integrating; too much adds lag and overshoot, and can **wind up** (clamp the integrator).
- **D (Kd):** damping/anticipation; reduces overshoot but amplifies noise (filter it).

**Tuning — Ziegler-Nichols (closed-loop):** raise Kp until sustained oscillation at the ultimate gain Ku with period Tu, then:

| Controller | Kp | Ki | Kd |
|---|---|---|---|
| P | 0.5·Ku | — | — |
| PI | 0.45·Ku | 1.2·Kp/Tu | — |
| PID | 0.6·Ku | 2·Kp/Tu | Kp·Tu/8 |

Z-N is a starting point (often aggressive ~25% overshoot) — then hand-tune. Practical rule of thumb: bump P for speed, add I to remove offset, add D to tame overshoot; change one at a time. `control_calc.py pid --ku --tu` gives the Z-N gains.

## Steady-state error & system type

Error depends on system **type** (number of integrators, poles at origin) and input:
- Type 0: finite error to a step (e_ss = 1/(1+Kp)); infinite to a ramp.
- Type 1: zero error to a step; finite to a ramp (1/Kv).
- Adding an integrator (raising type) removes the step error — that's *why* the I term works.

## Frequency response (Bode) basics

- **Gain margin / phase margin** quantify how close to instability you are. Rule of thumb: **PM ≈ 45–60°** and **GM ≥ 6 dB** for a robust loop.
- More phase margin ⇒ more damping/less overshoot but slower. Bode is the design view for robustness and bandwidth; for plotting, use python-control or MATLAB.

## The calculator

`scripts/control_calc.py` (stdlib only):
```bash
python3 scripts/control_calc.py routh 1 6 11 6          # coeffs high→low: s³+6s²+11s+6
python3 scripts/control_calc.py response --wn 10 --zeta 0.5   # 2nd-order metrics
python3 scripts/control_calc.py response --wn 10 --os 0.10    # back out zeta from %OS target
python3 scripts/control_calc.py pid --ku 6 --tu 0.5          # Ziegler-Nichols gains
python3 scripts/control_calc.py poles2 --wn 10 --zeta 0.5    # the two complex poles
```

## Chat output format

```
**Stability — s³ + 6s² + 11s + 6**

Routh first column: [1, 6, 10, 6] — no sign changes → **STABLE** ✅
(0 right-half-plane poles; poles at −1, −2, −3.)

**Response — ωn=10, target 10% overshoot**
ζ = 0.59 → settling(2%) ≈ 0.68 s · peak time 0.39 s · ωd 8.1 rad/s
ζ≈0.59 is in the good range (near 0.7); responsive with modest ringing.
```

## Workflow

1. **Frame it:** analysis (stable? metrics?) or design (tune PID, hit a spec)?
2. **Stability first** — Routh-Hurwitz on the characteristic polynomial (`routh`); for a gain K, find the stable range.
3. **Performance** — map specs (overshoot, settling) to ωn/ζ with `response`; locate poles.
4. **Design the controller** — PID with Z-N as a start (`pid`), then refine one gain at a time; watch margins.
5. **Check robustness** (PM/GM) and steady-state error vs the input type.
6. **Deliver** verdict/gains + caveats; suggest simulation (python-control) for plots/MIMO; route plant modeling to `circuit-analysis`/`mechanical-engineering`.

## Key pitfalls

- **Performance before stability.** Always confirm stability first — a fast unstable loop is useless.
- **Coefficient sign/missing term.** A characteristic polynomial with a missing or sign-flipped coefficient is unstable — no Routh array needed.
- **Z-N as final.** Ziegler-Nichols is an aggressive starting point (~25% overshoot); always refine.
- **Cranking integral gain.** Too much I causes overshoot, lag, and windup — clamp the integrator.
- **Derivative on noise.** D amplifies measurement noise; filter it or use derivative-on-measurement.
- **Tuning multiple gains at once.** Change one term at a time or you can't tell what helped.
- **Ignoring margins.** A nominally stable loop with tiny phase margin rings and is fragile to delay/parameter drift.
- **Continuous vs discrete confusion.** Digital control uses the z-plane (stable = poles *inside* the unit circle), not the left-half s-plane — don't mix them.

## Quick reference

- Stable (continuous): all closed-loop poles in the left-half plane (Re < 0). Discrete: inside the unit circle.
- Routh-Hurwitz: all coeffs same sign (necessary); no first-column sign changes (sufficient); #changes = #RHP poles.
- 2nd-order: %OS = e^(−ζπ/√(1−ζ²)) · t_s(2%) ≈ 4/(ζωn) · t_p = π/(ωn√(1−ζ²)) · ωd = ωn√(1−ζ²). ζ≈0.7 sweet spot.
- PID: P=speed, I=kills steady-state error, D=damping. Z-N PID: Kp=0.6Ku, Ki=2Kp/Tu, Kd=KpTu/8.
- Robustness rule of thumb: PM 45–60°, GM ≥ 6 dB. Higher type ⇒ lower steady-state error.
