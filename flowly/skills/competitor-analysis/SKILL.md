---
name: competitor-analysis
description: "Analyze competitors — build a feature/pricing comparison matrix, map positioning and segments, assess moats and differentiation, run a structured SWOT, and compare go-to-market. Sourced from public evidence with dates. Use when the user wants a competitive landscape, to compare their product against rivals, find differentiation/white space, a competitive teardown, or understand a competitor's strategy."
metadata: {"flowly":{"emoji":"🔭","tags":["business","competitive-analysis","strategy","positioning","moat","swot","gtm"],"requires":{"bins":[]},"category":"business","related_skills":["market-sizing","pricing-strategy","business-case","customer-research"]}}
---

# Competitor Analysis — Where You Win, Where You Don't

Useful competitive analysis is decision-oriented, not a feature spreadsheet for its own sake. The goal is to find **where you genuinely differentiate, where you're exposed, and the white space nobody owns** — then translate that into positioning and roadmap. Evidence over vibes: every claim about a competitor should trace to something observable (their site, pricing page, docs, reviews, filings), with a date, because competitors change.

## What this skill produces

**Chat-first.** Default: a tight comparison (matrix of the few axes that matter + the strategic read — differentiation, threats, white space). Offer a fuller teardown or a battlecard for sales. Keep the matrix to decision-relevant rows, not every feature.

## When to use

- "Who are our competitors and how do we compare?"
- "Competitive landscape / teardown for \<market\>."
- "Where can we differentiate?" / "What's the white space?"
- "Compare us vs \<competitor(s)\> on features/pricing."
- "What's \<competitor\>'s strategy / moat?"

## What to actually analyze (not just features)

1. **Segment & positioning** — who each player targets and the value prop they lead with. Two products with identical features can occupy different positions (premium vs cheap, SMB vs enterprise). Position is often the real battleground.
2. **Feature/capability matrix** — only the axes that drive buying decisions. Mark ✅/⚠️/❌ (or better/worse), not a checkbox dump. Highlight the few that actually differentiate.
3. **Pricing & packaging** — model, tiers, what's gated, effective price for a real use case. (→ `pricing-strategy`.) Pricing reveals strategy and target segment.
4. **Moat / defensibility** — network effects, switching costs, data, brand, scale, ecosystem/integrations, IP. A feature is copied in a quarter; a moat isn't. Distinguish a temporary feature lead from a durable advantage.
5. **Go-to-market** — sales motion (PLG vs sales-led), channels, ICP, marketing angle. How they *win deals*, not just what they ship.
6. **Strengths/weaknesses from the customer's view** — mine reviews (G2/Reddit/app stores) for what users love and hate; that's ground truth competitors won't tell you.

## Frameworks (use as lenses, don't ritualize)

- **SWOT** — per competitor or for you vs the field: Strengths/Weaknesses (internal), Opportunities/Threats (external). Keep it to the few items that change decisions.
- **Positioning map** — plot players on the two axes that matter to buyers (e.g. price × ease-of-use) to expose clustering and **white space**.
- **Porter's five forces** for the broader market structure (rivalry, new entrants, substitutes, supplier/buyer power) when the question is "how attractive is this market."

## Sourcing (evidence + dates)

- Public, citable: competitor sites, pricing pages, docs/changelogs, review sites, job postings (reveal roadmap/tech), filings/earnings (public cos → `sec-filings`/`earnings-analysis`), news.
- **Date every claim** — competitors ship and reprice constantly; an undated comparison rots.
- **Never invent** a competitor's price, feature, or metric — mark "not public" rather than guessing.
- Separate fact (observable) from inference (your read) explicitly.

## Chat output format

```
**Competitive read — us vs A vs B** (as of 2026-06)

| Axis | Us | A | B |
|------|----|----|----|
| Target | SMB | Enterprise | Prosumer |
| Pricing | $20/mo flat | $$$ sales-led | freemium |
| Key gap | — | no API ❌ | weak support ⚠️ |
| Moat | integrations | brand+scale | community |

White space: SMB + API + real support — A ignores SMB, B has no API.
Threat: B's freemium could move upmarket into us (watch their roadmap).
→ Position on "API-first for SMBs"; battlecard vs A: speed & price; vs B: reliability.
```

## Workflow

1. **Pick the right competitors** — direct, indirect, and the "do nothing / status quo" alternative. Don't omit the non-obvious substitute.
2. **Gather evidence** (sites, pricing, reviews, filings) with dates; separate fact from inference.
3. **Build the matrix** on decision axes; add positioning map + moat assessment.
4. **Find the strategic insight** — your durable differentiation, your exposure, and the white space.
5. **Translate to action** — positioning, roadmap priorities, sales battlecards.
6. **Deliver** matrix + read + actions; route market size to `market-sizing`, pricing to `pricing-strategy`, customer truth to `customer-research`, the decision to `business-case`.

## Key pitfalls

- **Feature-checkbox theater.** A giant matrix where you win every row is biased and useless — focus on decision-driving axes and be honest where you lose.
- **Ignoring positioning.** Same features, different segment/position = different competitor. Analyze the position, not just the spec sheet.
- **Confusing a feature lead with a moat.** Features get copied; ask what's *defensible*.
- **Home-team bias.** You'll overrate yourself — use customer reviews as the neutral check.
- **Omitting the status-quo / indirect substitute.** Often the real competitor is "a spreadsheet" or "nothing."
- **Undated / invented facts.** Date everything; mark unknowns as unknown.
- **Analysis without action.** End with positioning/roadmap/battlecard, not a static table.

## Quick reference

- Analyze: segment/positioning, decision-axis features, pricing/packaging, moat, GTM, customer-voiced strengths/weaknesses.
- Lenses: SWOT (sparingly), positioning map (find white space), five forces (market attractiveness).
- Include direct + indirect + status-quo competitors; date and cite; fact vs inference.
- Output = matrix + strategic read (differentiation/threat/white space) + actions (positioning, roadmap, battlecard).
- Market size → market-sizing; pricing → pricing-strategy; customer truth → customer-research.
