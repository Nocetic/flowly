---
name: pcb-kicad
description: "Work with KiCad PCB projects from the command line — run ERC/DRC checks, export Gerbers + drill files for fab, generate a BOM, plot schematics/PDFs, export netlists and STEP/3D, and pull position files for assembly. Covers the schematic→PCB→fabrication workflow and design rules. Includes a kicad-cli wrapper that degrades gracefully when KiCad isn't installed. Use when the user has a KiCad project to check/fabricate, asks about Gerbers, BOM, DRC/ERC, PCB design rules, or the path from schematic to manufactured board."
metadata: {"flowly":{"emoji":"🔌","tags":["engineering","pcb","kicad","electronics","gerber","drc","erc","bom","hardware"],"requires":{"bins":["python3"]},"optional_bins":["kicad-cli"],"category":"engineering","related_skills":["circuit-analysis","control-systems","engineering-units","mechanical-engineering"]}}
---

# PCB / KiCad — From Schematic to Fab Files

KiCad is the open-source EDA suite; this skill covers the **scriptable, headless** parts via `kicad-cli` — the operations a chat agent can actually drive: validate a design (ERC/DRC), and turn a finished project into the files a fab house needs (Gerbers, drill, BOM, pick-and-place). The schematic-capture and interactive layout happen in the GUI; the agent's job is checking, exporting, and advising on design rules.

## What this skill produces

**Chat-first.** Default: run the requested check/export and report the result inline (DRC violation count, exported file list, BOM summary). When `kicad-cli` is absent, hand over the exact commands and install hint. For fabrication, produce the Gerber/drill/BOM/position set and summarize what to upload.

## When to use

- "Run DRC/ERC on my KiCad board." / "Are there errors?"
- "Generate Gerbers / fab files for \<board\>." / "Export for JLCPCB/PCBWay."
- "Make a BOM." / "Pick-and-place / position file?"
- "Export the schematic to PDF." / "Netlist?" / "3D/STEP of the board?"
- "What design rules / trace width / clearance for X?"
- "What's the path from my schematic to a manufactured board?"

## The workflow (schematic → board → fab)

1. **Schematic capture** (`.kicad_sch`) — draw the circuit, assign components, annotate references, run **ERC** (electrical rules: unconnected pins, conflicting outputs, missing power flags).
2. **Assign footprints** — map each schematic symbol to a physical footprint.
3. **PCB layout** (`.kicad_pcb`) — place components, route traces, pour copper, respect the **design rules**; run **DRC** (design rules: clearance, track width, drill, annular ring violations).
4. **Fabrication outputs** — once DRC is clean: **Gerbers** (copper/mask/silk layers) + **drill files** + **BOM** + **pick-and-place (position)** file. That bundle goes to the fab.

`kicad-cli` automates steps that don't need the GUI: ERC/DRC, all the exports, and schematic plotting.

## kicad-cli — the headless commands

```bash
kicad-cli sch erc board.kicad_sch -o erc.rpt              # electrical rules check
kicad-cli pcb drc board.kicad_pcb -o drc.rpt              # design rules check
kicad-cli pcb export gerbers board.kicad_pcb -o gerbers/  # copper/mask/silk
kicad-cli pcb export drill   board.kicad_pcb -o gerbers/  # drill files
kicad-cli sch export bom board.kicad_sch -o bom.csv       # bill of materials
kicad-cli sch export pdf board.kicad_sch -o schematic.pdf
kicad-cli sch export netlist board.kicad_sch -o board.net
kicad-cli pcb export pos     board.kicad_pcb -o pos.csv   # pick-and-place
kicad-cli pcb export step    board.kicad_pcb -o board.step  # 3D model
kicad-cli version
```
The `scripts/kicad_cli.py` helper wraps these into one-word actions (`erc`, `drc`, `gerbers`, `fab`, `bom`, `pdf`, `step`, `pos`), creates output dirs, and **prints the install hint + exact command when `kicad-cli` is missing** so the user can run it locally.

```bash
python3 scripts/kicad_cli.py erc board.kicad_sch
python3 scripts/kicad_cli.py fab board.kicad_pcb --out fab/    # gerbers + drill + pos in one go
python3 scripts/kicad_cli.py bom board.kicad_sch --out bom.csv
```

## Design rules (what to advise on)

