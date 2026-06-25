---
name: data-visualization
description: "Choose and build the right chart for data — pick the chart type from the data shape and the message, apply sound visual-encoding principles, and generate clean matplotlib/plotly/Vega-Lite code. Covers color and accessibility, axis/scale honesty, labeling, and the common chart mistakes (truncated axes, pie overload, dual axes, chartjunk). Use when the user wants to visualize data, asks which chart to use, how to plot something, or to improve/critique an existing chart."
metadata: {"flowly":{"emoji":"📊","tags":["data","visualization","charts","matplotlib","plotly","dataviz","plotting"],"requires":{"bins":["python3"]},"category":"data","related_skills":["sql-query","ab-testing","statistical-analysis","excel-author"]}}
---

# Data Visualization — The Right Chart, Honestly Drawn

A chart's job is to make a point *truthfully and instantly*. Two decisions determine success: **which chart type** (driven by the data shape and the message you want to land) and **how it's encoded** (so the eye reads the data accurately without being misled). Most bad charts are the wrong type, a dishonest axis, or decoration drowning the data.

## What this skill produces

**Chat-first.** Default: the chart recommendation with *why*, plus ready-to-run plotting code (matplotlib/plotly/Vega-Lite) the user executes locally. For a critique, the specific fixes. State which library and that plotting needs it installed (`pip install matplotlib`/`plotly`).

## When to use

- "How should I visualize this?" / "What chart for \<data\>?"
- "Plot / graph / chart this." / "Make a \<bar/line/scatter\>."
- "Improve / critique this chart." / "Why does this look misleading?"
- "Show \<trend / comparison / distribution / relationship / composition\>."

## Step 1 — Chart type from message + data shape

Pick by what you're trying to **show**, then by the data types involved:

| You want to show… | Use | Notes |
|---|---|---|
| **Comparison** across categories | **bar** (horizontal if many/long labels) | start the value axis at 0; sort by value unless order is meaningful |
| **Trend over time** | **line** (area for cumulative) | time on x; don't connect unordered categories with lines |
| **Relationship** between two numerics | **scatter** (+ trend line) | add size/color for a 3rd/4th dim sparingly |
| **Distribution** of one numeric | **histogram** / **box** / **violin** | histogram for shape; box/violin to compare groups |
| **Composition** (parts of a whole) | **stacked bar** / **treemap** | avoid pie for >~3 slices; bars compare better |
| **Part-to-whole over time** | **stacked area** / **100% stacked bar** | watch readability of middle bands |
| **Correlation matrix / 2D density** | **heatmap** | sequential/diverging colormap |
| **Ranking / change between two states** | **slope chart / dumbbell** | clearer than grouped bars for before/after |
| **Geospatial** | **choropleth / point map** | normalize by area/population, not raw counts |

Rules of thumb: **bars for comparison, lines for time, scatter for relationships, histograms/box for distributions.** Match the chart to the question, not to what looks fancy.

## Step 2 — Encode honestly (so the eye reads it right)

- **Position is the most accurate visual channel**, then length, then angle/area/color. Encode the most important value as position/length; reserve color for categories or a secondary dimension.
- **Bar charts MUST start at zero** — a truncated bar axis exaggerates differences and is the classic lie. Line charts *may* use a non-zero y-axis (to show variation) but label it clearly.
- **One axis, usually.** Dual y-axes invite spurious "correlation" by arbitrary scaling — avoid; use two panels or normalize instead.
- **Linear vs log:** log scales for data spanning orders of magnitude or multiplicative/growth processes — but label them (readers assume linear).
- **Sort meaningfully** (by value for nominal categories; keep natural order for ordinal/time).
- **Don't overload:** one clear message per chart; small multiples (faceting) beat one cluttered chart with 12 series.

## Step 3 — Color & accessibility

- **Categorical** palette for distinct groups; **sequential** for ordered magnitude; **diverging** for a meaningful midpoint (e.g. above/below zero). Don't use a categorical palette for ordered data.
- **Colorblind-safe** palettes (e.g. viridis, ColorBrewer, Okabe-Ito) — ~8% of men have CVD. Don't rely on red/green alone; add labels/patterns/position as redundant cues.
- Limit to ~6–8 distinguishable colors; beyond that, direct-label or facet.
- Ensure sufficient contrast; avoid rainbow/jet colormaps (perceptually non-uniform and misleading).

## Step 4 — Clarity (reduce ink, add meaning)

