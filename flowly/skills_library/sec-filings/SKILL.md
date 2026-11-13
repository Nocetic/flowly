---
name: sec-filings
description: "Read and dissect SEC filings — 10-K, 10-Q, S-1, 8-K, proxy (DEF 14A), 13F. Pull and analyze MD&A, risk factors, segment data, debt schedules, share count, related-party transactions, and surface red flags. Pulls directly from EDGAR (no auth). Use when the user asks what a filing says, wants a 10-K/10-Q digest, IPO S-1 breakdown, 8-K event read, or a forensic look at a company's disclosures."
metadata: {"flowly":{"emoji":"📄","tags":["finance","sec","edgar","10-k","10-q","s-1","8-k","filings","fundamental-analysis","forensic"],"requires":{"bins":["python3","curl"]},"category":"finance","related_skills":["earnings-analysis","credit-analysis","comps-analysis","finance","ocr-and-documents"]}}
---

# SEC Filings — Read, Dissect, and Stress-Test Disclosures

Turn a dense SEC filing into the handful of facts that actually move a thesis: what the business does, how it makes money, what could break it, how much debt sits on it, who's getting paid, and where the accounting gets interesting.

## What this skill produces

**Chat-first.** The default deliverable is a tight markdown digest the user can read on a phone — a few narrow tables and bullet callouts, not a wall of text. Offer a full file (`.md`, `.xlsx`, or `.pdf`) only when the user wants the long form or a model-ready data dump.

You are an analyst reading on the user's behalf, not a search box. Read the relevant sections, then **tell them what matters and why**, with a section/page citation for every non-obvious claim.

## When to use

- "What does Acme's latest 10-K say about X?" / "Summarize this 10-Q."
- "Break down this S-1 / IPO prospectus."
- "What was in that 8-K?" / "Did they file anything material this week?"
- "How much debt does NVDA have and when does it mature?"
- "Any red flags in their accounting?" / "Walk me through their risk factors."
- "Who are the related parties?" / "What did insiders get paid?" (proxy / DEF 14A)
- "What does this 13F holder own?"

## The filing types (and what each is for)

| Form | What it is | Read it for |
|---|---|---|
| **10-K** | Annual report | Full business model, audited financials, full risk factors, MD&A, segments |
| **10-Q** | Quarterly report | Latest numbers, sequential trends, updated risks, unaudited |
| **8-K** | Material event | M&A, exec changes, guidance, debt deals, restatements, bankruptcies |
| **S-1 / 424B** | IPO / offering | New issuer: business, cap table, use of proceeds, dilution, lockups |
| **DEF 14A** | Proxy statement | Exec comp, board, related-party deals, shareholder proposals |
| **13F-HR** | Institutional holdings | What a >$100M manager owns (45 days lagged, long-only, no shorts/cash) |
| **13D / 13G** | >5% ownership | Activist (13D) vs passive (13G) large stakes |
| **Form 4** | Insider trades | Buys/sells by officers, directors, 10% holders |

## Data sourcing — pull from EDGAR, never invent

All US filings are free on EDGAR with **zero authentication**. Use `scripts/edgar.py` (stdlib only, sets the required User-Agent header).

```bash
python3 scripts/edgar.py cik AAPL                      # ticker → CIK
python3 scripts/edgar.py filings AAPL --type 10-K -n 3 # recent filings of a type
python3 scripts/edgar.py latest AAPL --type 10-Q       # newest filing, prints doc URLs
python3 scripts/edgar.py facts AAPL --tag Revenues     # XBRL company-fact time series
python3 scripts/edgar.py search "going concern" --type 10-K  # full-text search
python3 scripts/edgar.py get <document-url>            # fetch a filing document as text
```

Sourcing rules:

1. **Prefer the primary document.** Quote the filing itself, not a news summary of it. If the user pastes a PDF/HTML, read that; otherwise pull from EDGAR.
2. **For scanned or image PDFs**, hand off to the `ocr-and-documents` skill to extract text first.
3. **Every number gets a location.** "Long-term debt of \$9.7B (Balance Sheet, p.52)" — not a bare figure.
4. **Never fabricate.** If a figure isn't in the filing, say "not disclosed" and, if useful, note where it *would* normally appear.
5. **Date everything.** Filings go stale; lead with the period covered and the filing date.

## How to read each major section

### Business (Item 1) — the model in three sentences
What do they sell, to whom, and how do they get paid (one-time vs recurring, who the real customer is)? Note concentration ("X% of revenue from one customer"), the moat they *claim*, and how revenue is actually recognized.

### Risk Factors (Item 1A) — signal vs boilerplate
90% is legal CYA. Your job is to find the 10% that's specific and new.
- **Flag the specific.** "We depend on a single fab in Taiwan" beats "macroeconomic conditions may affect us."
- **Diff against the prior year.** A *newly added* risk factor is a tell — pull last year's 10-K and compare. New language around liquidity, covenants, litigation, or a key customer is the highest-signal change in the whole document.
- **Watch escalation.** A risk moving from generic to detailed, or gaining a dollar figure, means it got real.

