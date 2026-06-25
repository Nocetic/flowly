---
name: startup-unit-economics
description: "Analyze a startup's unit economics and SaaS/subscription health — CAC, LTV, LTV/CAC, CAC payback, gross margin, churn/retention (logo + net revenue), cohort behavior, burn multiple, magic number, Rule of 40, and runway. Includes a Python helper that computes the full scorecard. Use when the user asks if a startup's economics work, about CAC/LTV, churn, payback, burn, SaaS metrics, or whether growth is efficient."
metadata: {"flowly":{"emoji":"🚀","tags":["finance","startup","saas","unit-economics","cac","ltv","churn","retention","burn-multiple","rule-of-40"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["comps-analysis","earnings-analysis","risk-modeling","finance"]}}
---

# Startup Unit Economics — Does the Business Actually Work?

Unit economics strip a startup down to one question: **does each customer make more than it costs to win and keep them, and how fast?** Revenue growth hides everything; unit economics reveal whether that growth is a business or a bonfire. Center on the per-customer P&L and the *quality* and *efficiency* of growth, not the top-line vanity number.

## What this skill produces

**Chat-first.** Default: a unit-economics scorecard — CAC, LTV, LTV/CAC, payback, gross margin, NRR/churn, burn multiple / magic number, Rule of 40, runway — each with a health flag and the one-line verdict. Offer a file for a cohort model or a board-ready deck.

## When to use

- "Do this startup's unit economics work?" / "Is this business healthy?"
- "What's my CAC / LTV / LTV-to-CAC / payback?"
- "Is my churn / retention good?" / "What's a good NRR?"
- "Am I growing efficiently?" / "Is my burn reasonable?" / "Burn multiple?"
- "What's my runway?" / "Rule of 40?"
- "Should I spend more on sales & marketing?"

## The metrics that matter (and the honest definitions)

### Acquisition cost — CAC
**CAC = fully-loaded S&M spend ÷ new customers acquired** in the period.
- "Fully-loaded" = ad spend **+ sales salaries/commissions + marketing headcount + tools**. CAC that counts only ad spend is a vanity number.
- Distinguish **blended CAC** (all new customers ÷ all S&M, includes organic) from **paid CAC** (paid channels only). Paid CAC is the marginal truth; blended flatters you when organic is strong.
- Watch CAC *trend* — rising CAC as you scale is the normal failure mode (you exhaust the cheap channels).

### Lifetime value — LTV (a.k.a. CLV)
**LTV = (ARPA × gross margin %) ÷ churn rate.**
- **Use gross-margin-based LTV**, not revenue LTV — you keep the margin, not the revenue. Revenue LTV overstates value, badly, for low-margin businesses.
- Churn here is the *revenue/customer* churn rate (monthly or annual — be consistent with ARPA).
- LTV is a projection built on a churn assumption; an early-stage company doesn't have enough history to know its real churn, so treat LTV as a range, not a fact.

### The headline ratios
| Metric | Healthy | What it means |
|---|---|---|
| **LTV / CAC** | **≥ 3x** | Value created per dollar of acquisition. <1x = losing money per customer; >5x may mean *under*-investing in growth |
| **CAC payback** | **< 12 mo** (< 18 for enterprise) | Months of gross profit to recover CAC; the cash-flow reality behind LTV/CAC |
| **Gross margin** | 70–85% (SaaS) | The margin everything else is computed on; <60% isn't really "software" economics |

### Retention — the foundation everything rests on
- **Logo churn** (customers lost) vs **revenue churn** (dollars lost).
- **Net Revenue Retention (NRR / NDR)** = (start ARR + expansion − contraction − churn) ÷ start ARR. **>100% means the existing base grows by itself** (the holy grail — best SaaS hits 120%+). <100% means you're refilling a leaky bucket before you even add new logos.
- **Gross Revenue Retention (GRR)** ≤ 100% always; isolates pure churn (no expansion mask).
- **Cohort retention curves** — does retention *flatten* (a real, sticky base) or decay to zero (no product-market fit)? A flattening curve is the single best PMF signal.

### Growth efficiency (how much cash per dollar of growth)
- **Burn multiple = net burn ÷ net new ARR.** Lower is better. <1 excellent, 1–2 good, 2–3 wasteful, >3 alarming. The cleanest single efficiency metric.
- **Magic number = net new ARR ÷ prior-period S&M.** >0.75 means S&M is paying off; <0.5 means stop and fix before spending more.
- **Rule of 40:** revenue growth % + profit (or FCF) margin % **≥ 40**. Balances growth vs profitability; lets you compare a hyper-grower burning cash against a slower, profitable one.

### Survival
- **Runway = cash ÷ net monthly burn** (months). Pair with the trend — is burn rising or falling?
- **Net burn** = cash out − cash in (the real number), not gross burn.

## The helper

`scripts/unit_econ.py` computes the whole scorecard from the inputs you have and flags each metric.

```bash
python3 scripts/unit_econ.py \
  --arpa 1200 --arpa-monthly --gross-margin 0.78 --monthly-churn 0.02 \
  --sm-spend 500000 --new-customers 250 \
  --net-new-arr 1500000 --net-burn 1200000 --prior-sm 450000 \
  --revenue-growth 0.9 --fcf-margin -0.3 --cash 18000000
```
Stdlib only. Pass only what you have; it computes what it can.

## Data sourcing

- Numbers from the user / the company's data — **never invent CAC, churn, or ARR**.
- **Pin down the period and whether churn is monthly or annual** (a 2% monthly churn ≈ 22% annual — mixing them is the #1 error here).
- Distinguish **bookings vs revenue vs ARR vs cash** — they are not interchangeable.

## Chat output format

```
**Unit economics** (B2B SaaS)

CAC $2,000 · LTV $46,800 (GM-based) · **LTV/CAC 23x** ⚠️ (likely under-investing)
CAC payback 2.1 mo ✅ · Gross margin 78% ✅
NRR 118% ✅ · monthly churn 2.0% (≈22%/yr) ❌
Burn multiple 0.8 ✅ · Magic number 3.3 ✅ · Rule of 40 = 60 ✅
Runway 15 mo

Verdict: efficient, sticky (NRR 118%), and arguably *under*-spending on
growth — LTV/CAC of 23x says step on the gas. Watch the 22%/yr logo churn.
```

## Workflow

1. **Get the inputs:** ARPA, gross margin, churn (monthly/annual — confirm!), S&M spend, new customers, ARR movement, burn, cash.
2. **Run `unit_econ.py`** for the scorecard.
3. **Interpret together, not in isolation:** a great LTV/CAC with a 30-month payback is a cash-flow problem; high NRR can mask high logo churn.
4. **Check the retention curve shape** if cohort data exists (PMF signal).
5. **Diagnose the constraint:** acquisition (CAC), monetization (ARPA/margin), retention (churn/NRR), or efficiency (burn) — name the binding one.
6. **Deliver** the scorecard + verdict + the one lever to pull; offer a cohort model.

## Key pitfalls

- **Revenue LTV instead of gross-margin LTV.** Overstates value; always margin-adjust.
- **Vanity CAC** (ad spend only). Fully load it with sales/marketing headcount and tools.
- **Mixing monthly and annual churn.** Convert and state which; it swings LTV by an order of magnitude.
- **LTV/CAC without payback.** A 5x ratio with a 24-month payback can still starve you of cash.
- **NRR hiding churn.** A few whales expanding can mask a leaky base — always show GRR/logo churn too.
- **Trusting early-stage LTV.** Without retention history, churn (and thus LTV) is a guess — present a range.
- **Growth at any cost.** Burn multiple > 3 and magic number < 0.5 mean the growth is bought, not earned — fix unit economics before scaling spend.
- **Confusing bookings/ARR/revenue/cash.** Be explicit about which metric.

## Quick reference

- CAC = fully-loaded S&M ÷ new customers
- LTV = (ARPA × gross margin) ÷ churn rate  ·  or ARPA × GM × avg lifetime (lifetime ≈ 1/churn)
- LTV/CAC ≥ 3x healthy · CAC payback = CAC ÷ (ARPA × GM) months, < 12 healthy
- NRR = (start + expansion − contraction − churn) ÷ start; >100% = net-negative churn
- Burn multiple = net burn ÷ net new ARR (lower better; <1 great)
- Magic number = net new ARR ÷ prior-quarter S&M (>0.75 good)
- Rule of 40 = growth % + profit/FCF margin % ≥ 40
- Runway = cash ÷ net monthly burn