- **Label directly** where possible (labels on lines beat a legend the eye must ping-pong to).
- **Title states the takeaway** ("Sales doubled in Q3"), not just the variable ("Sales").
- Axis titles **with units**; sensible tick formatting (%, $, thousands).
- **Remove chartjunk:** heavy gridlines, 3D effects, drop shadows, redundant borders. Maximize the data-ink ratio.
- Annotate the key point (an arrow/callout on the moment that matters).

## Code generation

Pick the library to fit the target:
- **matplotlib** — static, publication, full control; the default for scripts/reports.
- **plotly** — interactive (hover, zoom), dashboards, HTML.
- **Vega-Lite / Altair** — declarative, concise, great defaults.
- (For spreadsheets, → `excel-author`.)

Plotting libraries aren't stdlib — deliver the code and the `pip install` line; the user runs it. Always include: axis labels with units, a takeaway title, a colorblind-safe palette, and zero-based bar axes.

```python
import matplotlib.pyplot as plt
cats = ["A", "B", "C", "D"]
vals = [23, 45, 12, 38]
order = sorted(range(len(vals)), key=lambda i: vals[i])     # sort by value
cats, vals = [cats[i] for i in order], [vals[i] for i in order]
fig, ax = plt.subplots(figsize=(7, 4))
ax.barh(cats, vals, color="#4C78A8")                         # horizontal: long labels
ax.set_xlim(0, max(vals) * 1.1)                              # bars start at 0
ax.set_xlabel("Revenue ($k)")
ax.set_title("Product B leads revenue, 3.7× Product C")      # takeaway title
ax.spines[["top", "right"]].set_visible(False)               # de-junk
plt.tight_layout(); plt.savefig("chart.png", dpi=150)
```

## Chat output format

````
**Best chart: grouped horizontal bar** (comparing 4 products across 2 regions)

Why: categorical comparison → bars; horizontal because labels are long;
grouped (not stacked) since you're comparing, not showing composition.

```python
import matplotlib.pyplot as plt
... (runnable code) ...
```
Run: `pip install matplotlib && python chart.py`. Bars start at 0, viridis-safe
colors, takeaway title. Avoid a pie here — 8 slices are unreadable.
````

## Workflow

1. **Clarify the message** (comparison / trend / distribution / relationship / composition) and the **data shape** (categorical vs numeric vs time, how many series).
2. **Choose the chart type** from the table; reject pie-for-many, dual-axis, etc.
3. **Plan honest encoding:** zero-based bars, single axis, right scale, sorted, colorblind-safe.
4. **Generate clean code** (matplotlib/plotly/Vega-Lite) with labels+units, takeaway title, de-junked.
5. **Deliver** chart + code + the `pip install`/run line + the rationale; for a critique, list the specific fixes.
6. Route the data pull to `sql-query`, stats/CIs to `ab-testing`/`statistical-analysis`, spreadsheet charts to `excel-author`.

## Key pitfalls

- **Truncated bar axis.** Bars not starting at zero exaggerate differences — the most common dishonest chart.
- **Pie chart overload.** Humans compare angles poorly; for >3 slices use a sorted bar.
- **Dual y-axes.** Arbitrary scaling manufactures fake correlation — use two panels or normalize.
- **Wrong type for the message.** Lines for unordered categories, bars for distributions, etc. — match type to question.
- **Rainbow/jet colormap & non-colorblind-safe colors.** Perceptually misleading and exclusionary — use viridis/ColorBrewer/Okabe-Ito.
- **Chartjunk.** 3D, shadows, heavy gridlines bury the data — maximize data-ink.
- **Legend the eye must hunt.** Direct-label series where possible.
- **Unlabeled scale tricks.** Log axes or non-zero baselines without clear labels mislead.
- **One chart, twelve series.** Use small multiples instead of a spaghetti plot.

## Quick reference

- Comparison→bar (zero-based) · time→line · relationship→scatter · distribution→histogram/box · composition→stacked bar/treemap (not pie for many).
- Encoding accuracy: position > length > angle/area > color. Bars must start at 0.
- One y-axis; log only when labeled; sort categories by value.
- Colorblind-safe (viridis/Okabe-Ito); categorical vs sequential vs diverging by data type; ≤~8 colors.
- Takeaway title, axis units, direct labels, de-junk. Small multiples over clutter.
- Libraries: matplotlib (static), plotly (interactive), Vega-Lite/Altair (declarative); Excel → excel-author.
