---
name: openscad
description: "Design 3D models as parametric code with OpenSCAD — write .scad scripts that compile to STL/3MF/OFF for 3D printing or CAD. Covers the language (modules, functions, loops), CSG boolean modeling, 2D-sketch-and-extrude, transforms, smoothness ($fn/$fa/$fs), the BOSL2 library, and clean parametric design. Includes a render helper. Use when the user wants a 3D model, a printable part, a parametric enclosure/bracket/adapter, or asks to design something in OpenSCAD/code-CAD."
metadata: {"flowly":{"emoji":"📐","tags":["engineering","cad","openscad","3d-modeling","parametric","3d-printing","csg","stl"],"requires":{"bins":["python3"]},"optional_bins":["openscad"],"category":"engineering","related_skills":["cadquery","3d-printing","mechanical-engineering","engineering-units"]}}
---

# OpenSCAD — 3D Modeling as Code

OpenSCAD models a solid by *describing* it in a script, not by mouse-pushing in a GUI. That makes it ideal for a chat agent: you write a precise, parametric `.scad` file, the user (or the render helper) compiles it to an STL. The whole philosophy is **parametric and constructive** — build shapes by adding/subtracting primitives (CSG), and drive every dimension from named variables so the part can be re-flexed without a rewrite.

## What this skill produces

**Chat-first.** The primary deliverable is a clean, commented, parametric **`.scad` script** delivered in the chat (the user can render it anywhere). When OpenSCAD is available, also render an **STL/3MF** and a **preview PNG** via `scripts/render.py` and attach them. Always lead with the parametric variables so the user can tweak.

## When to use

- "Design a \<bracket / enclosure / adapter / spacer / knob / mount\>."
- "Make me a 3D-printable \<thing\> with these dimensions."
- "Write an OpenSCAD model for X." / "Parametric \<part\> in code."
- "I need an STL for \<part\>." (code-CAD route)
- "Make this box 5mm taller / add mounting holes" (edit an existing `.scad`).

For freeform/organic/B-rep surfaces, fillets-everywhere, or STEP output, prefer the `cadquery` skill. For print settings & STL sanity, hand to `3d-printing`.

## The mental model: CSG (constructive solid geometry)

You build solids by combining primitives with boolean operations:
- `union()` — merge (also the implicit default for a group of children).
- `difference()` — subtract every later child from the first (this is how you cut holes/pockets).
- `intersection()` — keep only the overlap.

Primitives: `cube([x,y,z], center=)`, `sphere(r=)`, `cylinder(h=, r1=, r2=, center=)`, `polyhedron(...)`. 2D: `square`, `circle`, `polygon`, `text`.

Transforms wrap their children: `translate([x,y,z])`, `rotate([x,y,z])`, `scale()`, `mirror()`, `hull()` (convex hull of children — great for rounded shapes), `minkowski()` (sum — for fillets/offsets, but slow).

```scad
difference() {
    cube([30, 20, 10], center=true);      // body
    translate([0,0,0]) cylinder(h=20, d=6, center=true);  // bored hole
}
```

## Parametric design (the whole point — do this every time)

Put **named variables at the top**, derive everything from them, and never hardcode a magic number twice.

```scad
// ---- Parameters ----
width      = 30;   // outer width (mm)
depth      = 20;
height     = 10;
wall       = 2;    // wall thickness
hole_d     = 3.2;  // M3 clearance
$fn        = 64;   // curve smoothness (see below)

// ---- Derived ----
inner_w = width - 2*wall;

module box() {
    difference() {
        cube([width, depth, height]);
        translate([wall, wall, wall])
            cube([inner_w, depth-2*wall, height]);  // hollow
    }
}
box();
```

- **Modules** = reusable parts (`module name(args){...}`); **functions** = return values (`function f(x)=...;`). Use modules to keep the model readable and composable.
- **Loops & logic:** `for (i=[0:n-1]) ...`, list comprehensions `[for (i=[0:4]) i*5]`, `if/else`. Use `for` to place hole arrays, fins, teeth, etc.
- **Comments on every parameter** with units — OpenSCAD is unitless; **treat 1 unit = 1 mm** (the universal convention for 3D printing) and say so.

## Smoothness: `$fn`, `$fa`, `$fs`

Curves are faceted polygons; these control facet count:
- `$fn` = fixed number of facets (e.g. `$fn=64`). Simple; set it high for the final render, lower (`$fn=24`) for fast preview.
- `$fa` (min angle) and `$fs` (min size) = adaptive; better for mixed-scale models.
Too low → blocky cylinders; too high → slow renders and huge STLs. Set a global `$fn` and override per-object when needed.

## 2D → 3D (sketch and extrude)

