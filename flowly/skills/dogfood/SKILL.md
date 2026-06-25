---
name: dogfood
description: "Exploratory QA of web apps: find bugs, evidence, reports."
version: 1.0.0
platforms: [linux, macos, windows]
metadata: {"flowly":{"emoji":"🐾","tags":["qa","testing","browser","web","dogfood"],"requires_tools":["browser_tab"],"category":"qa","related_skills":["flowly-browser"]}}
---

# Dogfood — adversarial QA sweeps of web apps

Drive a real browser through a web app like a skeptical user trying to
break it, then hand back a report a developer can act on without asking
follow-up questions. Every claim in that report is backed by a
screenshot, a console line, or an exact reproduction path — never a
vibe.

Read the `flowly-browser` skill first; it owns the current rules for
`browser_tab` and ref IDs. This skill assumes you already know how to
drive a tab.

## What you need before starting

- A start URL.
- A scope: a named area ("the checkout flow"), or "everything reachable
  from the home page" for a full sweep.
- A place to drop artifacts — default `./dogfood-output/` with a
  `screenshots/` subfolder and a `report.md`. Create it up front.

If the user gave you a URL but no scope, default to a full sweep and say
so in the report's coverage section.

## The mental model: a coverage map and an inspection loop

Don't test linearly. Hold two things in your head:

1. **A coverage map** — the set of surfaces worth visiting. Seed it from
   the start page, then grow it as navigation reveals more:
   - the landing page and primary nav (header / footer / sidebar)
   - the headline user journeys (sign-up, login, search, create, pay)
   - every form and its failure modes
   - the unglamorous edges: empty states, 404s, expired links, the back
     button mid-flow, double-submits

2. **An inspection loop** you run on each surface. One pass per surface,
   same four moves every time:

   **Load → Observe → Provoke → Record.**

### Load

```
browser_tab(action="navigate", url="https://example.com/checkout")
browser_tab(action="console_log", clear=true)
```

Clear the console *as you land* so anything that shows up afterward is
attributable to this surface, not leftover noise.

### Observe

Pull the structural view and a visual one — they catch different bugs:

```
browser_tab(action="read_page")     # text + interactive ref IDs (ref_3, …)
browser_tab(action="screenshot")    # what the human actually sees
```

`read_page` tells you what to click; the screenshot tells you whether it
*looks* right. Disagreements between the two (a button present in the DOM
but invisible on screen) are themselves findings.

### Provoke

Now act like a user with bad intentions. Reference elements by their
`read_page` ref IDs:

```
browser_tab(action="click",  ref="ref_7")
browser_tab(action="type",   ref="ref_4", text="not-an-email")
browser_tab(action="key",    key="Tab")
browser_tab(action="key",    key="Enter")
browser_tab(action="scroll", direction="down")
```

Cover, at minimum, per surface:
- happy path — does the obvious thing work at all?
- bad input — invalid emails, empty required fields, huge strings,
  emoji and quotes, negative numbers where positives are expected
- empty submit — submit with nothing filled in
- keyboard only — can you reach and trigger controls with Tab/Enter?
- impatience — double-click submit, hit back mid-flow, reload after a
  partial action

### Record

After *every* provocation, re-check both channels:

```
browser_tab(action="console_log")   # new errors since you cleared it
browser_tab(action="screenshot")    # visible result of the action
```

A click that prints an uncaught exception but looks fine on screen is a
high-value find precisely because a human tester would miss it. Note
expected-vs-actual the moment they diverge — don't trust memory.

Then move to the next surface and run the loop again.

## Turning an observation into a finding

A finding only counts when someone else could reproduce it. For each
one, capture:

- a screenshot — save the `screenshot_path` the tool returns
- the exact URL it happened on
- the minimal click/type sequence that triggers it
- what you expected vs. what happened
- any console output, verbatim

Then grade it. Severity (how badly it hurts) and category (what kind of
problem it is) are independent axes — see
`references/issue-taxonomy.md` for the rubric and worked examples. A
typo is Low/Content; a checkout that charges twice is Critical/Functional.

## Before you write the report

- **Deduplicate.** The same root bug often surfaces on five pages.
  Collapse those into one finding and list the affected URLs.
- **Re-grade with the full picture.** A bug you logged as Medium may be
  Critical once you realize it blocks the only path to a feature.
- **Sort** Critical → High → Medium → Low.
- **Tally** counts per severity for the summary.

## The report

Fill in `templates/dogfood-report-template.md` and save it to
`{output_dir}/report.md`. It must carry:

- an executive summary: totals, the severity breakdown, and the scope
  you actually covered (not the scope you intended)
- one section per finding with everything from the capture list above;
  embed evidence inline with `MEDIA:<screenshot_path>`
- a flat summary table of all findings for skimming
- a coverage section that is honest about what you did **not** reach and
  why (auth wall, paywall, missing test data, a blocker bug)

Under-claiming coverage is fine. Implying you tested something you
couldn't reach is not.

## browser_tab quick reference

| Call | Does |
|------|------|
| `browser_tab(action="navigate", url=…)` | open a URL |
| `browser_tab(action="read_page")` | DOM text + interactive ref IDs |
| `browser_tab(action="screenshot")` | capture the rendered page |
| `browser_tab(action="console_log", clear=true)` | read JS console; `clear` resets it |
| `browser_tab(action="click", ref=…)` | click a ref / selector / text |
| `browser_tab(action="type", ref=…, text=…)` | type into a field |
| `browser_tab(action="key", key=…)` | press a key (`Tab`, `Enter`, `Alt+Left` to go back) |
| `browser_tab(action="scroll", direction=…)` | scroll the page |

## Field notes

- Console-clear-on-land is the single highest-leverage habit here.
  Silent JS errors are the bugs humans never report.
- The fold hides bugs. Scroll every long page to its end.
- Forms are where apps break — always try valid *and* hostile input.
- Test journeys end-to-end, not pages in isolation; bugs live in the
  hand-offs between steps.
- Special characters, very long strings, and rapid repeated clicks
  surface a surprising share of crashes.
- When you show the user evidence in chat, prefix it `MEDIA:<path>` so
  the screenshot renders inline.
