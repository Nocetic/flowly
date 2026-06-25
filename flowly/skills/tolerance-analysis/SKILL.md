---
name: tolerance-analysis
description: "Analyze dimensional tolerances and fits — 1-D tolerance stack-ups (worst-case and statistical RSS), gap/clearance/interference min-max, hole/shaft fit classes (clearance/transition/interference), process capability (Cpk) implications, and GD&T basics. Includes a stdlib stack-up calculator. Use when the user asks whether parts will fit/assemble, a tolerance stack-up, gap analysis, what tolerance to specify, a press/clearance fit, or worst-case vs statistical tolerancing."
metadata: {"flowly":{"emoji":"📐","tags":["engineering","tolerance","gd&t","stack-up","fits","manufacturing","metrology","cpk"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["mechanical-engineering","cadquery","3d-printing","gcode-cnc"]}}
---

# Tolerance Analysis — Will the Parts Actually Fit?

Every dimension has variation; a "perfect" CAD model assembles, but real parts at the extremes of their tolerances may bind, fall apart, or leave an out-of-spec gap. Tolerance analysis predicts that **before** cutting metal. The central decision is **worst-case vs statistical (RSS)**: worst-case guarantees fit but demands tight (expensive) tolerances; RSS exploits the unlikelihood of all parts being extreme at once, allowing looser tolerances at a small, quantified risk.

## What this skill produces

**Chat-first.** Default: the stack-up result — nominal gap, worst-case min/max, and the statistical (RSS) spread with a fit verdict (always fits / fits statistically with X risk / can interfere). The `tolstack.py` helper computes both methods. Offer a fuller writeup or a fit-class recommendation.

## When to use

- "Will these parts fit / assemble?" / "Tolerance stack-up for this assembly."
- "What's the gap / clearance / interference between these features?"
- "Worst-case vs statistical — which tolerances do I need?"
- "What fit for a shaft in a hole?" (clearance / transition / press)
- "What tolerance should I specify to hit this gap?"
- "What does ⌖ / ⏥ / Ⓜ mean?" (GD&T basics)

## The stack-up (1-D)

A chain of dimensions adds up to a gap or critical dimension. Each contributor has a **nominal ± tolerance**; some add to the gap, some subtract (direction matters — assign +/− by whether increasing that dimension opens or closes the gap).

- **Nominal gap** = Σ(±nominalᵢ).
- **Worst-case (WC):** gap_tol = Σ|tolᵢ|. Min/max = nominal ∓ Σtol. **Guarantees** assembly across the full tolerance range, but is pessimistic — all parts at their worst simultaneously is rare. Tight, costly tolerances.
- **Statistical (RSS, root-sum-square):** assuming each dimension is independent and ~normal, tolerances combine as **gap_tol = √(Σtolᵢ²)**. The spread is much smaller than WC (especially with many contributors). Allows looser individual tolerances, at a small, quantifiable defect rate. Use when production volume is high and parts are statistically independent.
- **The trade:** WC = zero fit defects, expensive. RSS = looser/cheaper, tiny defect rate. Pick by volume, criticality, and cost. (A common middle ground: RSS with a safety factor, or "modified RSS".)

## Fits (hole & shaft)

When a shaft goes in a hole, the *combination* of their tolerances sets the fit:
- **Clearance fit:** always a gap (shaft always smaller than hole). For sliding/rotating. Clearance = hole − shaft, always > 0.
- **Transition fit:** may be slight clearance or slight interference depending on actual sizes. For location with easy assembly.
- **Interference (press) fit:** shaft always larger → press/shrink fit, holds by friction. For permanent joints.
ISO (H7/g6 etc.) and ANSI systems codify standard fits; the letter+number sets the tolerance band and position. For 3D-printed fits, add generous clearance (parts come out oversize → `3d-printing`).

## Process capability (why RSS works)

- **Cp = tolerance width / (6σ)**, **Cpk** accounts for centering. Cpk ≥ 1.33 (±4σ) is a common "capable" target; ≥ 1.0 means ±3σ fits the spec (~0.27% out).
- RSS implicitly assumes each dimension is produced to roughly ±3σ within its tolerance and is centered. If a process is off-center or non-normal, RSS optimism breaks — worst-case or a capability-corrected method is safer.

## GD&T basics (geometric dimensioning & tolerancing)

Beyond ± on sizes, GD&T controls **form, orientation, location** relative to **datums**:
- Form: flatness ⏥, straightness, circularity, cylindricity.
- Orientation: parallelism, perpendicularity ⟂, angularity.
- Location: **position ⌖** (the workhorse — locates a feature within a tolerance zone from datums), concentricity, symmetry.
- Profile: of a line / surface.
- **Material condition modifiers:** Ⓜ (MMC — bonus tolerance when the feature has extra material), Ⓛ (LMC). MMC is key for fits — it grants extra positional tolerance as the hole grows, reflecting that a bigger hole still accepts the pin.
GD&T is more powerful and less ambiguous than ± stacks for real parts, and bonus tolerance (MMC) can loosen requirements legitimately. For a full GD&T scheme, note it's a CAD-drawing task.

## The helper

`scripts/tolstack.py` (stdlib):
```bash
# each contributor: nominal:tol  (prefix - if it subtracts from the gap)
python3 scripts/tolstack.py stack 50:0.1 -30:0.05 -19.5:0.05
python3 scripts/tolstack.py stack --csv dims.csv          # columns: nominal,tol[,sign]
python3 scripts/tolstack.py fit --hole 10:0.015 --shaft 10:-0.01:0.006  # hole/shaft fit
python3 scripts/tolstack.py cpk --tol 0.1 --sigma 0.02 --offset 0.01
```
Stack reports nominal, **worst-case** min/max, and **RSS** ±3σ spread. Stdlib only.

## Chat output format

```
**Stack-up — gap = 50 − 30 − 19.5**

Nominal gap: 0.500 mm
Worst-case:  ±0.200 → gap [0.300, 0.700]  (always assembles ✅, but tight tols)
RSS (±3σ):   ±0.123 → gap [0.377, 0.623]  (looser tols possible; ~0.27% beyond if off-center)

Recommendation: gap never closes (min 0.30 WC > 0) → fit guaranteed.
If chasing cheaper tols at volume, RSS lets you open the ±'s; verify process Cpk ≥ 1.33.
```

## Workflow

1. **Define the critical dimension/gap** and the **loop** of contributing dimensions; assign +/− signs by their effect on the gap.
2. **Gather nominal ± tolerance** for each contributor.
3. **Run `tolstack.py`** for both worst-case and RSS.
4. **Verdict:** does the gap stay in spec at worst-case? If not, does RSS pass at acceptable risk? Identify the **biggest contributor** to tighten (largest |tol|, or largest tol² for RSS).
5. **For fits,** classify clearance/transition/interference and recommend a fit class.
6. **Deliver** results + recommendation (which tolerance to tighten, WC vs RSS choice); route feature geometry to `cadquery`, machining capability to `gcode-cnc`, printed-fit clearances to `3d-printing`.

## Key pitfalls

- **Sign errors in the loop.** A dimension that *closes* the gap must subtract. A wrong sign inverts the result — trace the dimension chain carefully.
- **Blindly using worst-case (or blindly RSS).** WC over-tightens (cost) for high-volume independent parts; RSS under-protects for low-volume, off-center, or correlated processes. Match the method to reality.
- **RSS on non-independent/non-normal dimensions.** RSS assumes independent, centered, ~normal contributors. Correlated features (same fixture) or skewed processes break the optimism.
- **Ignoring process capability.** RSS tolerances are only safe if the processes actually hit ±3σ centered (Cpk ≥ 1). Verify.
- **Forgetting it's a chain, not one dimension.** The gap depends on *all* contributors; tightening the wrong one wastes money — target the biggest contributor.
- **Plus/minus where GD&T belongs.** For location/orientation of features, ± on coordinates is ambiguous and wasteful; position tolerance (with MMC bonus) is the right tool.
- **3D-printed fits at nominal.** Printed parts come out oversize — add clearance (→ 3d-printing), don't use machining fit tables directly.

## Quick reference

- Worst-case gap tol = Σ|tolᵢ| (guaranteed, tight). RSS gap tol = √(Σtolᵢ²) (looser, small risk).
- Assign +/− to each dimension by whether it opens or closes the gap.
- Fits: clearance (always gap) · transition (either) · interference (press). Hole − shaft sets it.
- Cp = tol/(6σ); Cpk ≥ 1.33 capable; RSS assumes ~±3σ centered independent normals.
- Biggest stack contributor = largest |tol| (WC) or largest tol² (RSS) — tighten that one.
- GD&T position ⌖ with MMC Ⓜ gives legitimate bonus tolerance for fits.
- Printed fits → add clearance (3d-printing); machined fits → ISO/ANSI fit classes.
