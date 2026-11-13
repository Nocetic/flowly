---
name: customer-research
description: "Synthesize customer/user research — turn interviews, surveys, and feedback into jobs-to-be-done, personas, pain maps, and prioritized insights via affinity grouping; and design unbiased research (good questions, avoiding leading/confirmation bias). Use when the user has interview notes/survey data/feedback to synthesize, wants personas or JTBD, a pain/needs map, to plan customer interviews, or to find what users actually need."
metadata: {"flowly":{"emoji":"🔎","tags":["business","customer-research","ux-research","jobs-to-be-done","personas","interviews","synthesis"],"requires":{"bins":[]},"category":"business","related_skills":["product-requirements","sales-call-analysis","competitor-analysis","ab-testing"]}}
---

# Customer Research — What People Actually Need (Not What They Say)

The value of research is in the **synthesis**, not the transcripts — finding the patterns across many voices and separating what people *say* from what they *do* and *need*. Done well it kills bad ideas cheaply and reveals real demand; done badly (leading questions, cherry-picked quotes, n=1 generalizations) it manufactures false confidence. Anchor on **behavior and jobs**, treat opinions and feature-requests as clues to underlying needs, not orders.

## What this skill produces

**Chat-first.** Default: synthesized insights — the recurring jobs/pains/themes (with how many sources support each), a pain map or persona sketch, and prioritized findings with implications. For planning, an interview guide / discussion plan. Evidence-weighted: distinguish a pattern across many from a single anecdote.

## When to use

- "Synthesize these interviews / survey responses / feedback."
- "What do our users actually need?" / "Find the patterns."
- "Build personas / a jobs-to-be-done map / a pain map."
- "Plan customer interviews / write a discussion guide."
- "Are we building the right thing?" (demand validation)

## Synthesis: from raw voices to insight

1. **Affinity grouping** — cluster individual observations/quotes into themes bottom-up (don't force preset buckets). The clusters that recur across many participants are the signal.
2. **Weight by evidence** — note *how many* sources support each theme and whether it came up unprompted (stronger) vs prompted. One vivid quote ≠ a trend. Flag n=1.
3. **Separate layers:** what they **said** (opinions, requests) vs what they **do** (behavior) vs what they **need** (the underlying job). Behavior and the job are the truth; requests are clues. ("Faster horse" → the job is "get there quicker.")
4. **Surprise & disconfirmation** — actively note what contradicts your assumptions; that's the highest-value finding. Research that only confirms the plan probably had leading questions.

## Jobs-to-be-done (JTBD)

Frame needs as **jobs**: *"When [situation], I want to [motivation], so I can [outcome]."* JTBD focuses on the progress the customer is trying to make, independent of any solution — which is why it survives when features don't. Capture functional, emotional, and social dimensions. Prioritize jobs by **importance × dissatisfaction** (high-importance, poorly-served jobs = opportunity).

## Personas (lightweight, behavior-based)

A useful persona captures **goals, context, behaviors, pains, and the jobs they hire products for** — not demographics theater ("Marketing Mary, 34, likes yoga"). Keep 2–4 distinct personas tied to real behavioral segments; each should imply different product/marketing decisions or it's not worth having.

## Pain map / opportunity sizing

Map pains by **severity × frequency × how well current solutions address them**. The sweet spot: frequent, severe, badly-served pains. This prioritizes where to build (→ `product-requirements`) and reveals where competitors fall short (→ `competitor-analysis`).

## Designing research (avoid biasing it)

- **Ask about past behavior, not hypothetical futures.** "Tell me about the last time you…" beats "Would you use…" (people are terrible at predicting their own behavior and will be polite).
- **Open, non-leading questions.** "What's frustrating about X?" not "Don't you hate how slow X is?" Avoid yes/no and questions that signal the desired answer.
- **Dig with "why" / "tell me more"** — the first answer is rarely the real one (5-whys to root cause).
- **Don't pitch or sell** during discovery — you'll get courtesy validation. Listen far more than you talk.
- **Sample right:** talk to real target users (and non-users/churned), enough for saturation (~5–8 per segment until themes repeat). Beware survivorship bias.

## Chat output format

```
**Research synthesis — 9 interviews, SMB owners**

Top jobs (× support):
1. "Know cash position at a glance" (8/9, unprompted) — high importance, poorly served 🎯
2. "Look professional to clients" (6/9) — emotional/social job
3. "Stop chasing invoices" (5/9)

Pain map: late-payment chasing = frequent + severe + badly served → biggest opportunity.
Surprise: nobody wanted the analytics we planned (0/9 unprompted) — disconfirms roadmap.
Personas: "Solo operator" (time-poor, mobile-first) vs "Small-team owner" (delegation, controls).
→ Prioritize cash-at-a-glance + invoice chasing; reconsider the analytics bet. (→ product-requirements)
```

## Workflow

1. **Clarify the question** (validate demand? understand a segment? prioritize pains?) and the data (interviews/survey/feedback) — or design the study + discussion guide if not yet run.
2. **Affinity-group** observations bottom-up; weight themes by support and prompted/unprompted.
3. **Separate said/do/need**; frame needs as JTBD; note disconfirming surprises.
4. **Map pains** (severity×frequency×current-solution) and sketch behavior-based personas.
5. **Prioritize insights** with implications; flag n=1 and confidence.
6. **Deliver** synthesis + priorities; route to `product-requirements` (build), `competitor-analysis` (gaps), `ab-testing` (validate quantitatively), `sales-call-analysis` (deal-level signal).

## Key pitfalls

- **Taking feature requests literally.** "Build X" is a clue to a need; solve the job, don't just ship the ask.
- **Said ≠ done.** People misreport behavior and give polite answers — weight behavior and past actions over stated intentions.
- **Cherry-picking quotes / n=1 generalization.** One quote isn't a trend; count support and flag single-source claims.
- **Leading questions & pitching.** Biases the whole study toward your hypothesis — ask open, behavior-based questions; don't sell.
- **Demographics-theater personas.** Base personas on behavior/goals/jobs, not age/hobbies; each must drive a decision.
- **Confirmation bias.** If findings only confirm the plan, suspect the method — hunt for disconfirmation.
- **Wrong/too-small sample.** Talk to real target users (incl. non-users) to saturation; beware survivorship bias.

## Quick reference

- Synthesize via affinity grouping; weight by support count and unprompted-ness; flag n=1.
- Separate said (opinion) / do (behavior) / need (the job). Behavior + job = truth; requests = clues.
- JTBD: "When [situation], I want to [motivation], so I can [outcome]." Prioritize by importance × dissatisfaction.
- Personas = goals/context/behaviors/jobs (not demographics); 2–4 decision-driving ones.
- Pain map = severity × frequency × current-solution gap → opportunity.
- Interview: ask about the last real time, open/non-leading, dig with why, don't pitch, sample to saturation.
- Build → product-requirements; validate → ab-testing; gaps → competitor-analysis.
