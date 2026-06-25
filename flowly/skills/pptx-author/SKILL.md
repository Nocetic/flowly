---
name: pptx-author
description: Build PowerPoint decks headless with python-pptx. Pairs with excel-author for model-backed decks where every number traces to a workbook cell. Use for pitch decks, IC memos, earnings notes.
version: 1.0.0
license: Apache-2.0
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"📈","tags":["powerpoint","pptx","python-pptx","presentation","finance"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["excel-author","dcf-model","3-statement-model"]}}
---

# pptx-author

This skill generates a `.pptx` file directly on disk through the `python-pptx` library, with no PowerPoint application running. Reach for it whenever the deliverable is a presentation saved as a file artifact rather than edits made inside a live, open document.

It is deliberately narrow: a repeatable recipe for finance-oriented decks — pitch books, investment-committee memos, earnings write-ups — where the figures on each slide originate in a spreadsheet model and must stay faithful to it. For general slide work (rich speaker notes, embedded media, animations), use the broader built-in `powerpoint` skill instead.

## What you must produce

- Save the result to `./out/<name>.pptx`, creating the `./out/` directory first if it is missing.
- Report the relative path back in your closing message so the caller knows where the file landed.

## Installing the dependency

```bash
pip install "python-pptx>=0.6"
```

## Authoring principles

### One slide, one argument

The title carries the conclusion; everything below it is supporting evidence. Prefer a headline that states the finding — "Revenue growth accelerated to 14% Y/Y in Q3" — over a bare label like "Q3 Revenue." A reader scanning only the titles should still follow the story.

### Numbers are bound to the model, not typed from memory

Any figure shown on a slide should be traceable to the cell it came from. Annotate it with the originating sheet and coordinate:

```
Revenue: $1,250M  (Source: model.xlsx, Inputs!C3)
```

Do not copy values out of your head or from a prose summary. Open the workbook, read the cell or named range, and — wherever feasible — write the deck value programmatically so the two cannot diverge.

### Inherit branding from a mounted template

When a firm template is present at `./templates/firm-template.pptx`, open the deck from it. The new presentation then picks up the template's master layouts, palette, and fonts automatically.

```python
from pptx import Presentation
from pathlib import Path

template = Path("./templates/firm-template.pptx")
prs = Presentation(str(template)) if template.exists() else Presentation()
```

### Embed chart images instead of native pptx charts

`python-pptx`'s built-in chart objects are limited and rarely reproduce a firm's exact styling. When a chart's appearance has to match the source model precisely, render it to a PNG from the workbook and drop that image onto the slide:

```python
from pptx.util import Inches
slide.shapes.add_picture("./out/charts/football_field.png",
                         Inches(1), Inches(2),
                         width=Inches(8))
```

### This skill only writes files

It produces a `.pptx` and stops there. It does not email, upload, or post the deck anywhere — delivery is the responsibility of whatever orchestration calls it.

## A starting skeleton

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pathlib import Path

template = Path("./templates/firm-template.pptx")
prs = Presentation(str(template)) if template.exists() else Presentation()

# Title slide
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Project Aurora — Strategic Alternatives"
slide.placeholders[1].text = "Preliminary Discussion Materials"

# Valuation summary slide (title-only layout)
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Valuation implies $38–$52 per share across methodologies"

# Add a table bound to model outputs
rows, cols = 5, 4
tbl_shape = slide.shapes.add_table(rows, cols,
                                   Inches(0.5), Inches(1.5),
                                   Inches(9), Inches(3))
tbl = tbl_shape.table
headers = ["Methodology", "Low ($)", "Mid ($)", "High ($)"]
for c, h in enumerate(headers):
    tbl.cell(0, c).text = h

# In a real deck, read these from the model workbook with openpyxl
data = [
    ("Trading comps",     "35", "41", "48"),
    ("Precedent M&A",     "39", "45", "52"),
    ("DCF (base)",        "36", "43", "51"),
    ("LBO (10% IRR)",     "33", "38", "44"),
]
for r, row in enumerate(data, start=1):
    for c, val in enumerate(row):
        tbl.cell(r, c).text = val

# Embed a chart rendered from the model
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Football field — current price $42"
slide.shapes.add_picture("./out/charts/football_field.png",
                         Inches(1), Inches(1.8), width=Inches(8))

Path("./out").mkdir(exist_ok=True)
prs.save("./out/pitch-aurora.pptx")
```

## Wiring slide values back to the workbook

To keep the deck and the model in lockstep, pull named ranges or explicit cells straight out of the Excel file at build time:

```python
from openpyxl import load_workbook

wb = load_workbook("./out/model.xlsx", data_only=True)
def nr(name):
    """Resolve a named range to its current computed value."""
    rng = wb.defined_names[name]
    sheet, coord = next(rng.destinations)
    return wb[sheet][coord].value

revenue_fy24 = nr("RevenueFY24")
implied_mid  = nr("ImpliedSharePriceBase")
```

Then compose slide text from those resolved values:

```python
slide.shapes.title.text = f"Implied share price of ${implied_mid:.2f} (base case)"
```

One caveat: `openpyxl` reads cached results, not live formulas. With `data_only=True` it returns whatever value was last computed and stored in the file, so a workbook that has never been calculated will hand back `None`. Recalculate the sheet before reading — run the recalc helper from the `excel-author` skill, or open and re-save the workbook through a real Excel session — so the cached values are current.

## A reference outline for pitch decks

Banking pitch books tend to follow a recognizable arc. Treat the list below as scaffolding to adapt, not a rigid template:

1. Cover / title
2. Disclaimer
3. Table of contents
4. Situation overview
5. Company snapshot (the target)
6. Market / sector context
7. Valuation summary (football field) — the centerpiece
8. Trading comps detail
9. Precedent transactions detail
10. DCF summary
11. Illustrative LBO / sponsor case
12. Process considerations
13. Appendix

## When to choose a different tool

- A live PowerPoint session is open and an Office integration can edit it directly — drive that document instead of writing a new file.
- The deck is general-purpose slideware (all-hands updates, marketing material) rather than finance content — use the broader `powerpoint` skill.
- The deck leans heavily on animations, transitions, or detailed speaker notes — again, the broader `powerpoint` skill is the better fit.
