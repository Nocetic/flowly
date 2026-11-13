---
name: earnings-analysis
description: "Analyze a company's earnings — press release, earnings call transcript, and 10-Q/10-K together. Build the beat/miss table vs consensus, track guidance changes, KPI trends, the margin bridge, and mine the analyst Q&A for what management is dodging. Use when the user asks 'how was the quarter', wants an earnings recap, guidance read, call summary, or post-earnings thesis check."
metadata: {"flowly":{"emoji":"📈","tags":["finance","earnings","quarterly-results","guidance","transcript","kpi","beat-miss","fundamental-analysis"],"requires":{"bins":["python3","curl"]},"category":"finance","related_skills":["sec-filings","comps-analysis","credit-analysis","finance"]}}
---

# Earnings Analysis — Read the Quarter Like a Buy-Side Analyst

A quarter is three documents telling one story, sometimes inconsistently: the **press release** (the spin), the **call transcript** (the nuance and the dodges), and the **10-Q/10-K** (the truth in the footnotes). This skill reconciles all three into a verdict.

## What this skill produces

**Chat-first.** Default output is a one-screen recap: headline beat/miss, the two or three numbers that moved the stock, what changed in guidance, and the single most important thing management said (or avoided) on the call. Offer a full file only for a deep model-update or a tracked KPI history.

The job is judgment, not transcription. Anyone can paste the press release. You tell the user **whether the quarter was actually good**, why the stock moved the way it did, and what to watch next.

## When to use

- "How was \<TICKER\>'s quarter?" / "Recap the earnings."
- "Did they beat?" / "Beat or miss on revenue and EPS?"
- "What did they say about guidance / next quarter / the full year?"
- "Summarize the earnings call." / "What did analysts push back on?"
- "Why is the stock down 8% after a beat?"
- "Update my thesis after this print."

## The three sources (and what each gives you)

| Source | Where | What it's best for |
|---|---|---|
| **Press release** (8-K Ex-99.1) | EDGAR `8-K`, or IR site | Headline numbers, the metrics *they* chose, initial guide |
| **Earnings call transcript** | IR webcast, transcript sites | Tone, color on drivers, the analyst Q&A, the dodges |
| **10-Q / 10-K** | EDGAR | Audited-grade detail, segment data, footnotes, true cash flow |
| **Consensus estimates** | Provided by user / web | The bar that defines beat vs miss |

Use the `sec-filings` skill's `edgar.py` to pull the 8-K press release and the 10-Q:
```bash
python3 ../sec-filings/scripts/edgar.py latest <TICKER> --type 8-K
python3 ../sec-filings/scripts/edgar.py latest <TICKER> --type 10-Q
python3 ../sec-filings/scripts/edgar.py facts <TICKER> --tag <XBRL-tag>   # multi-quarter KPI history
```

## Data sourcing — and the consensus problem

- **Numbers come from the release / filing.** Never invent reported figures.
- **Consensus is the hard part.** Beat/miss is meaningless without the estimate. If the user didn't give you consensus, **ask for it or pull it from a cited source** — do not guess the Street number. State the consensus source and date next to every comparison.
- **Distinguish GAAP from "adjusted."** Companies headline the flattering one. Report both; flag if the gap is widening.
- **Mind the calendar.** "Q3 FY2026" ≠ calendar Q3 for many firms. Lead with the fiscal period and the report date.

## The analysis, in order

### 1. Beat / miss table (the headline)
Revenue and EPS vs consensus, plus the YoY growth. Keep it to the metrics that matter.

| Metric | Actual | Consensus | Surprise | YoY |
|---|---|---|---|---|
| Revenue | \$12.4B | \$12.1B | +2.5% ✅ | +8% |
| EPS (adj) | \$1.32 | \$1.25 | +5.6% ✅ | +12% |
| EPS (GAAP) | \$1.05 | — | — | +4% |

A beat on adjusted EPS driven by a lower tax rate or buybacks is *not* the same as an operating beat — decompose it.

### 2. Guidance — the real stock mover
The print is backward-looking; **guidance moves the stock.** For each guided metric, compare new guide vs prior guide vs Street:
- Raised / reaffirmed / cut / introduced / **withdrawn** (withdrawn guidance is a red flag).
- Note whether a "raise" merely passes through the beat or actually lifts the back half.
- Watch the shape: front-loaded vs back-half-weighted (a back-half-heavy guide is a hope).