You can't route in the GUI from chat, but you can advise the rules that make a board manufacturable:
- **Trace width vs current:** wider = more current and less drop. Rule of thumb (1 oz copper, ~10°C rise): ~0.25 mm (10 mil) ≈ 1 A; ~0.5 mm ≈ 2 A; scale up for power. Use an IPC-2221 calculator for real specs; for signals, width is about impedance/routing, not current.
- **Clearance:** depends on voltage (creepage/clearance) and fab capability; **6 mil (0.15 mm)** track/space is a safe cheap-fab minimum, 4 mil for advanced. Higher voltage needs more spacing.
- **Vias:** typical 0.3 mm drill / 0.6 mm pad on cheap fabs; check the fab's capability sheet.
- **Annular ring, drill sizes, edge clearance** — match the fab's minimums (their DRC rule file).
- **Layer stackup:** 2-layer is cheapest; 4-layer when you need a ground plane for signal integrity / power.
- **Decoupling, ground planes, return paths** — keep decoupling caps close to ICs; give high-speed signals a continuous reference plane.
- Set the **board's design rules to the fab's capabilities** before DRC, so "clean DRC" actually means "manufacturable here."

## Fab handoff

- Most fabs (JLCPCB, PCBWay, OSH Park, etc.) accept a **zip of Gerbers + drill**. KiCad's defaults work for most; some fabs prefer specific layer naming — check their KiCad guide.
- For **assembly (PCBA)**, add the **BOM** (with their part numbers, e.g. LCSC for JLCPCB) and the **pick-and-place** file; mind rotation/origin conventions (a frequent assembly headache).
- Always **re-run DRC clean** before generating fab files, and eyeball the Gerbers in a viewer.

## Chat output format

```
**KiCad — board.kicad_pcb**

✅ DRC: 0 violations, 0 unconnected (report: drc.rpt)
📦 Fab files → fab/: 8 Gerber layers + drill + position file
📋 BOM: 24 lines, 41 components (bom.csv)

Upload fab/*.zip to your fab. For JLC assembly, add LCSC part #s to the BOM
and include the position file. Confirm min track/space (6 mil) matches their
capability before ordering.
```

## Workflow

1. **Identify the project files** (`.kicad_sch`, `.kicad_pcb`) and the goal (check vs export vs advise).
2. **Validate first:** ERC on the schematic, DRC on the board — fix-list any violations before exporting.
3. **Export** what's needed via `kicad_cli.py` (gerbers/drill/BOM/pos/pdf/step); hand over commands if no binary.
4. **Advise design rules** for the fab and use case (trace width for current, clearance for voltage, stackup).
5. **Fab handoff:** zip Gerbers+drill; add BOM+position for assembly; remind to DRC-clean and eyeball Gerbers.
6. **Deliver** results + the upload checklist; route circuit math to `circuit-analysis`, enclosure/3D fit to `cadquery`/`mechanical-engineering`.

## Key pitfalls

- **Exporting before DRC is clean.** Generating Gerbers from a board with violations ships errors to the fab — DRC clean first, always.
- **DRC against KiCad defaults, not the fab's rules.** "Clean" only means manufacturable if the rules match your fab's capabilities — load their constraints first.
- **Trace too thin for the current.** Power traces sized like signal traces overheat — size by current (IPC-2221), not by what routes neatly.
- **Pick-and-place rotation/origin mismatch.** The top assembly failure mode — verify the fab's rotation and origin conventions.
- **Forgetting drill files / NPTH.** Gerbers without the drill file are incomplete; include plated and non-plated holes.
- **No ground plane on fast signals.** Routing high-speed traces with a broken return path causes EMI/signal-integrity problems — give them a continuous reference.
- **Assuming kicad-cli is present.** It ships with KiCad 7+; if absent, hand over the commands + install hint (the wrapper does this).

## Quick reference

- Flow: schematic (ERC) → footprints → PCB layout (DRC) → Gerbers + drill + BOM + position → fab.
- `kicad-cli`: `sch erc`, `pcb drc`, `pcb export gerbers|drill|pos|step`, `sch export bom|pdf|netlist`.
- DRC clean (against the **fab's** rules) before exporting; eyeball Gerbers in a viewer.
- Trace width ≈ 0.25 mm/A (1 oz, 10°C rise, rough — use IPC-2221); cheap-fab min track/space ~6 mil.
- Fab handoff: zip Gerbers+drill; PCBA adds BOM (their part #s) + position (watch rotation/origin).
- 2-layer cheapest; 4-layer for a ground plane / power integrity.
