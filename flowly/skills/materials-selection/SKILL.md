---
name: materials-selection
description: "Select engineering materials for a part — compare metals, polymers, ceramics, and composites by density, stiffness (Young's modulus), strength, cost, temperature, and corrosion; rank candidates by Ashby material indices (e.g. specific stiffness E/ρ, specific strength σ/ρ) for the function (stiff/light beam, strong/light tie, etc.); and reason through trade-offs. Includes a stdlib property database with show/compare/rank. Use when the user asks which material to use, to compare materials, what's strongest/lightest/cheapest for a job, or about material properties."
metadata: {"flowly":{"emoji":"🧱","tags":["engineering","materials","selection","ashby","properties","metals","polymers","composites"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["mechanical-engineering","3d-printing","gcode-cnc","engineering-units"]}}
---

# Materials Selection — The Right Material for the Function

Material selection is not "what's strongest" — it's "what maximizes the right **index** for *this* function under *these* constraints." A stiff, light bicycle frame and a cheap, strong bracket optimize different ratios. The discipline (Ashby's method): translate the design into a **function + objective + constraints**, derive the material index that captures it, then rank candidates by that index — not by any single raw property.

## What this skill produces

**Chat-first.** Default: a ranked shortlist of candidate materials for the stated function, with the index used and the key trade-offs (and why the winner beats the obvious choice). The `materials.py` helper holds a property database and ranks by Ashby indices. Offer a fuller comparison table or a property deep-dive.

## When to use

- "What material should I use for \<part\>?" / "Best material for X?"
- "Compare aluminium vs steel vs titanium vs CFRP for this."
- "Strongest / lightest / stiffest / cheapest material for a \<beam/tie/panel\>?"
- "What's the density / modulus / yield strength of \<material\>?"
- "Why is CFRP used in aerospace?" (specific-property reasoning)

## The Ashby method (function → index → ranking)

1. **Function** — what does the part do? (tie in tension, beam in bending, panel, shaft, spring, pressure vessel…)
2. **Objective** — what to minimize/maximize? (mass, cost, energy storage…)
3. **Constraints** — what must it satisfy? (stiffness, strength, temperature, corrosion, geometry fixed or free?)
4. **Derive the index** — the property group that, maximized, gives the best material. Then **rank candidates by it.**

**Common material indices** (maximize M):

| Function / objective | Index M | Reads as |
|---|---|---|
| Stiff tie, min mass | E/ρ | specific stiffness |
| Strong tie, min mass | σ_f/ρ | specific strength |
| Stiff beam, min mass | E^(1/2)/ρ | bending-stiffness-limited light beam |
| Strong beam, min mass | σ_f^(2/3)/ρ | bending-strength-limited light beam |
| Stiff panel, min mass | E^(1/3)/ρ | plate in bending |
| Spring, max energy/volume | σ_f²/E | resilience |
| Min cost (strong tie) | σ_f/(ρ·C_m) | strength per cost-mass |

