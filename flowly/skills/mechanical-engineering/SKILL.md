---
name: mechanical-engineering
description: "Solve mechanical engineering problems — statics (forces, moments, equilibrium), stress/strain, beam bending (deflection and bending stress for common load cases), factor of safety, column buckling, fasteners (bolt preload, torque, shear), gears (ratios, torque), and pressure vessels. Includes a stdlib calculator for the standard formulas. Use when the user asks to size a beam/shaft/bolt, find a stress or deflection, check if a part is strong enough, a factor of safety, a gear ratio, or analyze a mechanical load."
metadata: {"flowly":{"emoji":"⚙️","tags":["engineering","mechanical","statics","stress","beam","fasteners","gears","fos","strength"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["openscad","cadquery","3d-printing","engineering-units"]}}
---

# Mechanical Engineering — Will It Hold, and by How Much?

Most mechanical questions reduce to: **what's the stress, what's the material's strength, and is the ratio (the factor of safety) big enough?** The discipline is getting the load path right, choosing the correct formula for the geometry/load case, keeping units consistent (SI: N, m, Pa — or be deliberate about mm/MPa), and always reporting a **factor of safety**, never a bare stress.

> **Reality check, not a stamped analysis.** These are first-order hand calculations to size parts and sanity-check designs. For safety-critical, fatigue, dynamic, or code-governed designs, a licensed PE and/or FEA is required — flag that.

## What this skill produces

**Chat-first.** Default: the worked solution — formula, numbers with units, the result, and a **factor of safety** vs the material limit, with a pass/fail read. The `mech_calc.py` helper does the standard formulas. Offer a fuller writeup or a parametric sweep for sizing.

## When to use

- "Is this \<beam/shelf/bracket/shaft\> strong enough?" / "What's the stress / deflection?"
- "Size a beam / shaft / bolt for this load."
- "What's the factor of safety?" / "Will it buckle?"
- "Bolt torque / preload for an M\<x\>?" / "How many bolts?"
- "Gear ratio / output torque / speed?"
- "Wall thickness for a pressure of \<p\>?"

## The core loop: stress vs strength vs FoS

- **Stress** σ = Force / Area (axial). **Strain** ε = ΔL/L. **Hooke:** σ = E·ε (E = Young's modulus).
- **Factor of Safety** = material strength / applied stress. FoS = σ_yield / σ_applied (yield) or σ_ultimate / σ_applied (ultimate). Typical targets: **1.5–2** (known loads, ductile, static), **3–4+** (uncertain loads, brittle, safety-critical, fatigue). FoS < 1 = it fails. **Always report FoS** — a stress number alone tells you nothing without the material limit.
- **Strength is material-specific:** quote which (yield vs ultimate) and which material (steel ~250 MPa yield mild / ~36 ksi; 6061-T6 Al ~276 MPa; PLA ~50 MPa tensile but brittle/creep). Direct material data → `engineering-units`/datasheets.

## Beam bending (the workhorse)

For a beam under transverse load, two things matter: **bending stress** (does it break?) and **deflection** (does it sag too much?).
- **Bending stress:** σ = M·c / I, where M = max bending moment, c = distance to outer fiber, I = area moment of inertia. (Section modulus S = I/c, so σ = M/S.)
- **I for common sections:** rectangle I = b·h³/12; solid circle I = π·d⁴/64; tube I = π(d_o⁴−d_i⁴)/64. The **h³** term is why depth matters far more than width — doubling depth gives 8× the stiffness.
- **Deflection** depends on the load case and end conditions:

| Case | Max moment | Max deflection |
|---|---|---|
| Cantilever, end point load P, length L | M = P·L | δ = P·L³/(3·E·I) |
| Cantilever, uniform load w (per length) | M = w·L²/2 | δ = w·L⁴/(8·E·I) |
| Simply supported, center point load P | M = P·L/4 | δ = P·L³/(48·E·I) |
| Simply supported, uniform load w | M = w·L²/8 | δ = 5·w·L⁴/(384·E·I) |

Note the **L³/L⁴**: deflection is extremely sensitive to length. The `mech_calc.py beam` command handles these cases.

## Columns & buckling

Slender members in compression fail by **buckling** before crushing. Euler critical load: **P_cr = π²·E·I / (K·L)²**, where K depends on end fixity (pinned-pinned 1.0, fixed-free 2.0, fixed-fixed 0.5, fixed-pinned 0.7). Check the slenderness ratio; for stocky columns, yield governs instead. Compression members must be checked for buckling, not just σ = P/A.

## Fasteners

- **Bolt preload (tension)** from torque: T ≈ K·F·d, where T = torque, F = preload, d = nominal dia, **K ≈ 0.2** (dry steel, rule of thumb; varies with lube/finish). So F ≈ T/(K·d). Preload should be ~75% of proof load typically.
- **Bolt shear / tensile capacity** = stress area × allowable stress; for a group, distribute load and check the worst bolt. More smaller bolts often beat one big one for a moment-loaded joint.
- **Thread engagement** ≥ ~1×d in steel, ~2×d in aluminum/soft materials, or strip the threads.

## Gears & rotating power

- **Ratio** = N_out/N_in (teeth) → speed_out = speed_in / ratio; **torque_out = torque_in × ratio** (ideal, before efficiency).
- **Power** P = T·ω (ω in rad/s = RPM·2π/60); P stays ~constant through a gear train (minus losses).
- Watch torque amplification on the slow-speed shaft — that's where the stress is.

## Pressure vessels (thin-wall)

For a thin-wall cylinder (t ≪ r): **hoop stress σ_h = p·r/t** (the one that governs — twice the axial), **axial σ_a = p·r/(2t)**. Sphere: σ = p·r/(2t). Solve for t given an allowable stress and FoS.

## The calculator

`scripts/mech_calc.py` covers the standard formulas (SI units: N, m, Pa; or pass mm and it notes the unit). It always prints the factor of safety when a material limit is given.

```bash
python3 scripts/mech_calc.py stress --force 5000 --area 100      # area in mm^2 -> MPa
python3 scripts/mech_calc.py fos --stress 120 --strength 250     # MPa
python3 scripts/mech_calc.py beam --case cantilever-point --p 200 --length 0.5 \
    --b 0.02 --h 0.04 --e 200e9 --yield 250e6                    # deflection + stress + FoS
python3 scripts/mech_calc.py buckling --e 200e9 --i 8.3e-9 --length 1.0 --k 1.0
python3 scripts/mech_calc.py bolt --torque 10 --d 0.008 --k 0.2  # preload from torque
python3 scripts/mech_calc.py gear --teeth-in 12 --teeth-out 36 --torque-in 2 --rpm-in 1500
python3 scripts/mech_calc.py vessel --pressure 1e6 --radius 0.05 --t 0.002 --yield 250e6
```
Stdlib only.

## Chat output format

```
**Cantilever bracket** (steel, 200 N at 500 mm, 20×40 mm section)

I = b·h³/12 = 0.02·0.04³/12 = 1.07e-7 m⁴
Bending stress σ = M·c/I = (200·0.5)·0.02 / 1.07e-7 = 18.7 MPa
Deflection δ = P·L³/(3EI) = 0.39 mm
FoS = 250 / 18.7 = 13.4 ✅ (very safe statically)

Note: deflection-driven? 0.39mm is small. Fatigue not checked — flag if cyclic.
```

## Workflow

1. **Define the load path & case:** what force, where, how supported, static vs cyclic.
2. **Pick the formula** for the geometry/load (axial, bending case, buckling, pressure).
3. **Compute with `mech_calc.py`**; keep units consistent.
4. **Get the material limit** (yield/ultimate for the actual material) and **report FoS** with a target.
5. **Check the other failure mode** — a beam strong in bending may still over-deflect; a compression member may buckle; a bolt joint may strip.
6. **Deliver** result + FoS + caveats; flag fatigue/dynamic/safety-critical for FEA/PE; route geometry to `openscad`/`cadquery`, units/properties to `engineering-units`.

## Key pitfalls

- **Stress without FoS.** "18 MPa" is meaningless alone — divide by the material limit and state the FoS and target.
- **Wrong load case.** Cantilever vs simply-supported, point vs distributed — using the wrong formula can be off by large factors.
- **Unit chaos.** Mixing mm and m, N and kN, MPa and Pa is the dominant error source. Pick a system and stay in it (the calculator notes units).
- **Checking strength but not deflection (or buckling).** Different failure modes; a part can pass one and fail another.
- **Trusting one strength number.** Yield vs ultimate, and the actual material/temper — quote which. Brittle materials (cast iron, PLA) need ultimate + higher FoS.
- **Ignoring stress concentrations.** Holes, notches, sharp internal corners multiply local stress (Kt) — add fillets; nominal σ understates the corner.
- **Static analysis on a cyclic load.** Fatigue fails well below yield — flag any repeated/vibrating load for a fatigue analysis.
- **Calling it final.** These are first-order checks; safety-critical → PE + FEA.

## Quick reference

- σ = F/A · ε = ΔL/L · σ = Eε · **FoS = strength/σ** (target 1.5–2 static ductile, 3–4+ uncertain/brittle/fatigue).
- Bending: σ = M·c/I = M/S · rectangle I = bh³/12 · circle I = πd⁴/64 (depth³ dominates).
- Deflection: cantilever-point PL³/3EI · simply-supported-center PL³/48EI · SS-uniform 5wL⁴/384EI.
- Buckling: P_cr = π²EI/(KL)² · K: pin-pin 1, fixed-free 2, fixed-fixed 0.5.
- Bolt: F_preload ≈ T/(K·d), K≈0.2 dry steel · thread engagement ≥1d steel / ~2d aluminum.
- Gears: torque_out = torque_in×ratio · speed_out = speed_in/ratio · P = Tω.
- Thin-wall pressure: hoop σ = pr/t (governs) · axial pr/2t · sphere pr/2t.
