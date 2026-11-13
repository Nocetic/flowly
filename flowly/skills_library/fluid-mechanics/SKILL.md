---
name: fluid-mechanics
description: "Solve fluid mechanics problems — continuity (Q=VA), Bernoulli's equation, the Reynolds number and laminar/turbulent regime, pipe head loss (Darcy-Weisbach with Swamee-Jain friction factor), minor (fitting) losses, pressure drop, and pump power/head. Includes a stdlib calculator. Use when the user asks about flow rate, pipe sizing, pressure drop, head loss, pump sizing, Reynolds number, or whether flow is laminar or turbulent."
metadata: {"flowly":{"emoji":"💧","tags":["engineering","fluid-mechanics","flow","reynolds","bernoulli","head-loss","pump","piping"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["thermodynamics","mechanical-engineering","engineering-units","power-sizing"]}}
---

# Fluid Mechanics — Flow, Pressure, and the Pump to Move It

Most practical fluids questions are a chain: **flow rate → velocity → Reynolds number (which regime?) → friction → head loss/pressure drop → pump power.** Each step feeds the next, and the Reynolds number is the hinge — it decides whether the flow is orderly (laminar) or chaotic (turbulent), which changes every loss calculation. Keep SI units, track the fluid's density and viscosity, and always state the regime.

## What this skill produces

**Chat-first.** Default: the worked answer — the relevant quantity (flow, velocity, Re, head loss, pump power) with the regime stated and a sanity check. The `fluid.py` helper does the standard pipe-flow chain. Offer a fuller writeup for a multi-segment system.

## When to use

- "What flow rate / velocity in this pipe?" / "Size a pipe for this flow."
- "Pressure drop / head loss over this pipe run?"
- "Is the flow laminar or turbulent?" / "Reynolds number?"
- "What pump (power/head) do I need?"
- "Apply Bernoulli to this." / "Velocity from this pressure?"
- "How much does adding fittings/elbows cost me?" (minor losses)

## The fundamentals

- **Continuity (mass conservation):** Q = V·A (volumetric flow = velocity × cross-section). Incompressible: Q constant, so velocity rises where area shrinks (A₁V₁ = A₂V₂). Pipe area A = π·d²/4.
- **Bernoulli (energy, ideal/no-loss):** P + ½ρV² + ρgz = constant along a streamline. Pressure ↓ where velocity ↑ (the Venturi/lift effect). Real flows add a head-loss term — Bernoulli is the frictionless baseline.
- **Heads:** express energy as height of fluid. Pressure head P/(ρg), velocity head V²/(2g), elevation head z. Pumps add head; friction removes it.

## Reynolds number — the regime decider

**Re = ρVd/μ = Vd/ν** (ρ density, V velocity, d diameter, μ dynamic / ν kinematic viscosity). It's the ratio of inertial to viscous forces:
- **Re < ~2300:** laminar (smooth, orderly; friction f = 64/Re).
- **~2300–4000:** transitional (unpredictable).
- **Re > ~4000:** turbulent (mixing, higher losses; friction from Colebrook/Swamee-Jain).

Always compute Re first — it picks the friction-factor formula and tells you how the system behaves.

## Head loss & pressure drop

- **Major loss (pipe friction), Darcy-Weisbach:** h_f = f·(L/d)·(V²/2g). f is the **Darcy friction factor**:
  - Laminar: f = 64/Re (exact).
  - Turbulent: solve Colebrook, or use the explicit **Swamee-Jain**: f = 0.25 / [log₁₀(ε/(3.7d) + 5.74/Re^0.9)]², where ε is pipe roughness.
- **Minor losses (fittings):** h_m = ΣK·(V²/2g), K = loss coefficient per fitting (elbow ~0.9, gate valve open ~0.2, sudden exit ~1.0). On long runs major loss dominates; on short fitting-heavy runs minor losses matter.
- **Pressure drop:** ΔP = ρg·h_loss. Total head loss = major + minor.

## Pump sizing

- **Pump head** must cover: elevation change + total head loss + any pressure/velocity head needed. H_pump = Δz + h_loss (+ terms).
- **Hydraulic power:** P = ρ·g·Q·H. **Shaft power** = hydraulic / pump efficiency (η typically 0.5–0.85). Size the motor to the shaft power (→ `power-sizing`).
- Check **NPSH** (net positive suction head) to avoid cavitation on the suction side — flag it; don't let suction pressure fall below the fluid's vapor pressure.

## The calculator

`scripts/fluid.py` (SI: m, m/s, Pa, kg/m³; water defaults ρ=998, ν=1.0e-6):
```bash
python3 scripts/fluid.py reynolds --v 2 --d 0.05            # + regime (water default)
python3 scripts/fluid.py reynolds --q 0.004 --d 0.05        # from flow rate
python3 scripts/fluid.py flow --q 0.004 --d 0.05            # velocity & area
python3 scripts/fluid.py headloss --q 0.004 --d 0.05 --l 50 --rough 4.5e-5 --ksum 2.0
python3 scripts/fluid.py pump --q 0.004 --head 25 --eff 0.7
python3 scripts/fluid.py bernoulli --p1 200000 --v1 1 --z1 0 --v2 5 --z2 3  # solve P2
```
Defaults to water; pass `--rho`/`--nu` (or `--mu`) for other fluids. Stdlib only.

## Chat output format

```
**Pipe run — 4 L/s through 50 m of 50 mm pipe (water)**

V = Q/A = 0.004 / 0.00196 = 2.04 m/s
Re = Vd/ν = 2.04·0.05/1e-6 = 1.02e5 → TURBULENT
f (Swamee-Jain, ε=0.045mm) = 0.0205
Major loss h_f = f·(L/d)·V²/2g = 4.35 m · minor (ΣK=2) = 0.42 m → 4.77 m
ΔP = ρg·h = 46.7 kPa
Pump (incl. 3 m lift, η 0.7): H≈7.8 m → ~0.44 kW shaft. (Check NPSH.)
```

## Workflow

1. **Get geometry + fluid:** diameter, length, flow or velocity, roughness, fittings; fluid ρ and ν/μ (water default).
2. **Velocity & Reynolds** (`reynolds`/`flow`) — **state the regime** (it picks the friction model).
3. **Head loss** (`headloss`): major (Darcy-Weisbach + regime-appropriate f) + minor (ΣK); convert to ΔP.
4. **Pump** (`pump`): head = lift + losses; hydraulic→shaft power via efficiency; flag NPSH/cavitation.
5. **Sanity-check:** velocity in a sane range (liquids ~1–3 m/s typical), Re regime consistent, ΔP reasonable.
6. **Deliver** result + regime + check; route motor sizing to `power-sizing`, thermal/property data to `thermodynamics`/`engineering-units`.

## Key pitfalls

- **Skipping Reynolds.** The regime determines the friction factor; using a turbulent formula on laminar flow (or vice versa) is wrong. Compute Re first, every time.
- **Bernoulli with friction.** Pure Bernoulli ignores losses — only valid for short, smooth, ideal flows. Real pipe runs need the head-loss term.
- **Forgetting minor losses on fitting-heavy runs.** Elbows/valves/contractions can dominate short systems.
- **Unit/viscosity mix-ups.** Dynamic (μ, Pa·s) vs kinematic (ν = μ/ρ, m²/s); roughness in mm vs m. One slip wrecks Re and f.
- **Wrong roughness.** ε varies widely (smooth PVC ~0.0015 mm, steel ~0.045 mm, old/corroded much more) — it shifts turbulent friction.
- **Ignoring pump efficiency / NPSH.** Hydraulic power isn't shaft power; and low suction pressure causes cavitation that destroys pumps.
- **Velocity too high.** >~3 m/s in liquid lines means noise, erosion, and steep losses — usually a sign to upsize the pipe.

## Quick reference

- Q = V·A, A = πd²/4 · Bernoulli: P + ½ρV² + ρgz = const (+ h_loss for real).
- Re = ρVd/μ = Vd/ν · laminar <2300, turbulent >4000.
- f: laminar 64/Re · turbulent Swamee-Jain 0.25/[log₁₀(ε/3.7d + 5.74/Re^0.9)]².
- Darcy-Weisbach h_f = f(L/d)(V²/2g) · minor h_m = ΣK·V²/2g · ΔP = ρg·h.
- Pump: H = Δz + h_loss; hydraulic P = ρgQH; shaft = hydraulic/η (0.5–0.85). Check NPSH.
- Roughness ε: PVC ~0.0015 mm, steel ~0.045 mm. Liquid velocity ~1–3 m/s typical. Motor → power-sizing.
