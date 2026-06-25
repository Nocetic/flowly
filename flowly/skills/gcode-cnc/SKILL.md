---
name: gcode-cnc
description: "Work with CNC machining and G-code — feeds & speeds (spindle RPM from surface speed, feedrate from chip load), material/tool cutting data, common G/M codes, work offsets and coordinate systems, safe program structure, and basic toolpath generation plus G-code sanity-checking (bounding box, modal/safety checks). Includes a stdlib helper. Use when the user asks about feeds and speeds, RPM/feedrate, G-code, a CNC program, milling/turning/drilling parameters, or to generate or check a toolpath."
metadata: {"flowly":{"emoji":"🛠️","tags":["engineering","cnc","gcode","machining","feeds-speeds","milling","manufacturing","toolpath"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["mechanical-engineering","cadquery","materials-selection","engineering-units"]}}
---

# CNC & G-code — Feeds, Speeds, and Safe Toolpaths

CNC has two failure modes that matter: **wrong feeds & speeds** (burned tools, broken endmills, ruined surface finish) and **unsafe/incorrect G-code** (crashes into the part, fixture, or machine). This skill nails the cutting parameters from material + tool data, and helps write/check G-code that won't crash. Conservative defaults beat aggressive ones — a slow cut finishes; a broken tool doesn't.

> **Safety:** generated G-code is a starting point. The operator must verify on the specific machine — work offsets, tool lengths, clearances, and a dry run / single-block first. Never run un-simulated G-code at full rate on real stock.

## What this skill produces

**Chat-first.** Default: the computed RPM/feedrate with the inputs and a sanity check, or a short annotated G-code snippet, or a check report on pasted G-code. The `cnc.py` helper does feeds & speeds, has a material/tool table, generates simple patterns, and bounding-box-checks G-code. Offer a fuller program for multi-op jobs (and always say "verify on your machine").

## When to use

- "What RPM / feedrate for \<material\> with a \<tool\>?" / "Feeds and speeds?"
- "Write G-code to \<face / drill a pattern / cut a pocket\>."
- "Check this G-code." / "What's the bounding box / will it fit my stock?"
- "Explain G54 / G00 vs G01 / canned cycles / M-codes."
- "Why is my tool chattering / burning / breaking?"

## Feeds & speeds (the core calculation)

Two numbers drive everything: **spindle speed (RPM)** and **feedrate**.
- **Cutting/surface speed → RPM:** RPM = (Vc × 1000) / (π × D) for metric (Vc in m/min, D mm), or RPM = (Vc × 12)/(π × D) imperial (Vc in SFM=ft/min, D inch). Vc is a **material+tool property** (e.g. aluminium HSS ~70–120 m/min, steel carbide ~150–250).
- **RPM → feedrate:** Feed (mm/min) = RPM × N × f_z, where N = number of flutes, f_z = **chip load** (feed per tooth, a material/tool/diameter property, e.g. ~0.025–0.1 mm/tooth for small endmills).
- **Depth of cut:** axial (DOC) and radial (WOC/stepover). Conservative: axial ≤ 0.5–1× D, radial ≤ 0.3–0.5× D for slotting/profiling; lighter for hard materials. Trochoidal/adaptive paths allow deeper axial at low radial.
- **Material removal rate (MRR)** = WOC × DOC × feed — the productivity number; bounded by spindle power and tool/rigidity.

Get Vc and f_z from the **tool manufacturer's data** for the exact tool+material; the built-in table is a sane starting point, not gospel.

## G-code essentials

| Code | Meaning | | Code | Meaning |
|---|---|---|---|---|
| G00 | rapid move (positioning, **not cutting**) | | G20/G21 | inch / mm units |
| G01 | linear feed (cutting) | | G90/G91 | absolute / incremental |
| G02/G03 | CW / CCW arc | | G54–G59 | work coordinate offsets |
| G17/18/19 | plane select (XY/XZ/YZ) | | G43 | tool length offset |
| G28 | return to home | | M03/M04/M05 | spindle CW/CCW/stop |
| G81/G83 | drill / peck-drill canned cycle | | M06 | tool change |
| | | | M08/M09 | coolant on/off |

**Safe program structure:** header (units G21, absolute G90, plane G17, work offset G54) → tool change + length offset → spindle on at safe Z → operations (rapid to position at clearance, then feed down) → retract to safe Z → spindle/coolant off → home → M30. **Never rapid (G00) through the stock**; always retract to a clearance plane before rapids.

## Toolpath basics

- **Climb vs conventional milling:** climb (cutter rotation with feed direction) gives better finish and tool life on rigid CNC machines — prefer it; conventional for backlash-heavy/manual machines.
- **Drilling:** peck (G83) for deep holes to clear chips; spot-drill first for accuracy.
- **Pockets/profiles:** leave a finishing pass (small radial stock) for a clean wall; ramp or helix into pockets rather than plunging straight down.
- **Tabs/holding:** keep parts secured through the final cut.

## The helper

`scripts/cnc.py` (stdlib):
```bash
python3 scripts/cnc.py speeds --material aluminium --tool carbide --dia 6 --flutes 2   # RPM + feed
python3 scripts/cnc.py speeds --vc 100 --dia 6 --flutes 2 --chipload 0.04              # explicit
python3 scripts/cnc.py materials                                                       # show the table
python3 scripts/cnc.py face --width 50 --length 80 --dia 6 --stepover 0.5 --rpm 8000 --feed 1200  # G-code
python3 scripts/cnc.py drill --holes "10,10 30,10 50,10" --depth 5 --rpm 5000 --feed 200          # G81 pattern
python3 scripts/cnc.py check program.nc                                                # bbox + safety scan
```
Stdlib only.

## Chat output format

```
**Feeds & speeds — 6mm 2-flute carbide in aluminium**

Vc 120 m/min → RPM = 120·1000/(π·6) = 6,366 → use ~6,400 RPM
Chip load 0.04 mm/tooth → Feed = 6400·2·0.04 = 512 mm/min
Suggested: axial DOC ≤ 6mm, radial ≤ 3mm (slotting lighter). MRR scales with both.
⚠️ Starting point from generic data — confirm with the tool's datasheet and
   take a conservative first pass. Verify offsets and dry-run on the machine.
```

## Workflow

1. **Get material + tool + diameter + flutes** (and machine limits: max RPM, rigidity).
2. **Compute RPM then feed** (`speeds`) from Vc and chip load; set conservative DOC/WOC.
3. **Generate or review G-code** (`face`/`drill`, or `check` on pasted code) — verify safe structure (units, offsets, clearance retracts, no rapids through stock).
4. **Bounding-box check** vs stock; confirm work-offset and tool-length assumptions.
5. **Deliver** parameters/code + the mandatory "verify + dry-run" caveat; route part geometry to `cadquery`, cutting-force/rigidity to `mechanical-engineering`, material data to `materials-selection`.

## Key pitfalls

- **Wrong feeds & speeds.** Too-high RPM/too-low feed burns/rubs (work-hardens steel, melts aluminium onto the tool); too-low RPM/too-high feed snaps tools. Start from real datasheet Vc/f_z, conservatively.
- **Rapiding through stock.** G00 doesn't avoid material — always retract to a clearance plane before rapids, or you crash.
- **Units mismatch (G20/G21).** A program in inches run as mm (or vice versa) is a guaranteed crash. Always set units in the header.
- **Wrong/zeroed work offset (G54).** The program is relative to the part zero you set — verify it before running.
- **Forgetting tool-length offset (G43).** Different tools, different lengths — without G43 the Z is wrong.
- **Plunging straight into a pocket.** Ramp/helix in; straight plunges with a non-center-cutting endmill break tools.
- **Climb vs conventional on a loose machine.** Climb on a backlash-heavy machine pulls the cutter into the work — use conventional there.
- **Running un-simulated.** Always simulate/dry-run/single-block a new program before committing to stock.

## Quick reference

- RPM = Vc·1000/(π·D) [metric] or Vc·12/(π·D) [SFM, inch]. Feed = RPM·flutes·chipload.
- Chip load f_z and surface speed Vc come from the tool+material datasheet (table = starting point).
- DOC ≤ ~1×D axial, WOC ≤ ~0.3–0.5×D radial (lighter for hard materials); MRR = DOC·WOC·feed.
- G00 rapid (not cutting) · G01 feed · G02/03 arcs · G54 offset · G43 tool length · G83 peck-drill.
- Safe header: G21 G90 G17 G54; retract to clearance before every rapid; M30 to end.
- Climb mill on rigid CNC; ramp/helix into pockets; peck deep holes. Always verify + dry-run.
