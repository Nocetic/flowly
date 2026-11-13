---
name: cadquery
description: "Build precise parametric 3D CAD models in Python with CadQuery — a B-rep (BREP/solid) kernel that exports STEP (for real CAD/CAM) as well as STL/3MF. Covers workplanes, the fluent API, face/edge selectors, fillets/chamfers, sketches, sweeps/lofts/revolves, and assemblies. Use when the user wants engineering-grade CAD, a STEP file, filleted/chamfered parts, something to hand to a machinist or import into FreeCAD/SolidWorks, or parametric CAD driven by Python logic."
metadata: {"flowly":{"emoji":"🔩","tags":["engineering","cad","cadquery","python","parametric","brep","step","3d-modeling"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["openscad","3d-printing","mechanical-engineering","engineering-units"]}}
---

# CadQuery — Engineering CAD in Python

CadQuery is parametric CAD written as Python, built on the OpenCASCADE B-rep kernel — the same class of geometry engine behind FreeCAD. The practical payoff over mesh-based code-CAD: **true solids with curved faces, proper fillets/chamfers, and STEP export** you can hand to a machinist, a CAM tool, or SolidWorks/Fusion. Reach for CadQuery when "3D-printable blob" isn't enough and you need real engineering geometry.

## What this skill produces

**Chat-first.** The deliverable is a clean, parametric **Python script** (delivered in chat) that, when run, exports **STEP** (engineering) and/or **STL/3MF** (printing). When CadQuery is installed, run it and attach the files; otherwise give the user the one-line run command and `pip` hint. Lead with the parameters block.

## When to use

- "I need a **STEP file** / a part for a machinist / CAM."
- "Design a \<bracket/flange/housing\> with proper **fillets and chamfers**."
- "Parametric CAD driven by calculations / a loop / a table of sizes."
- "Something I can open in FreeCAD / Fusion / SolidWorks."
- "Engineering-grade model" (tolerances, mating features, threads via plugins).

Choose `openscad` instead for quick mesh-only prints or when the user prefers SCAD; choose `cadquery` when B-rep, fillets, or STEP matter. Print prep → `3d-printing`.

## The mental model: workplanes + the fluent chain

You select a **workplane**, draw 2D geometry on it, then give it depth — chaining operations fluently. The "stack" carries the current selection (faces/edges/wires) that the next operation acts on.

```python
import cadquery as cq

result = (
    cq.Workplane("XY")          # start on the XY plane
    .box(40, 30, 10)            # 40×30×10 mm solid
    .faces(">Z")                # select the top face
    .workplane()                # new workplane on it
    .hole(6)                    # bore a 6mm through-hole (centered)
    .edges("|Z")                # select vertical edges
    .fillet(2)                  # round them 2mm
)
cq.exporters.export(result, "part.step")
cq.exporters.export(result, "part.stl")
```

Core building ops: `.box() .cylinder() .sphere()`, sketch + `.extrude() .cutThruAll() .cutBlind() .revolve() .sweep() .loft()`, `.hole() .cboreHole() .cskHole()`, `.shell(t)` (hollow), `.fillet(r) .chamfer(d)`, booleans `.union() .cut() .intersect()`.

## Selectors — the superpower (and the gotcha)

CadQuery picks geometry with **string selectors**; getting these right is most of the skill:
- Faces by direction: `.faces(">Z")` (top), `.faces("<Z")` (bottom), `.faces(">X")`, etc.
- Edges: `.edges("|Z")` (parallel to Z — the vertical edges), `.edges(">Z")` (topmost), `.edges("%Circle")` (circular edges).
- Combine: `.edges("|Z and >X")`, nearest-to: `.edges(cq.selectors.NearestToPointSelector((x,y,z)))`.
- `.vertices()`, `.wires()`, tag with `.tag("name")` and recall with `.workplaneFromTagged("name")`.

The gotcha: a selector that matches **zero** or **too many** elements silently does the wrong thing. After a tricky selection, verify (count, or export and eyeball). Prefer tagging stable references over fragile directional chains in complex parts.

## Parametric design

Variables on top, derive everything, wrap reusable geometry in functions:

```python
import cadquery as cq

# ---- Parameters (mm) ----
L, W, H   = 40, 30, 12     # outer
wall      = 2
bolt_d    = 3.2            # M3 clearance
fillet_r  = 2

def bracket(L, W, H, wall, bolt_d, fillet_r):
    b = (cq.Workplane("XY").box(L, W, H)
         .faces(">Z").shell(-wall)          # hollow from the top
         .edges("|Z").fillet(fillet_r))
    # 4 mounting holes via a rectangular array
    b = (b.faces("<Z").workplane()
         .rect(L-2*5, W-2*5, forConstruction=True).vertices()
         .hole(bolt_d))
    return b

cq.exporters.export(bracket(L,W,H,wall,bolt_d,fillet_r), "bracket.step")
```

Because it's Python, you get loops, conditionals, `math`, and data-driven generation (a table of sizes → a family of parts) for free.

## Sketches, sweeps, assemblies

- **Sketch API** (`.sketch()...finalize()`) for complex 2D with constraints/fillets before extrude.
- `.revolve(angle)` for turned parts; `.sweep(path)` along a wire; `.loft()` between profiles.
- **Assemblies:** `cq.Assembly().add(part, loc=cq.Location((x,y,z)), color=...)`, constraints, and export the whole thing to STEP — for multi-part designs and fit checks.

## Export

- `cq.exporters.export(obj, "part.step")` — **STEP** (AP214): the engineering interchange format; preserves true geometry.
- `.stl` / `.3mf` for printing (set tolerance for mesh quality), `.dxf` for 2D profiles, `.svg` for drawings.
- Prefer STEP when the user will edit it in real CAD; STL only for the printer.

## Running it (and the no-install path)

CadQuery isn't pure-stdlib (it ships the OCP/OpenCASCADE kernel). To run:
```bash
pip install cadquery        # or: conda install -c conda-forge cadquery
python3 part.py             # writes the exported files
```
If it isn't installed, **still deliver the full script** and the two commands above — the user runs them locally. (Tip: `cq-editor` gives a live GUI preview of the same script.)

## Chat output format

````
Parametric enclosure base — B-rep, exports STEP + STL. Edit the params.

```python
import cadquery as cq
L, W, H = 60, 40, 20; wall = 2.5; ...
... (full script) ...
cq.exporters.export(part, "enclosure.step")
```

Run: `pip install cadquery && python3 enclosure.py`
✅ (if run here) enclosure.step + enclosure.stl attached — 60×40×20 mm.
Fillets 2mm, M3 bosses. For print settings → 3d-printing.
````

## Workflow

1. **Clarify** geometry, tolerances/fits, mating features, and the **target format** (STEP for CAD/CAM, STL for print).
2. **Plan the feature tree:** base solid → cuts/holes → shell → fillets/chamfers (order matters — fillet last, usually).
3. **Write parametric Python**, variables on top; use functions for reuse and loops for arrays/families.
4. **Get selectors right** — verify tricky selections; tag stable references.
5. **Export** STEP and/or STL; run it if CadQuery is available, else hand over the run commands.
6. **Sanity-check** for the use case (print: walls/clearances → `3d-printing`; load-bearing: → `mechanical-engineering`).

## Key pitfalls

- **Selector matches nothing / too much.** The #1 CadQuery bug — silently wrong geometry. Verify counts; tag stable refs in complex parts.
- **Fillet/chamfer ordering & radius.** Filleting too early, or a radius larger than the adjacent geometry, throws kernel errors — add fillets late and keep radii feasible.
- **Workplane confusion.** Operations act on the current workplane/selection; losing track misplaces features. Re-select deliberately (`.faces(...).workplane()`).
- **`shell` sign/face.** `.shell(-t)` hollows inward; pick the face(s) to open carefully or you get a closed/odd shell.
- **Exporting STL when STEP was wanted.** STL is a faceted mesh — lossy and not editable as CAD. Match the format to the user's downstream tool.
- **Assuming it's installed.** It carries a heavy native kernel; always include the `pip install` + run command so the script is usable regardless.
- **Mesh tolerance too coarse** on STL export — curved faces look faceted; set a finer linear/angular tolerance for printing.

## Quick reference

- Start: `cq.Workplane("XY")`; build: `.box .cylinder`, `.extrude .revolve .sweep .loft`, `.hole .cboreHole .cskHole`, `.shell .fillet .chamfer`, `.union .cut .intersect`.
- Selectors: faces `>Z/<Z/>X`, edges `|Z` (parallel), `>Z` (topmost), `%Circle`; `.tag()/.workplaneFromTagged()`.
- Arrays: `.rect(w,h,forConstruction=True).vertices().hole(d)`; or Python `for` loops.
- Export: `cq.exporters.export(obj, "x.step")` (engineering) · `.stl/.3mf` (print) · `.dxf/.svg` (2D).
- Install/run: `pip install cadquery` → `python3 part.py`; preview with `cq-editor`.
- STEP for CAD/CAM, STL for the printer. Fillet last. Verify selectors.
