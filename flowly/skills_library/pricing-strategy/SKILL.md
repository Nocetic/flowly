---
name: pricing-strategy
description: "Design pricing and packaging — choose the value metric, structure tiers/packages, decide between cost-plus / competitive / value-based pricing, reason about willingness-to-pay and price elasticity, handle discounting and price increases, and plan price tests. Use when the user asks how to price a product, set up tiers/plans, what to charge, how to package features, whether to raise prices, or to fix a pricing model."
metadata: {"flowly":{"emoji":"🏷️","tags":["business","pricing","packaging","monetization","willingness-to-pay","saas","strategy"],"requires":{"bins":[]},"category":"business","related_skills":["market-sizing","competitor-analysis","business-case","startup-unit-economics"]}}
---

# Pricing Strategy — Capture the Value You Create

Pricing is the highest-leverage business decision and the most under-thought — a small price change drops almost entirely to the bottom line. The core principle: **price to the value the customer gets, not to your costs.** Cost sets a floor and competition sets a reference, but value sets the ceiling, and most companies leave money there. Packaging (how you bundle and tier) is half the work — it steers customers to the right plan and segments willingness-to-pay.

## What this skill produces

**Chat-first.** Default: a pricing recommendation — the value metric, the tier structure with what's gated where, the price points with rationale, and how to validate. Offer a fuller pricing-page layout or a test plan. Always tie price to value and segment, not to a gut number.

## When to use

- "How should I price \<product\>?" / "What should I charge?"
- "Design tiers / packaging / plans." / "What goes in which tier?"
- "Should we raise prices?" / "How do we handle discounting?"
- "Cost-plus vs value-based vs competitive?"
- "How do I test a price?" / "Why isn't our pricing working?"

## Step 1 — The value metric (get this right first)

The **value metric** is what you charge *per* — it should scale with the value the customer receives (seats, usage/API calls, GB, transactions, revenue processed). A good value metric: aligns price with value, grows the account as the customer succeeds (expansion revenue), and is easy to understand. A bad one (e.g. flat fee regardless of value) leaves money on the table and misaligns incentives. This choice matters more than the absolute number.

## Step 2 — Pricing approach

| Approach | Basis | When | Risk |
|---|---|---|---|
| **Cost-plus** | cost + margin | commodities, simple | ignores value → underprice the valuable, overprice the cheap |
| **Competitive** | match/beat rivals | crowded, reference-priced markets | race to the bottom; ignores your differentiation |
| **Value-based** | customer's ROI / WTP | differentiated products | needs WTP research, but captures the most |

Default to **value-based** where you have differentiation; use cost as the floor and competition as a sanity reference, not the driver.

## Step 3 — Willingness-to-pay & elasticity

- **WTP** varies by segment — that's *why* you tier. Research it: Van Westendorp price-sensitivity survey, conjoint, interviews, or simply testing. Don't guess one number for everyone.
- **Elasticity:** how demand responds to price. For differentiated/value products demand is often less elastic than founders fear — underpricing is more common than overpricing. A price increase usually raises revenue unless demand is very elastic.
- **Anchoring & psychology:** a high-end tier anchors perception and makes the middle look reasonable; charm pricing ($99 vs $100); annual-vs-monthly framing.

## Step 4 — Packaging & tiers

- **Good-better-best (3 tiers)** is the workhorse: it segments WTP, anchors with the top tier, and steers most buyers to the middle. Avoid choice overload (>~4 tiers).
- **Gate features by value and segment:** put must-haves for bigger customers (SSO, API, admin, SLA, advanced analytics) in higher tiers; keep the entry tier genuinely useful (or a free tier for PLG funnels).
- **Decide the model:** flat, per-seat, usage-based, hybrid (platform fee + usage), freemium. Usage-based scales with value but is less predictable; per-seat is simple but can cap expansion.
- **Free tier / trial:** free-forever for funnel & network effects (costs you margin) vs time-limited trial (urgency). Pick by motion.

## Step 5 — Discounting & price changes

- **Discounting:** discipline matters — ad-hoc discounts train customers to wait and erode price integrity. Trade discounts for something (annual commit, case study, multi-year). Set guardrails.
- **Raising prices:** usually under-done. Grandfather existing customers or phase in; communicate value; raise on new customers first. Most SaaS can and should raise prices periodically as they add value.
- **Expansion > acquisition:** the best pricing grows accounts over time (the value metric does this). Net revenue retention (→ `startup-unit-economics`) is the scoreboard.

## Step 6 — Validate (don't just decide)

- **Test:** A/B price tests (→ `ab-testing`), willingness-to-pay surveys, sales-call signals ("too expensive" rate, discount-ask rate), win/loss analysis.
- **Watch the signals:** if nobody pushes back on price, you're too cheap; if everyone does, too high or value unclear. ~20–30% "ouch" is roughly the sweet spot.

## Chat output format

```
**Pricing — B2B analytics SaaS**

Value metric: per tracked-event (scales with the customer's usage/value). ✅
Tiers (good-better-best):
  Starter $49/mo — 100k events, core dashboards (entry, genuinely useful)
  Growth $199/mo — 1M events, API, integrations (the steer-to middle)
  Scale  $custom — unlimited, SSO, SLA, support (enterprise anchor)
Rationale: gate API/SSO to higher tiers (enterprise WTP); usage metric drives expansion.
Validate: WTP survey + watch discount-ask rate; A/B the Growth price ($199 vs $249).
Discounting: only for annual commits. Plan a price review every ~12 months.
```

## Workflow

1. **Pick the value metric** that scales with customer value (the foundational choice).
2. **Choose the approach** — value-based where differentiated; cost = floor, competition = reference.
3. **Segment WTP** and design **good-better-best tiers**, gating features by value/segment.
4. **Set price points** with rationale (anchoring, charm, annual framing); keep the entry tier useful.
5. **Set discounting guardrails** and a price-increase cadence.
6. **Validate** via tests/surveys/sales signals; iterate.
7. **Deliver** metric + tiers + prices + validation plan; route WTP testing to `ab-testing`, retention/expansion to `startup-unit-economics`, rivals to `competitor-analysis`, ROI to `business-case`.

## Key pitfalls

- **Cost-plus on a differentiated product.** Caps you below the value you create — price to value.
- **Underpricing (the common error).** Founders fear churn more than they should; if nobody balks, raise.
- **Wrong/flat value metric.** A metric that doesn't scale with value blocks expansion and misaligns incentives.
- **Tier overload / unclear gating.** Too many plans or arbitrary gating confuses buyers; 3 tiers, value-based gating.
- **Undisciplined discounting.** Ad-hoc discounts erode integrity and train customers to wait — trade them for commitments.
- **Set-and-forget pricing.** You add value over time; review prices periodically and raise.
- **Deciding without validating.** Test prices and read sales signals; don't ship a guessed number permanently.

## Quick reference

- Price to **value** (ceiling); cost = floor, competition = reference. Value-based where differentiated.
- **Value metric** scaling with customer value drives expansion — the #1 choice.
- Good-better-best tiers; gate by value/segment; keep entry useful; ≤~4 tiers; anchor with the top.
- WTP varies by segment (that's why you tier); underpricing is the common mistake.
- Discount only in exchange for commitment; raise prices periodically; grandfather/phase-in.
- Validate: A/B (ab-testing), WTP surveys, discount-ask/“ouch” rate (~20–30% sweet spot). Expansion → startup-unit-economics.
