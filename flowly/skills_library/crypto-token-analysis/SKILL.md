---
name: crypto-token-analysis
description: "Analyze a crypto token's fundamentals — supply (circulating vs total vs max), FDV vs market cap, the vesting/unlock schedule and emissions, token allocation (team/investors/community), treasury, protocol revenue and value accrual, staking/yield, and risk flags (unlock cliffs, concentration, mercenary yield). Includes a Python helper for supply/FDV/unlock-dilution math. Use when the user asks about a token's tokenomics, unlocks, FDV, emissions, supply, or whether a token is over/undervalued on-chain."
metadata: {"flowly":{"emoji":"🪙","tags":["crypto","tokenomics","defi","fdv","unlocks","emissions","supply","protocol-revenue","valuation","web3"],"requires":{"bins":["python3"]},"category":"finance","related_skills":["polymarket","risk-modeling","comps-analysis","finance"]}}
---

# Crypto Token Analysis — Tokenomics, Not Vibes

A token's price is a story; its **tokenomics** are the mechanics that will overwhelm that story. The recurring way crypto investors get hurt isn't a bad narrative — it's **dilution they didn't model**: a low circulating supply masking a huge fully-diluted valuation, then cliff unlocks dumping supply onto a market that can't absorb it. Lead with supply and value accrual; treat price targets as downstream of those.

> **Not financial advice; crypto is high-risk.** Frame outputs as analysis. Tokens can go to zero; many have no cash-flow backing at all.

## What this skill produces

**Chat-first.** Default: a tokenomics snapshot — market cap vs FDV, circulating %, the next big unlocks, emissions/inflation, allocation concentration, value-accrual mechanism, and risk flags, with a one-line read. Offer a file for an unlock-schedule table or a full token report.

## When to use

- "Analyze \<token\>'s tokenomics." / "Is this token's supply a problem?"
- "What's the FDV vs market cap?" / "How dilutive is this?"
- "When do the unlocks / cliffs hit?" / "What's the emission/inflation rate?"
- "Who holds the supply — team/VCs/community?"
- "Does this protocol actually make money?" / "What's the value accrual?"
- "Is \<token\> over/undervalued on fundamentals?"

## The supply picture (start here, always)

| Term | Meaning | Why it matters |
|---|---|---|
| **Circulating supply** | Tokens liquid & tradeable now | The denominator of market cap |
| **Total supply** | Minted minus burned (incl. locked) | What exists today |
| **Max supply** | Hard cap (or "uncapped"/inflationary) | The ceiling on dilution |
| **Market cap** | Price × circulating | What the *liquid* token is valued at |
| **FDV** | Price × max (or fully-diluted) supply | What it'd be worth if *everything* were liquid |

**The MC/FDV ratio is the first thing to compute.** A token with \$200M MC but \$2B FDV (10% circulating) means **90% of supply is waiting to hit the market.** Today's price is being set by a thin float; as locked supply vests, that price must be defended against a flood of new sellers. Low circulating % + high FDV is the single most common value trap in crypto.

## Vesting & unlocks — the supply you'll be diluted by

- **Map the unlock schedule:** how much unlocks when, to whom (team, investors/VCs, foundation, ecosystem).
- **Cliffs** (a big chunk unlocking at once, e.g. the 1-year investor cliff) are far more dangerous than linear vesting — they're discrete supply shocks.
- **Compute the unlock as a % of circulating supply** and as a multiple of daily volume — a 5% unlock onto thin liquidity is a different event than onto deep markets.
- **VC/team unlocks** are the ones to fear: low cost basis, often in profit, with incentive to sell. Community/airdrop unlocks behave differently.
- Sources: token vesting trackers, the project's docs/whitepaper, on-chain vesting contracts. **Verify against the contract where possible** — docs lie or go stale.

## Emissions & inflation (the ongoing dilution)

- **Emission rate / annual inflation:** new tokens minted per year ÷ circulating. High emissions = constant sell pressure that the protocol must out-grow.
- **Where do emissions go?** Staking rewards, liquidity mining, validators. "Real yield" (paid from protocol revenue) ≠ "emissions yield" (paid by printing tokens, i.e. diluting you to pay you).
- **Net inflation** = emissions − burns. Some tokens have burn mechanisms (fee burns, buyback-and-burn) that offset or reverse inflation — model the net.

## Allocation & concentration

