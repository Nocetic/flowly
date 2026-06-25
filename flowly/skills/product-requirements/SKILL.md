---
name: product-requirements
description: "Write product requirements — PRDs, user stories with acceptance criteria, edge cases, scope and explicit non-goals, success metrics, and launch checklists. Turns a feature idea into a buildable, testable spec. Use when the user wants a PRD, a spec, user stories, acceptance criteria, to define a feature, scope a project, or a launch/readiness checklist."
metadata: {"flowly":{"emoji":"📋","tags":["business","product","prd","requirements","user-stories","acceptance-criteria","spec"],"requires":{"bins":[]},"category":"business","related_skills":["customer-research","competitor-analysis","writing-plans","ab-testing"]}}
---

# Product Requirements — From Idea to Buildable, Testable Spec

A good PRD answers **why, what, and how-we'll-know** — not how to build it (that's the team's job). Its real value is forcing clarity: the problem, who it's for, what "done" means (testable acceptance criteria), and explicitly **what's out of scope**. Most failed features die from vague requirements, missing edge cases, or scope that quietly grew. Write the spec so an engineer and a QA could build and verify it without guessing — and a reviewer could tell if it succeeded.

## What this skill produces

**Chat-first.** Default: a structured PRD or a set of user stories with acceptance criteria, scope/non-goals, edge cases, and success metrics. Offer the full document (`.md`/`.docx`) for a formal spec. Precision over prose — testable statements, not aspirations.

## When to use

- "Write a PRD / spec for \<feature\>."
- "Turn this idea into user stories / acceptance criteria."
- "Define / scope this feature." / "What are the edge cases?"
- "Launch checklist for \<feature\>." / "Definition of done."

## PRD structure

1. **Problem & why now** — the user problem (tie to evidence → `customer-research`) and why it matters/is timely. The reason this is worth building.
2. **Goals & success metrics** — what outcome defines success, measurably (e.g. "reduce checkout abandonment from 30%→25%"). Pick a primary metric + guardrails (→ `ab-testing`). No metric = you can't tell if it worked.
3. **Users & use cases** — who it's for and the primary scenarios (personas/JTBD from research).
4. **Scope & non-goals** — what's in v1, and **explicitly what's out** (the single most valuable section — non-goals prevent scope creep and set expectations). Phase later wants into "future."
5. **Requirements / user stories** — the functional behavior as stories + acceptance criteria (below).
6. **Edge cases & error states** — the unhappy paths (empty/error/loading, limits, concurrency, permissions, offline) — where most bugs and rework live.
7. **Dependencies, risks, open questions** — what it relies on, what could go wrong, what's undecided.
8. **Rollout** — flagging, phased release, migration, launch checklist.

## User stories & acceptance criteria

- **Story:** *"As a [user], I want to [action], so that [benefit]."* Keep it user-and-outcome focused, not solution-prescriptive.
- **Acceptance criteria = the testable definition of done.** Prefer **Given/When/Then**:
  > Given [context], When [action], Then [observable result].
  Each criterion must be unambiguous and verifiable — if QA can't write a pass/fail test from it, it's not done. Cover the happy path **and** the key edge/error cases.
- **INVEST** check for stories: Independent, Negotiable, Valuable, Estimable, Small, Testable. Slice big stories until each is shippable and testable.

## Edge cases (the checklist that prevents rework)

For each feature, deliberately consider: **empty** (no data / first run), **error** (network/validation/permission failures), **boundary** (max/min, very long input, zero, huge volume), **concurrency** (two users/tabs at once), **permissions/auth** (who can/can't), **state** (loading, partial, offline/retry), and **i18n/accessibility** if relevant. Spelling these out up front is far cheaper than discovering them in production.

## Launch / readiness checklist (template)

- Acceptance criteria met & tested (incl. edge cases)
- Feature-flagged / phased rollout plan
- Success metric instrumented (you can measure it)
- Error handling & monitoring/alerts in place
- Docs / help / changelog updated
- Migration/backfill (if data changes); rollback plan
- Stakeholder sign-off; support team briefed

## Chat output format

```
**PRD — One-click reorder** (v1)

Problem: repeat buyers re-find past items manually; 18% cite it as friction (research).
Goal: lift repeat-purchase conversion 12%→15% (primary); no drop in AOV (guardrail).
Users: returning customers with ≥1 prior order.

In scope: reorder a past order from order history.
Non-goals: subscriptions, partial reorder, reorder from email (future).

Story: As a returning customer, I want to reorder a past order in one click,
so I can repurchase without rebuilding the cart.
Acceptance:
  - Given a past order with all items in stock, When I click Reorder, Then the
    cart is filled with those items at current prices and I'm taken to checkout.
  - Given some items are out of stock, When I reorder, Then available items are
    added and out-of-stock ones are listed as skipped.
Edge cases: empty order history (hide button); price changed (use current + notify);
  item discontinued (skip + message); not logged in (prompt sign-in).
Metric: reorder→purchase rate. Rollout: flag to 10% → 100%. Rollback: disable flag.
```

## Workflow

1. **Anchor on the problem + evidence** (→ `customer-research`) and **why now**.
2. **Define measurable success** (primary metric + guardrails).
3. **Set scope and explicit non-goals** — phase the rest.
4. **Write user stories + Given/When/Then acceptance criteria** (testable); slice with INVEST.
5. **Enumerate edge/error cases** from the checklist.
6. **Add dependencies/risks/open questions + rollout/launch checklist.**
7. **Deliver** inline or as a doc; route research to `customer-research`, competitive context to `competitor-analysis`, the implementation plan to `writing-plans`, validation to `ab-testing`.

## Key pitfalls

- **No non-goals.** The biggest source of scope creep — explicitly state what's out of v1.
- **Untestable acceptance criteria.** "Works well / is fast" can't be verified — use observable Given/When/Then with thresholds.
- **Missing edge/error cases.** The happy path is the easy 20%; the unhappy paths are where rework and bugs live — spec them.
- **No success metric.** Without a measurable goal you can't tell if the feature worked — define and instrument it.
- **Prescribing the solution / over-speccing.** Say what and why, leave how to the team (unless a constraint truly requires it).
- **Solution in search of a problem.** Lead with the user problem + evidence, not the feature.
- **Boiling the ocean.** Ship a small, valuable v1; phase the rest. INVEST-slice the stories.

## Quick reference

- PRD: problem/why-now → goals+metrics → users → **scope & non-goals** → stories+acceptance → edge cases → deps/risks → rollout.
- Story: "As a [user], I want [action], so that [benefit]." Acceptance: Given/When/Then, testable.
- INVEST stories; non-goals prevent scope creep; every feature needs a measurable success metric.
- Edge checklist: empty, error, boundary, concurrency, permissions, state/offline, i18n/a11y.
- Launch checklist: criteria tested, flagged, metric instrumented, monitoring, docs, rollback.
- Evidence → customer-research; build plan → writing-plans; validate → ab-testing.
