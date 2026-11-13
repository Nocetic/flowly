---
name: merger-model
description: "Build a merger / M&A model — accretion/dilution analysis on pro-forma EPS, the financing mix (cash / stock / debt), purchase-price allocation and goodwill, synergies (cost + revenue), and the breakeven synergy / premium math. Includes a Python helper for the accretion/dilution calc across financing structures. Use when the user asks if an acquisition is accretive or dilutive, about deal financing, synergies, a merger's EPS impact, or whether an M&A deal makes sense."
metadata: {"flowly":{"emoji":"🤝","tags":["finance","m&a","merger","accretion-dilution","synergies","ppa","goodwill","deal-financing","investment-banking"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["lbo-model","comps-analysis","dcf-model","credit-analysis","finance"]}}
---

# Merger Model — Is This Deal Accretive, and Does It Create Value?

A merger model answers two different questions that people constantly conflate. The first is mechanical: **does the deal raise or lower the acquirer's EPS** (accretion/dilution)? The second is real: **does it create value** (are synergies worth more than the premium paid)? A deal can be accretive and value-destructive, or dilutive and brilliant. Always answer both.

## What this skill produces

**Chat-first.** Default: the accretion/dilution verdict (pro-forma EPS vs standalone, by financing mix), the breakeven synergies/premium, and a one-line read on whether the deal is sensible. Offer a full `.xlsx` (via `excel-author`) for a combined pro-forma with sources & uses and a synergy phase-in.

## When to use

- "Is this acquisition accretive or dilutive?" / "What's the EPS impact?"
- "Should Acquirer buy Target?" / "Does this deal make sense?"
- "How should they finance it — cash, stock, or debt?"
- "How much in synergies do they need to justify the premium?"
- "What premium can they pay and stay accretive?"

## The accretion/dilution mechanics

Pro-forma EPS = **combined net income ÷ combined share count.** Whether it's higher or lower than the acquirer's standalone EPS depends entirely on how the deal is financed.

**Pro-forma net income** = Acquirer NI + Target NI + after-tax synergies − after-tax incremental interest (new debt) − after-tax foregone interest (cash used) − incremental D&A from asset write-ups (PPA).

**Pro-forma shares** = Acquirer shares + new shares issued (stock deals only).

### The financing-mix intuition (the quick gut check)
Compare the **acquirer's P/E** to the deal's effective cost of funding:
- **All-stock:** accretive if acquirer P/E **>** target P/E (paid for, post-premium). A higher-multiple acquirer buying a lower-multiple target with its expensive stock is accretive — you're issuing cheap-to-you currency.
- **All-cash:** accretive if the **after-tax yield on the target's earnings** (target earnings ÷ purchase price, i.e. 1/deal-P/E) **>** the after-tax interest/opportunity cost of the cash/debt used. With low rates, cash deals are usually accretive; the cost is balance-sheet/leverage, not EPS.
- **Debt-financed:** same as cash but the cost is the after-tax cost of debt; watch the leverage (cross-check `credit-analysis`).
- The **cheapest financing is usually most accretive** — but accretion ≠ value. Stock deals share the downside with the seller; cash/debt keep all the upside but all the risk.

## Purchase-price allocation (PPA) & goodwill

- **Premium** = offer price − target's unaffected market price (typically 20–40% in control deals).
- Allocate the purchase price: write up identifiable assets to fair value, recognize intangibles; the residual over net identifiable assets = **goodwill** (not amortized, but tested for impairment).
- Asset/intangible **write-ups create incremental D&A** that drags pro-forma earnings (and is a real accretion headwind) — don't forget it.
- **Transaction & financing fees** reduce value and (for financing fees) get amortized.

## Synergies — where deals are won or lost

- **Cost synergies** (overhead, facilities, headcount, procurement): more credible, faster, more controllable. Phase them in (rarely 100% day one) and net out the **costs to achieve** them.
- **Revenue synergies** (cross-sell, pricing): cited often, realized rarely. Haircut them hard; many models should ignore them in the base case.
- **The breakeven test:** how much in annual synergies makes the deal value-neutral (covers the premium)? If required synergies exceed what's plausibly achievable, the acquirer is overpaying — full stop. This is the most important output of the whole model.

## Value creation vs accretion (don't confuse them)

- **Accretion/dilution is an EPS-mechanics result**, heavily driven by financing and the multiple gap — it can be engineered.
- **Value creation** = (target standalone value + synergies) vs price paid. A deal is good if synergies + the business are worth more than the premium, regardless of the EPS optics.
- Always state both, and flag when they disagree ("accretive but value-destructive — the accretion is just cheap debt; required synergies exceed plausible").

## The helper

`scripts/merger_accretion.py` computes pro-forma EPS and accretion/dilution across financing mixes, plus the breakeven synergy level.

```bash
python3 scripts/merger_accretion.py \
  --acq-ni 1000 --acq-shares 500 --acq-price 50 \
  --tgt-ni 200 --tgt-shares 100 --offer-price 40 --tgt-unaffected 30 \
  --pct-stock 0.5 --pct-cash 0.3 --pct-debt 0.2 \
  --debt-rate 0.06 --cash-yield 0.03 --tax 0.25 \
  --synergies 50 --incremental-da 20
```
Stdlib only. (NI, synergies, D&A in \$M; shares in M; prices per share.)

## Data sourcing

- Financials & share counts from filings (`sec-filings` / `edgar.py`); market prices from the user or a cited source — **never invent prices, share counts, or synergies**.
- Get the **target's unaffected price** (pre-rumor) for the true premium.
- Date everything; deal terms and prices move.

## Chat output format

```
**Merger — Acquirer / Target** ($40/sh offer, 33% premium)

Financing: 50% stock / 30% cash / 20% debt
Standalone acq EPS $2.00 → Pro-forma EPS $2.08 → **+4% accretive** ✅
(driver: cheap debt + multiple gap; PPA D&A is a $0.04 drag)

Breakeven synergies: ~$30M/yr to be EPS-neutral; deal assumes $50M.
Value check: premium = $1.0B; PV of credible (cost) synergies ≈ $0.7B 🚩

Read: accretive on paper, but you're paying ~$1.0B premium for ~$0.7B of
believable synergies — accretion is financing-driven, not value-driven.
```

## Workflow

1. **Gather both companies:** NI, shares, share prices, target unaffected price, tax rate.
2. **Set the offer** (price/premium) and **financing mix** (stock/cash/debt).
3. **Estimate synergies** (cost first, haircut revenue) and incremental D&A from PPA.
4. **Run `merger_accretion.py`** for pro-forma EPS, accretion/dilution, and breakeven synergies.
5. **Test the financing mix** — show how accretion changes across stock/cash/debt.
6. **Do the value check:** premium paid vs PV of credible synergies.
7. **Deliver both verdicts** (EPS impact + value creation), flag disagreements; cross-ref `lbo-model` if a sponsor alternative is relevant, `credit-analysis` for the debt capacity.

## Key pitfalls

- **Reporting accretion as if it means "good deal."** Financing can engineer accretion; value creation is the real test.
- **Forgetting PPA D&A.** Asset write-ups create incremental amortization that drags EPS — a common omission that flips the verdict.
- **Trusting revenue synergies.** Haircut hard or exclude; cost synergies are the credible ones.
- **Ignoring costs-to-achieve.** Synergies aren't free; net them out and phase them in.
- **Wrong premium base.** Use the *unaffected* (pre-rumor) target price, not the already-run-up price.
- **No breakeven test.** "Required synergies vs achievable synergies" is the single best overpayment check.
- **Mixing diluted/basic shares** or forgetting new shares issued in stock deals.

## Quick reference

- Pro-forma EPS = (Acq NI + Tgt NI + after-tax synergies − after-tax new interest − after-tax foregone interest on cash − after-tax incremental D&A) ÷ (Acq shares + new shares issued)
- Accretion/dilution % = Pro-forma EPS ÷ Standalone EPS − 1
- Quick rule (all-stock): accretive if acquirer P/E > target deal P/E
- Quick rule (all-cash/debt): accretive if 1/deal-P/E > after-tax cost of financing
- New shares (stock) = (stock portion of consideration) ÷ acquirer share price
- Premium = offer ÷ unaffected price − 1
- Breakeven synergies = the annual after-tax synergy that sets pro-forma EPS = standalone EPS
- Value test: premium paid vs PV of credible synergies — the deal's real verdict.
