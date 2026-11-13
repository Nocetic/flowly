---
name: 3d-printing
description: "Prepare models for 3D printing (FDM/FFF and resin) — design-for-printing rules (wall thickness, overhangs, tolerances/clearances, orientation, supports, bridging), material selection (PLA/PETG/ABS/ASA/TPU/resin), slicer settings (layer height, walls, infill, temps, speed), and STL sanity checks. Includes a stdlib STL analyzer (bounding box, volume, triangle count, degenerate-triangle check). Use when the user has an STL/model to print, asks about print settings, supports, orientation, why a print failed, material choice, or how to make a part printable."
metadata: {"flowly":{"emoji":"🖨️","tags":["engineering","3d-printing","fdm","resin","slicing","dfm","stl","manufacturing"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["openscad","cadquery","mechanical-engineering","engineering-units"]}}
---

# 3D Printing — Design for the Process, Then Dial in the Slicer

A model that's geometrically perfect can still print terribly. 3D printing is a **manufacturing process with physics** — gravity during printing, layer adhesion, nozzle width, cooling. This skill covers the two halves: **design-for-printing** (so the part *can* print well) and **slicer settings** (so it *does*), plus quick STL sanity checks.

## What this skill produces

**Chat-first.** Default: a concise printability assessment + recommended settings (material, layer height, walls, infill, orientation, supports yes/no) and any design fixes — readable inline. The STL analyzer prints a quick fact sheet. Offer a fuller writeup for tricky parts.

## When to use

- "How should I print this?" / "What settings for this STL?"
- "Why did my print fail / warp / have weak layers / stringing?"
- "Does this need supports?" / "Best orientation?"
- "Which material — PLA / PETG / ABS / TPU / resin?"
- "Make this design printable." / "What clearance for a press-fit / snap?"
- "Check this STL." (dimensions / is it sane)

## Design-for-printing (FDM) — the rules that matter

| Rule | Guideline | Why |
|---|---|---|
| **Wall thickness** | ≥ 2–3 perimeters (≥0.8–1.2 mm at 0.4 nozzle) | Thinner = weak/unprintable |
| **Overhangs** | ≤ ~45° from vertical print support-free | Steeper droops without support |
| **Bridges** | Short unsupported spans OK (~5–10 mm); longer sag | Cooling-dependent |
| **Holes** | Print ~0.1–0.3 mm undersize (they shrink); vertical holes go oval | Account for it or ream/model oversize |
| **First layer / bed contact** | Maximize flat contact; add brim for tall/small footprints | Adhesion = success |
| **Fillets > sharp internal corners** | Round internal corners | Reduce stress risers, ease printing |
| **Tolerances/clearances** | **0.2 mm** loose fit, **0.1–0.15 mm** snug, **0.3–0.4 mm** for moving/assembly | Printers over-extrude; nominal = stuck |
| **Min feature/text** | Embossed ≥ 0.8 mm, engraved text easier than embossed | Below nozzle width won't resolve |

**Orientation is the most important free decision.** It sets where supports go, where the weak layer-adhesion axis points (loads should run *along* layers, not pull them apart), the surface finish (top/sides vs support-scarred bottom), and print time. Orient to: minimize supports, put strength along the load path, and keep critical surfaces support-free.

**Supports:** needed for overhangs >45° and unsupported islands. Tree/organic supports use less material and peel cleaner; keep support interfaces off cosmetic faces. Best part = no supports (reorient or split the model first).

## Material selection (FDM)

| Material | Use it for | Watch |
|---|---|---|
| **PLA** | Easy prints, prototypes, detail, looks | Low heat resistance (~60°C), brittle |
| **PETG** | Functional, tougher, some flex, watertight-ish | Stringing; less crisp detail |
| **ABS/ASA** | Heat/impact, outdoor (ASA UV-stable) | Warps — needs enclosure; fumes |
| **TPU** | Flexible parts, gaskets, grips | Slow; direct-drive extruder preferred |
| **Nylon / PC / CF-filled** | Engineering loads, stiffness, heat | Moisture-sensitive; high temps; hard to print |

Resin (SLA/MSLA): far finer detail (minis, dental, smooth surfaces), but brittle-ish, messy, needs wash + UV cure, and design rules differ (drain holes for hollows, supports on a raft, anti-suction).

## Slicer settings — sane defaults, then adjust

- **Layer height:** 0.2 mm general; 0.12–0.16 for detail/curves; 0.28–0.3 for speed/strength on big parts. (≤ ~75% of nozzle dia.)
- **Walls/perimeters:** 3+ for strength (walls add more strength than infill).
- **Infill:** 15–20% typical; 30–50%+ for load-bearing; gyroid/cubic for isotropic strength.
- **Top/bottom layers:** 4–6 (≈ 0.8–1.2 mm) for a solid surface.
- **Temps:** PLA ~200–215°C / bed 50–60; PETG ~230–245 / 70–80; ABS ~240–260 / 100–110 (enclosure). Tune per filament.
- **Speed:** 40–60 mm/s general; slow first layer; slow for quality/tall thin parts.
- **Cooling:** 100% for PLA; low/off for ABS (warping) and first layers; moderate for PETG.
- **Adhesion:** brim for small/tall; raft rarely needed on FDM; clean/level bed first.

## Diagnosing failures (quick map)

- **Warping / corners lifting** → bed adhesion + ABS shrinkage: enclosure, brim, higher bed temp, no draft.
- **Weak / splitting layers** → low temp, fast cooling on ABS, or load pulling across layers (reorient).
- **Stringing / blobs** → retraction + too-hot (esp. PETG); dry filament.
- **Poor overhangs / drooping** → more cooling, supports, or reorient; lower layer height.
- **First layer not sticking** → level/clean bed, Z-offset, temp, brim.
- **Elephant's foot** (bulging base) → lower bed temp, Z-offset, first-layer compensation.
- **Under/over-extrusion** → flow calibration, clog, filament diameter, temp.
- **Dimensional inaccuracy** → flow + steps calibration; account for hole shrinkage in design.

## The STL analyzer

`scripts/stl_info.py` parses a binary or ASCII STL (stdlib only) and reports the facts you need before printing.

```bash
python3 scripts/stl_info.py model.stl
```
Reports: triangle count, **bounding box (mm)** and whether it fits a given bed, **volume** (→ rough filament/cost estimate), surface area, and **degenerate/zero-area triangle** count (a sign of a bad mesh). Add `--bed 220x220x250` to flag fit; `--density 1.24 --price 22` for a PLA mass/cost estimate.

```bash
python3 scripts/stl_info.py model.stl --bed 256x256x256 --density 1.24 --price 22
```

## Chat output format

```
**Print plan — bracket.stl**

Size 48×30×12 mm · vol 9.4 cm³ · 1.2k triangles · mesh clean ✅
Fits 220×220 bed ✅ · ~11.7 g PLA ≈ $0.26

🧭 Orientation: flat on the back face → no supports, strength along the 2 bolt tabs.
🧱 Material: PETG (functional, some flex). Layer 0.2mm, 4 walls, 25% gyroid infill.
🌡️ 240°C / bed 75°C, 100% fan after layer 2.
⚠️ The 3.0mm holes will print ~2.8mm — model at 3.2mm or ream for M3.
```

## Workflow

1. **Get the model + intent:** prototype vs functional? loads? cosmetic? fit with other parts?
2. **Run `stl_info.py`** — size, bed fit, volume/cost, mesh sanity.
3. **Assess design-for-printing** (walls, overhangs, clearances) — propose fixes (→ `openscad`/`cadquery` to edit).
4. **Pick orientation** (supports, strength axis, surface finish) — usually the highest-impact call.
5. **Choose material** for the use case; **recommend slicer settings**.
6. **If it failed,** diagnose from the symptom map and give the specific fix.
7. **Deliver** the print plan; loop back to the CAD skill for geometry changes.

## Key pitfalls

- **Ignoring orientation.** It dominates supports, strength, finish, and time — decide it deliberately, first.
- **Nominal clearances.** 0.0 mm gaps fuse. Add 0.1–0.4 mm depending on fit; printers over-extrude.
- **Loading across layers.** FDM is weak in Z (between layers) — orient so forces run along layers, not pulling them apart.
- **Infill over walls.** Walls add strength more efficiently; bump perimeters before cranking infill.
- **Wrong material for the job.** PLA for a hot car interior or a living hinge will fail — match material to environment and load.
- **Trusting a bad mesh.** Non-manifold/degenerate STLs slice wrong; check before printing (and fix in CAD, not the STL).
- **Over-supporting.** Supports scar surfaces and waste filament — reorient or split to avoid them.

## Quick reference

- Clearances: 0.2 mm loose · 0.1–0.15 snug · 0.3–0.4 moving/assembly.
- Overhang limit ≈ 45°; bridges short only; holes print undersize (oval if vertical).
- Walls ≥ 3 perimeters (strength > infill); top/bottom 4–6 layers; infill 15–20% (more for load).
- Layer height ≤ ~75% nozzle (0.2 general; 0.12 detail; 0.3 speed).
- Temps: PLA 200–215/55 · PETG 230–245/75 · ABS 240–260/105 (enclosure).
- Orientation sets supports + strength (along layers) + finish — the key decision.
- Mesh sanity: `stl_info.py` (degenerate-triangle + bbox + volume check); fix geometry in `openscad`/`cadquery`.