### 3. KPI & operating trends
Pull the company's own operating metrics and trend them across quarters (sequential matters as much as YoY):
- SaaS: ARR/NRR, net adds, churn, RPO, billings, magic number.
- Consumer/retail: same-store sales, traffic vs ticket, units, AOV.
- Semis/hardware: bookings/billings, backlog, ASPs, utilization.
- Banks: NIM, NCOs, deposit beta, loan growth.
Flag any metric they **stopped disclosing** — companies bury KPIs when they turn ugly.

### 4. Margin bridge
Explain the margin move (gross and operating) in components: price, volume, mix, input costs, FX, opex leverage, one-timers. "Gross margin −150bps on unfavorable mix and higher freight, partly offset by price" — not just "margins fell."

### 5. Cash & balance sheet
FCF vs net income (is the earning real cash?), buybacks/dividends, debt changes, dilution from SBC. A quarter of "record EPS" with negative FCF deserves a callout.

### 6. The call — read the Q&A, not just the prepared remarks
The prepared remarks are scripted. The **analyst Q&A is where information leaks**:
- Which topics did analysts hammer? Repeated questions on one theme = the market's real worry.
- **Dodges:** "we don't break that out," "we'll talk about that next quarter," pivoting to a different metric. Note what was asked and *not answered*.
- Tone shift: hedging language, "challenging environment," "prudent/conservative" guidance, "pockets of weakness."
- New disclosures or quiet walk-backs of prior claims.

## Chat output format

```
**NVDA FQ1'26** (reported 2026-05-28) — Beat & raise, stock +6% AH

Rev $X.XB (+Y% YoY) vs $X.XB est ✅  ·  Adj EPS $X.XX vs $X.XX ✅
Guide: next-Q rev $X.XB, ABOVE Street $X.XB ⬆️ (real raise, not just pass-through)

📊 Drivers: Data Center +Z% QoQ; gross margin XX% (+/−bps on <mix/cost>)
💵 FCF $X.XB (clean vs net income $X.XB); $XB buyback
🎙️ Call: analysts pressed 3x on <topic>; mgmt dodged <X>. Tone: confident.
🔭 Watch: <the one metric/risk for next Q>
```

Chat tables ≤4 columns, abbreviate, ✅/❌/⬆️/⬇️ for fast scanning. More than ~3 tables → build a file, summarize inline.

## Workflow

1. **Identify the print.** Ticker + fiscal period. Confirm which quarter if ambiguous.
2. **Get consensus** from the user or a cited source — this gates the whole analysis.
3. **Pull the three sources.** 8-K press release + 10-Q via `edgar.py`; transcript from IR/web (the user may paste it).
4. **Build the beat/miss table**, then guidance, then KPIs/margins/cash.
5. **Read the Q&A** for dodges and tone.
6. **Render the verdict:** Was it good? Why did the stock move? What's the one thing to watch?
7. **Hand off** as needed: `sec-filings` for footnote forensics, `comps-analysis` to reprice vs peers, `credit-analysis` if leverage is the story.

## Key pitfalls

- **Beat/miss without consensus.** A number alone is not a result. No estimate → no verdict.
- **Headlining adjusted EPS** without checking what's adjusted out (recurring "one-time" charges, stock comp, tax).
- **Ignoring guidance.** The quarter is history; the guide is the trade.
- **Reading only the prepared remarks.** The Q&A is where the quarter is really judged.
- **Confusing a good print with a good stock reaction.** Explain the gap (expectations, guide, positioning) rather than assuming the market is wrong.
- **Single-quarter tunnel vision.** Frame KPIs against the trailing 4–8 quarters; one quarter is noise.
- **Fiscal-calendar mix-ups.** State the fiscal period explicitly.

## Quick reference

- Surprise % = (Actual − Consensus) / |Consensus|
- "Quality of beat": operating beat (rev + margin) > tax/buyback beat > one-timer beat.
- Guidance raise that ≈ the size of the beat = pass-through (not incremental); a raise *larger* than the beat lifts the outlook.
- Sequential (QoQ) trend catches inflections earlier than YoY; report both for fast-moving businesses.
