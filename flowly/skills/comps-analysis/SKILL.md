---
name: comps-analysis
description: "Build a comparable-company analysis (trading comps) — select the right peer set, pull and normalize multiples (EV/EBITDA, EV/Revenue, EV/EBIT, P/E, P/B, PEG, FCF yield), handle outliers, and derive an implied valuation range for a target. Use when the user asks 'how does X trade vs peers', wants a comps table, a relative-value read, peer multiples, or a sanity-check on a DCF."
metadata: {"flowly":{"emoji":"⚖️","tags":["finance","valuation","comps","trading-multiples","ev-ebitda","peer-analysis","relative-value"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["dcf-model","earnings-analysis","sec-filings","excel-author","finance"]}}
---

# Comparable Company Analysis — Relative Valuation Done Right

A comps spread answers "what is this company worth *relative to* how the market prices businesses like it." It's the market's verdict, where a DCF is your own. The whole exercise lives or dies on two things: **picking a defensible peer set** and **comparing apples to apples**. Everything else is arithmetic.

## What this skill produces

**Chat-first.** Default: a clean comps table (peers × a few key multiples) plus the implied valuation range for the target and a one-line read ("trades at a discount to peers on EV/EBITDA, justified by lower growth"). Offer a formatted `.xlsx` via `excel-author` when the user wants a model-ready, sourced spread.

You are building a *defensible* comparison, not a spreadsheet of every vaguely-similar ticker. Every peer earns its place; every adjustment is explained.

## When to use

- "How does \<TICKER\> trade vs peers?" / "Build me a comps table."
- "Is \<TICKER\> cheap or expensive relative to the group?"
- "What's the implied value of \<private/target co\> using public comps?"
- "Sanity-check this DCF against where peers trade."
- "What EV/EBITDA multiple does the sector trade at?"

## Step 1 — Peer selection (this is 80% of the work)

A comp is only valid if the businesses are genuinely comparable. Select on, in priority order:

1. **Business model & end market** — same way of making money, same customers. (A payments network ≠ a bank, even both "fintech.")
2. **Growth profile** — pair high-growth with high-growth; a 30%-grower and a 3%-grower don't share a multiple.
3. **Margin structure & profitability** — software vs hardware margins are different planets.
4. **Size** — within a rough order of magnitude (mega-cap multiples ≠ small-cap).
5. **Geography & regulatory regime** — where revenue and rules live.
6. **Capital intensity** — asset-light vs asset-heavy.

Rules:
- **5–10 peers** is the sweet spot. Fewer than 4 isn't a set; more than ~12 dilutes signal.
- **State the inclusion logic** and explicitly **note who you excluded and why** ("excluded MegaCorp — different end market; excluded TinyCo — pre-revenue").
- Flag any peer that's a stretch rather than silently padding the list.

## Step 2 — The multiples (and which to use when)

| Multiple | Numerator/Denominator | Best for | Watch out |
|---|---|---|---|
| **EV/Revenue** | EV / sales | Unprofitable, high-growth | Ignores all profitability |
| **EV/EBITDA** | EV / EBITDA | Capital-structure-neutral workhorse | Ignores capex & SBC; abused as "cash" |
| **EV/EBIT** | EV / EBIT | Capital-intensive (respects D&A) | — |
| **EV/FCF** | EV / unlevered FCF | Cash quality matters | Lumpy capex distorts |
| **P/E** | Price / EPS | Mature, profitable, stable cap structure | Distorted by leverage & one-timers |
| **P/B** | Price / book equity | Banks, insurers, asset-heavy | Useless for asset-light |
| **PEG** | P/E ÷ growth | Comparing across growth rates | Crude; sensitive to the growth input |
| **FCF yield** | FCF / mkt cap | Income/value lens | Inverse of EV/FCF-ish |

- **EV-based multiples are capital-structure-neutral** — prefer them when peers have different leverage. EV = market cap + total debt + preferred + minority interest − cash & equivalents.
- Use **NTM (forward)** multiples where you have consensus; valuation is forward-looking. Be explicit: LTM vs NTM, and don't mix them in one column.
- Pick 2–4 multiples that fit the sector — don't show all eight. Banks → P/E + P/B; software → EV/Rev + EV/EBITDA (+ Rule of 40 context); industrials → EV/EBITDA + EV/EBIT + P/E.

## Step 3 — Normalize (apples to apples)

The point of comps is comparability, so adjust before you compare:
- **Calendarize** to a common period if fiscal years differ (or be explicit that you didn't).
- **Strip one-timers** from EBITDA/EPS (restructuring, litigation, impairments) — but be consistent across all peers, and don't let "adjusted" become "imaginary."
- **Treat SBC consistently** — either burden everyone or no one; stock comp is a real cost.
- **EV components consistently** — same treatment of leases (operating-lease liabilities), minority interest, and convertible/dilutive securities across the set.
- **Same data vintage** — all prices/multiples as of one date; state it.

## Step 4 — Handle outliers (don't let one ticker run the table)

- Report **median AND mean**; lead with the **median** (robust to outliers).
- Add the **range and quartiles** (25th/75th) so the spread is visible.
- A multiple that's 2–3x the group is usually a different business or a distortion (M&A target, near-zero denominator, distressed). **Investigate, then trim or footnote it** — don't silently average it in.
- A negative EBITDA/EPS makes that multiple "n.m." (not meaningful) — mark it, don't compute a nonsense ratio.

## Step 5 — Derive the implied range for the target

1. Take the peer-group **median** (and 25th–75th) for each chosen multiple.
2. Apply to the target's corresponding metric (its EBITDA, revenue, EPS — LTM or NTM, matched).
3. For EV-based multiples, **bridge EV → equity → per share**: equity value = EV − net debt − preferred − minority interest; ÷ diluted shares.
4. Present a **range**, not a false-precision point estimate. Triangulate across multiples (EV/EBITDA range vs P/E range) and reconcile.
5. Position the target *within* the range with a reason: a discount/premium should map to a fundamental (growth, margins, risk), not be hand-waved.

## Data sourcing

- Pull financials from filings (`sec-filings` skill / `edgar.py`) for the LTM denominators.
- Market caps, share prices, and forward estimates: from the user or a cited live source — **never invent prices or consensus**.
- Stamp the **as-of date** on the whole table; comps are a snapshot and go stale fast.

## Chat output format

```
**Comps — Target: ACME** (prices as of 2026-06-04, NTM multiples)

| Peer | EV/EBITDA | EV/Rev | P/E | Rev growth |
|------|-----------|--------|-----|-----------|
| BetaCo | 14.2x | 4.1x | 22x | 11% |
| GammaCo | 11.8x | 3.4x | 18x | 8% |
| ... |
| **Median** | **12.5x** | **3.6x** | **19x** | **9%** |

Implied ACME EV: $X–$Y B → equity $A–$B → **$P–$Q / share**
Read: ACME trades ~15% below peer median EV/EBITDA, broadly justified by
its lower growth (6% vs 9%) and thinner margins. Not obviously cheap.
```

Keep to ≤5 columns for mobile; if the peer set is large, send the table as an `.xlsx` and summarize the median/range + implied value inline.

## Workflow

1. **Define the target and the question** (relative-value read vs implied valuation vs DCF cross-check).
2. **Build the peer set** — state inclusion/exclusion logic, get user sign-off if the set is contentious.
3. **Choose 2–4 multiples** that fit the sector; decide LTM vs NTM.
4. **Pull and normalize** the data; stamp the as-of date.
5. **Compute multiples**, mark n.m./outliers, report median/mean/quartiles.
6. **Derive the implied range** and bridge to per share.
7. **Deliver** the chat table + read; offer the `.xlsx`; cross-reference `dcf-model` for an intrinsic anchor.

## Key pitfalls

- **Garbage peer set.** The single biggest error. "Same industry" is not "comparable business."
- **Mixing LTM and NTM** in one column, or different price dates across peers.
- **Mean over median** — one outlier silently inflates the whole group.
- **Inconsistent normalization** — adjusting one peer's EBITDA but not the others.
- **EV mismatches** — forgetting to subtract cash, or double-counting leases/minorities unevenly.
- **False precision** — quoting an implied price to the cent off a median multiple. Give a range.
- **Multiples without context** — "trades at 30x" is meaningless without the growth/margin that justifies it. A comp is a *relative* statement, always.
- **Computing multiples on negative denominators** — mark n.m.

## Quick reference

- EV = Market cap + Total debt + Preferred + Minority interest − Cash & equivalents
- Equity value (from EV) = EV − Net debt − Preferred − Minority interest
- Implied price = Equity value ÷ Diluted shares outstanding
- Lead with **median**; show **25th–75th** for spread; mark **n.m.** on negative denominators.
- Forward (NTM) > trailing (LTM) when you have reliable consensus.