The exponents come from the mechanics of the shape (a beam's stiffness scales with the second moment, hence the ^(1/2)) — which is *why* "strongest" alone is the wrong question; the geometry changes which property dominates.

## Material families (the big trade-offs)

| Family | Strong at | Weak at | Typical |
|---|---|---|---|
| **Steels** | strength, stiffness, cheap, tough | heavy, corrodes (unless stainless) | structures, tools, shafts |
| **Aluminium alloys** | light, corrosion-OK, machinable | lower stiffness/strength than steel, softens with heat | aerospace, frames, heatsinks |
| **Titanium** | strength/weight, corrosion, high-temp | very expensive, hard to machine | aerospace, medical, marine |
| **Polymers** | cheap, light, formable, corrosion-proof | low stiffness/strength, low temp, creep | housings, consumer parts |
| **Composites (CFRP/GFRP)** | best specific stiffness & strength, tailorable | costly, anisotropic, hard to join/repair | aerospace, performance |
| **Ceramics** | hardness, stiffness, temperature, wear | brittle (low toughness), tension-weak | cutting tools, wear, thermal |

Key idea: **specific properties** (per density) flip the ranking. Titanium and CFRP aren't the strongest in absolute terms but win on strength/weight — which is why aerospace pays for them.

## Beyond the index — the constraints that kill candidates

Rank by the index, then screen by the hard constraints that disqualify regardless of index:
- **Temperature** (service temp vs softening/melting; polymers fail early).
- **Corrosion / environment** (saltwater, chemicals, UV).
- **Toughness / brittleness** (ceramics are stiff and strong but shatter — wrong for impact).
- **Manufacturability & joining** (can you machine/weld/print/bond it? → `gcode-cnc`, `3d-printing`).
- **Cost & availability** (the index winner may be unaffordable).
- **Fatigue** (cyclic loads — aluminium has no true endurance limit, steel does).

## The helper

`scripts/materials.py` (stdlib; built-in property DB):
```bash
python3 scripts/materials.py show steel-mild aluminium-6061 titanium-ti6al4v cfrp
python3 scripts/materials.py compare aluminium-6061 steel-mild --props density,E,yield,cost
python3 scripts/materials.py rank --index stiff-beam            # E^0.5/rho, all materials
python3 scripts/materials.py rank --index strong-tie --top 5
python3 scripts/materials.py rank --index "E/rho"               # custom index expression
python3 scripts/materials.py list                                # all materials + indices
```
Properties are typical mid-range values for guidance — verify against a real datasheet for the specific alloy/grade.

## Chat output format

```
**Stiff, light beam — material ranking** (index E^½/ρ)

1. CFRP        12.4   (best specific bending stiffness — but $$ and anisotropic)
2. Aluminium   3.1    (cheap, isotropic, machinable — the practical default)
3. Ti-6Al-4V   2.4
4. Steel       1.8    (stiff but heavy → loses on E^½/ρ despite high E)

Pick: CFRP if budget/aerospace; aluminium-6061 for cost+manufacturability.
Screen: service temp? fatigue (cyclic)? joining method? Confirm grade datasheet.
```

## Workflow

1. **Frame it (Ashby):** function, objective, constraints. Is the geometry fixed or free? (Decides which index.)
2. **Pick the index** (`rank --index ...`) — tie vs beam vs panel, stiffness vs strength, mass vs cost.
3. **Rank candidates** by the index; show the top few with values.
4. **Screen by hard constraints** (temp, corrosion, toughness, manufacturability, cost, fatigue) — eliminate disqualified winners.
5. **Recommend** with the trade-off explained (why the winner beats the obvious choice, and the practical runner-up).
6. **Deliver** the shortlist; route stress/FoS to `mechanical-engineering`, machinability to `gcode-cnc`, printability to `3d-printing`, units/conversions to `engineering-units`. Verify final pick against a real datasheet.

## Key pitfalls

- **"Strongest" as the question.** The right metric is the *index* for the function — for a light beam that's E^(1/2)/ρ, not E or σ alone. Raw strength misleads.
- **Ignoring specific properties.** Per-density properties flip rankings; that's the whole reason for Ti/CFRP in weight-critical roles.
- **Index without constraints.** The index winner can be disqualified by temperature, brittleness, cost, or "can't be joined." Screen after ranking.
- **Forgetting brittleness/toughness.** Stiff+strong ceramics shatter under impact — wrong for shock/tension despite great numbers.
- **Anisotropy of composites.** CFRP properties depend on fiber direction — the quoted number may be along-fiber only.
- **Fatigue under cyclic load.** Static strength isn't endurance; aluminium lacks a true endurance limit — derate for cycles.
- **Generic property = specific alloy.** "Steel" spans a huge range; confirm the exact grade/temper on a datasheet before committing.

## Quick reference

- Ashby: function → objective → constraints → **index**; rank by the index, then screen by constraints.
- Indices: stiff tie E/ρ · strong tie σ/ρ · stiff beam E^½/ρ · strong beam σ^⅔/ρ · stiff panel E^⅓/ρ · spring σ²/E.
- Specific properties (per ρ) drive weight-critical selection (why aerospace uses Ti/CFRP).
- Screen winners by: temperature, corrosion, toughness, manufacturability/joining, cost, fatigue.
- `materials.py rank --index ...`; values are typical — verify the grade datasheet.
- Stress/FoS → mechanical-engineering; machining → gcode-cnc; printing → 3d-printing.