- **Who got the supply:** team, investors, foundation/treasury, community/airdrop, public sale, liquidity. A heavy insider allocation (team + VC > ~40%) is a governance and dump risk.
- **On-chain concentration:** top-10 / top-100 holder share; how much is on exchanges (sell-ready) vs staked/locked.
- **Insider cost basis** vs current price — how far in profit are the people who can sell?

## Value accrual — does owning the token *do* anything?

The core question: **why should this token be worth anything?**
- **Cash-flow-ish:** protocol revenue → buybacks/burns/staking distributions (the strongest case; treat like a quasi-equity yield).
- **Governance:** voting rights (often weak value accrual on its own).
- **Utility/gas:** required to use the network (demand scales with usage).
- **Pure speculation / meme:** no mechanism — say so plainly.
- **Fundamental ratios** (where revenue exists): **P/F (price-to-fees)**, **P/S (FDV-to-revenue)**, **fees/TVL**, MC/TVL. Compare to comparable protocols (see `comps-analysis` for the relative-value discipline). Annualize fees honestly (don't extrapolate one hot week).

## The helper

`scripts/tokenomics.py` does the supply/FDV/unlock math and prints a snapshot.

```bash
python3 scripts/tokenomics.py \
  --price 1.50 --circulating 200000000 --max-supply 1000000000 \
  --daily-volume 30000000 \
  --next-unlock-tokens 50000000 --next-unlock-label "investor cliff (Aug)" \
  --annual-emissions 80000000
```
Stdlib only.

## Data sourcing

- Supply, price, volume, unlocks: from the user or a **cited** source (market-data aggregators, vesting trackers, on-chain explorers). **Never invent supply figures or unlock dates.**
- **Verify on-chain when it matters** — contract supply, vesting contracts, treasury wallets are the ground truth; marketing docs are not.
- Stamp the as-of date and price — crypto data moves by the minute.
- For prediction-market-style "will token X do Y" questions, pair with `prediction-market-research`.

## Chat output format

```
**Tokenomics — TKN** (price $1.50, as of 2026-06-06)

MC $300M · FDV $1.5B · **circulating 20%** ⚠️ (80% still to unlock)
Next unlock: 50M (investor cliff, Aug) = 25% of float ≈ 1.7 days' volume 🚩
Emissions ~80M/yr → ~40% inflation on circulating ⚠️
Allocation: team+VC 45% 🚩 · community 30% · treasury 25%
Value accrual: fee-share to stakers; ~$12M annualized fees → P/F ~25x

Read: thin 20% float props up a $1.5B FDV; a 25%-of-float VC cliff in Aug
is the key risk. Real fee revenue exists (rare) but inflation is heavy.
Fundamentals secondary to the unlock overhang here.
```

## Workflow

1. **Pin the token + as-of price/date.**
2. **Supply first:** MC, FDV, circulating % → `token.py`.
3. **Unlock schedule:** next cliffs, % of float, vs volume; flag VC/team unlocks.
4. **Emissions/inflation** net of burns.
5. **Allocation & concentration**; insider cost basis.
6. **Value accrual:** mechanism + (if revenue) P/F / P/S vs comparables.
7. **Risk flags** → the read; offer a file. Hand off to `risk-modeling` for price-risk, `comps-analysis` for protocol relative value.

## Key pitfalls

- **Quoting market cap, ignoring FDV.** Low circulating % hides the dilution that will define returns. Always lead with MC *and* FDV.
- **Missing the cliffs.** Linear vesting is digestible; a one-day VC cliff is a supply shock — size it vs float and volume.
- **Emissions yield mistaken for real yield.** Being paid in freshly-printed tokens is dilution wearing a yield costume.
- **Trusting docs over chain.** Verify supply, vesting, and treasury on-chain where it matters.
- **Annualizing a hot week** of fees. Use a sane run-rate; crypto revenue is spiky.
- **No value-accrual answer.** If owning the token does nothing mechanically, say it's pure speculation — don't manufacture a thesis.
- **Stale snapshot.** Date and timestamp everything.

## Quick reference

- FDV = price × max (or fully-diluted) supply · Market cap = price × circulating
- Circulating % = circulating ÷ max supply (low % + high FDV = dilution overhang)
- Unlock impact = unlock tokens ÷ circulating (and ÷ daily volume for absorption)
- Annual inflation ≈ annual emissions ÷ circulating; net of burns
- Real yield (from revenue) ≫ emissions yield (from printing) in quality
- P/F = FDV ÷ annualized protocol fees; compare to peer protocols
- The supply schedule usually matters more than the narrative over 6–18 months.