### MD&A (Item 7) — management's own story
Reconcile what they *say* against what the numbers *show*.
- Find the revenue/margin bridge: price vs volume vs mix vs FX.
- Separate organic growth from acquisitions.
- Note non-GAAP adjustments and whether they're growing (recurring "one-time" charges are a flag).
- Pull liquidity & capital-resources language: cash runway, credit facility headroom, covenant mentions.

### Financial statements & notes — where the truth lives
The notes are the filing. Prioritize:
- **Debt note:** tranches, rates (fixed/floating), **maturity schedule**, covenants, secured vs unsecured.
- **Revenue recognition:** timing, deferred revenue/RPO trend, channel-stuffing risk.
- **Segments:** revenue + operating income by segment; which segment actually earns the money.
- **Commitments & contingencies:** litigation, leases, purchase obligations, off-balance-sheet.
- **Share count:** basic vs diluted, options/RSUs outstanding, buyback authorization, dilution trajectory.
- **Related-party transactions:** money flowing to insiders or affiliated entities.

## Red-flag checklist (forensic pass)

Run this when the user asks for red flags, due diligence, or a short thesis. Each hit is a *question to investigate*, not a verdict.

- **Going-concern** language or substantial-doubt disclosure.
- **Auditor change** or a **material weakness** in internal controls (Item 9A).
- **Restatement** or non-reliance 8-K (Item 4.02).
- **Receivables/inventory growing faster than revenue** (DSO/DIO creeping up).
- **Cash flow from operations diverging from net income** over multiple periods.
- **Non-GAAP > GAAP** by a widening margin; "adjusted" everything.
- **Rising related-party** activity or unusual affiliate transactions.
- **Debt maturity wall** inside 12–24 months with thin liquidity.
- **Frequent 8-K exec/CFO departures**, especially the CFO or audit-committee chair.
- **Late filing** (NT 10-K / NT 10-Q) — the company couldn't close its books on time.
- **Customer/supplier concentration** rising.
- **Capitalizing costs** that peers expense (watch software-dev and content cap policies).

`edgar.py search "going concern"` / `"material weakness"` / `"restatement"` is a fast first sweep across a company's filings.

## Chat output format

Keep it scannable on mobile. A typical 10-K digest:

```
**ACME 10-K — FY2025** (filed 2026-02-18, period ended 2025-12-31)

🏢 Business: <one-line model>. Segments: A 62% / B 38% of rev.
📈 Revenue $12.4B (+8% YoY) · Op margin 21% (−180bps) · Net $1.9B · Dil. EPS $4.12
💰 Net debt $3.1B · 2.0x EBITDA · nearest maturity $1.2B due 2027

**What changed vs last year**
- New risk factor: single-source supplier dependence (Item 1A, p.19)
- Non-GAAP adjustments up 40% YoY (mostly "restructuring", 3rd straight year)

🚩 Flags: DSO 52→61 days; CFO departed (8-K 2025-11)
✅ Clean: no going-concern, no control weaknesses, auditor unchanged
```

Rules for chat tables: ≤4 columns, abbreviate (\$B/\$M, bps, YoY/QoQ), one decimal. If the answer needs more than ~3 tables, build a file and send the highlights inline.

## Workflow

1. **Resolve the company and filing.** Ticker → CIK via `edgar.py cik`. Confirm which filing/period if ambiguous.
2. **Fetch the primary doc** (`latest` / `get`). For pasted PDFs, OCR if needed.
3. **Targeted read.** Go straight to the sections the question needs — don't read 200 pages to answer one question.
4. **Cross-check** numbers against XBRL facts (`edgar.py facts`) and, for trends, the prior period's filing.
5. **Run the red-flag sweep** if the ask is diligence/thesis-flavored.
6. **Deliver** the chat digest with citations; offer the full file or a handoff (`earnings-analysis` for the call, `credit-analysis` for the debt deep-dive, `comps-analysis` for peer context).

## Key pitfalls

- **Summarizing the summary.** Don't paraphrase a press release and call it a filing read. Open the actual document.
- **Treating all risk factors as equal.** The diff against last year is where the signal is.
- **Quoting stale numbers.** Always lead with the period and filing date; a 10-K can be 11 months old.
- **Ignoring the notes.** The income statement is the headline; the notes are the story.
- **Confusing forms.** A 10-Q is unaudited; a 13F is 45 days lagged and long-only; an 8-K is an event, not a full report.
- **Over-reading one quarter.** 10-Q swings can be seasonal/timing — frame against the trailing trend.

## Quick reference

- EDGAR full-text search (2001→present): https://efts.sec.gov/LATEST/search-index?q=
- Company filings JSON: `https://data.sec.gov/submissions/CIK##########.json`
- Company facts (XBRL): `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- All EDGAR requests **must** send a descriptive `User-Agent` (name + email) or SEC returns 403. `edgar.py` does this for you.