Often cleaner than stacking primitives:
- `linear_extrude(height=, twist=, scale=, center=)` a 2D shape into a prism.
- `rotate_extrude(angle=)` a 2D profile into a solid of revolution (vases, rings, pulleys).
- `offset(r=|delta=, chamfer=)` to grow/shrink/round a 2D outline before extruding.
- `projection()` to flatten 3D back to 2D.

```scad
linear_extrude(height=5) offset(r=2) square([20,10]);  // rounded slab
```

## Libraries (don't reinvent fillets and gears)

- **BOSL2** (the big one): rounded cuboids/cylinders, chamfers/fillets (`cuboid(..., rounding=2)`), threads (`threaded_rod`), gears, attachable parts, distributors. `include <BOSL2/std.scab>`. This is the single biggest quality multiplier — use it for anything needing rounding, threads, or gears.
- **MCAD** (bundled): nuts/bolts, gears, motors.
- **Round-Anything**, **threads.scad** for specific needs.
Tell the user which library a script needs and where to get it.

## The render helper

`scripts/render.py` wraps the OpenSCAD CLI to compile a `.scad` to a mesh and/or a preview image, passing parameters via `-D`.

```bash
python3 scripts/render.py model.scad --out part.stl                 # → STL
python3 scripts/render.py model.scad --out part.3mf --png prev.png  # mesh + preview
python3 scripts/render.py model.scad --out part.stl -D width=40 -D height=15
python3 scripts/render.py model.scad --check                        # syntax/compile only
```
If OpenSCAD isn't installed it prints the install hint and the exact CLI command to run — so the user can render the script you wrote even without the binary present.

## Chat output format

Deliver the script, then (if rendered) the files:

````
Parametric M3 cable clip — 1 unit = 1 mm. Tweak the top vars.

```scad
clip_d   = 6;    // cable diameter
gap      = 4;    // opening
wall     = 2;
$fn      = 64;
... (full script) ...
```

✅ Rendered: clip.stl (12×8×6 mm, 0.9 cm³). Preview attached.
Print: PLA, 0.2mm, no supports if printed opening-up. (→ 3d-printing)
````

## Workflow

1. **Clarify geometry & constraints:** dimensions, fit/clearances, mounting, what it mates with. Confirm units (assume mm).
2. **Choose the approach:** primitive CSG vs sketch-and-extrude; pull in BOSL2 if rounding/threads/gears are needed.
3. **Write parametric code** — variables on top, modules for parts, comments with units.
4. **Render/check** with `render.py` (or hand the user the command if no binary).
5. **Sanity-check** for printing: wall thickness, holes sized for clearance (e.g. M3 ≈ 3.2 mm), overhangs (→ `3d-printing`).
6. **Deliver** the `.scad` + STL/PNG; invite parameter tweaks; iterate.

## Key pitfalls

- **Hardcoded magic numbers.** Defeats the purpose — drive every dimension from a named, commented variable.
- **`difference()` with a hole the same height as the body.** Coplanar faces cause render artifacts/zero-thickness — make cutters slightly **oversized** (e.g. `h+0.2`, start at `-0.1`) so they poke through.
- **`$fn` too low on final / too high on preview.** Faceted cylinders or multi-minute renders. Use a low preview `$fn` and a high final one.
- **Non-manifold geometry.** Floating/just-touching bodies and self-intersections export bad STLs — keep overlaps positive; verify with `3d-printing`'s STL check.
- **Forgetting it's mm.** State the unit; size holes with real clearances (clearance > nominal for printed fits).
- **Reinventing rounding/threads.** Use BOSL2 instead of hand-rolling fillets with `minkowski()` (slow) or fragile hulls.
- **`center` confusion.** `cube` defaults to a corner at the origin; `cylinder`/`sphere` center on the axis. Mixing them misaligns parts — be deliberate with `center=`.

## Quick reference

- Booleans: `union()` (merge) · `difference()` (first minus rest) · `intersection()` (overlap).
- Primitives: `cube([x,y,z],center=)`, `cylinder(h=,d=|r1=,r2=)`, `sphere(d=)`, `polyhedron()`.
- Transforms: `translate`, `rotate`, `scale`, `mirror`, `hull` (rounding via spheres), `minkowski` (offset, slow).
- 2D→3D: `linear_extrude(h=,twist=,scale=)`, `rotate_extrude(angle=)`, `offset(r=)`.
- Smoothness: global `$fn` (e.g. 64 final / 24 preview), or `$fa`/`$fs`.
- Reuse: `module name(p=default){...}`, `for(i=[0:n]){...}`, list comprehensions.
- Convention: **1 unit = 1 mm**; make cutters oversized to avoid coplanar faces.
- Rounding/threads/gears → **BOSL2**. STEP/freeform → `cadquery`. Print prep → `3d-printing`.
