---
name: physics-solver
description: "Solve introductory/classical physics problems — kinematics (constant acceleration, projectiles), dynamics (Newton's laws, friction, inclines), work-energy and conservation, momentum and collisions, and circular motion. Emphasizes free-body diagrams, the right equation, and unit discipline. Includes a stdlib solver (kinematics, projectile, energy, momentum). Use when the user has a mechanics problem — motion, forces, energy, momentum, collisions — to solve or explain."
metadata: {"flowly":{"emoji":"🪐","tags":["science","physics","mechanics","kinematics","dynamics","energy","momentum"],"requires":{"bins":["python3"]},"category":"science","related_skills":["engineering-units","mechanical-engineering","chemistry","statistical-analysis"]}}
---

# Physics Solver — Diagram It, Pick the Law, Mind the Units

Classical mechanics problems are solved by a reliable procedure: **draw the situation (free-body diagram), identify what's conserved or what law applies, pick the equation that uses your knowns, solve symbolically, then plug numbers with units.** Most errors are sign/direction mistakes or unit slips, not algebra — so set up coordinates and units first.

## What this skill produces

**Chat-first.** Default: the worked solution — setup (knowns/unknowns, coordinate choice), the law/equation chosen and why, the algebra, and the answer with units and a sanity check. The `physics.py` helper solves the standard cases. Explain the reasoning, not just the number.

## When to use

- "How far/fast/long…?" (kinematics) / "Projectile range/height?"
- "What force / acceleration / tension?" (Newton's laws, friction, inclines)
- "How much work / energy / power?" / "Speed at the bottom of the ramp?"
- "Collision — final velocities?" / "Is momentum/energy conserved?"
- "Circular motion / centripetal force / orbital speed?"

## Setup discipline (do this first)

1. **Coordinate system & signs** — pick + directions (e.g. up +, right +) and stick to them. Half of all errors are sign errors.
2. **Free-body diagram** (for force problems) — every force on the object: gravity (mg down), normal (⊥ surface), friction (opposes motion, μN), tension, applied. Newton's 2nd law per axis: ΣF = ma.
3. **List knowns/unknowns with units**; convert to SI up front (→ `engineering-units`).
4. **Choose the conserved quantity / law** that connects knowns to the unknown.

## Kinematics (constant acceleration)

The five SUVAT equations (s, u=initial v, v=final v, a, t) — pick the one missing the variable you don't have:
- v = u + at
- s = ut + ½at²
- v² = u² + 2as
- s = ½(u+v)t
- s = vt − ½at²

**Projectile motion:** decompose into independent x (constant velocity) and y (a = −g) components; they share only time. Range, max height, and time-of-flight all fall out. (g = 9.81 m/s².)

## Dynamics (Newton's laws)

- **ΣF = ma** per axis. **Weight** W = mg. **Friction** f = μN (kinetic μk, static ≤ μs·N).
- **Inclines:** rotate axes along/perpendicular to the slope; gravity components are mg·sinθ (down-slope) and mg·cosθ (into surface → N).
- **Connected bodies / pulleys:** same acceleration magnitude; write ΣF=ma for each, solve the system.

## Energy & momentum (conservation)

- **Work** W = F·d·cosθ. **Kinetic energy** KE = ½mv². **Gravitational PE** = mgh. **Power** P = W/t = F·v.
- **Work-energy theorem:** net work = ΔKE. **Conservation of energy:** KE+PE constant when only conservative forces act (friction dissipates → subtract the work it does).
- **Momentum** p = mv; **conserved in all collisions** (no external force). **Elastic** collisions also conserve KE; **inelastic** don't (perfectly inelastic = objects stick). Use momentum conservation to find post-collision velocities.
- Choose energy methods when forces/time aren't needed (speed from height); momentum for collisions; Newton when you need forces/acceleration.

## Circular motion

- Centripetal acceleration a = v²/r; **centripetal force** F = mv²/r (net inward force — provided by tension/gravity/friction, not a new force). Angular: ω = v/r.

## The solver

`scripts/physics.py` (stdlib; SI units, g=9.81):
```bash
python3 scripts/physics.py kinematics --u 0 --a 9.81 --t 3          # give any 3 of u,v,a,t,s
python3 scripts/physics.py kinematics --u 20 --v 0 --a -9.81        # solve t and s
python3 scripts/physics.py projectile --v0 30 --angle 40            # range, height, time
python3 scripts/physics.py energy --mass 2 --height 10              # PE, and speed at bottom
python3 scripts/physics.py momentum --m1 2 --v1 3 --m2 1 --v2 -1 --type inelastic
```
Stdlib only.

## Chat output format

```
**Projectile — launched 30 m/s at 40°**

Decompose: vx = 30cos40 = 22.98 m/s, vy = 30sin40 = 19.28 m/s
Time of flight = 2·vy/g = 3.93 s
Range = vx·t = 90.3 m
Max height = vy²/(2g) = 18.95 m
Sanity: 45° would maximize range; 40° gives slightly less — consistent. ✅
```

## Workflow

1. **Set up:** coordinates/signs, free-body diagram (force problems), SI units, knowns/unknowns.
2. **Pick the law:** SUVAT (kinematics), ΣF=ma (forces), energy conservation (speed/height, no time), momentum conservation (collisions), v²/r (circular).
3. **Solve symbolically**, then plug numbers with `physics.py`.
4. **Sanity-check:** units come out right? magnitude/direction sensible? (e.g. speed < c, energy ≥ 0).
5. **Deliver** setup + reasoning + answer; route unit conversion to `engineering-units`, real-world stress/structures to `mechanical-engineering`, data to `statistical-analysis`.

## Key pitfalls

- **Sign/direction errors.** Not fixing a coordinate convention is the #1 mistake — define + directions and apply them to every vector.
- **Skipping the free-body diagram.** Force problems go wrong without enumerating every force; "centripetal force" is a *net* force, not an extra one to add.
- **Unit slips.** Mixing km/h with m/s, grams with kg, degrees with radians. Convert to SI first.
- **Wrong tool.** Using kinematics where energy is cleaner (or vice versa); use energy when time isn't involved, momentum for collisions.
- **Assuming energy is conserved with friction.** Friction dissipates energy — account for its negative work; momentum is still conserved.
- **Forgetting g or using the wrong value.** g = 9.81 m/s² down (9.8 fine); it's an acceleration, not a force.
- **Elastic vs inelastic confusion.** KE only conserved in elastic collisions; perfectly inelastic objects stick (one final velocity).

## Quick reference

- SUVAT: v=u+at · s=ut+½at² · v²=u²+2as · s=½(u+v)t. Projectile: independent x (const v) & y (a=−g), shared t.
- Newton: ΣF=ma per axis; W=mg; friction f=μN; incline mg sinθ / mg cosθ.
- Energy: W=Fd cosθ · KE=½mv² · PE=mgh · P=W/t=Fv · net work=ΔKE; subtract friction's work.
- Momentum p=mv conserved in collisions; elastic also conserves KE; inelastic doesn't (stick = same v).
- Circular: a=v²/r, F=mv²/r (net inward). g=9.81 m/s². Always set coordinates + SI first.
