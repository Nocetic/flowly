---
name: sales-call-analysis
description: "Analyze a sales call or transcript — extract the prospect's pains and goals, objections, buying signals, and next steps; score the opportunity with MEDDICC or BANT; flag risks and coaching points; and draft the follow-up. Use when the user shares a sales-call transcript/notes and wants a summary, qualification, deal risk assessment, next steps, a follow-up email, or call coaching."
metadata: {"flowly":{"emoji":"📞","tags":["business","sales","meddicc","bant","discovery","crm","qualification","objections"],"requires":{"bins":[]},"category":"business","related_skills":["customer-research","competitor-analysis","pricing-strategy","product-requirements"]}}
---

# Sales Call Analysis — Turn a Transcript Into a Qualified Next Step

A sales call is full of signal that evaporates if not captured: the real pain behind the stated ask, who actually decides, the objection that wasn't fully answered, and the concrete next step. This skill reads a transcript like a sales coach + RevOps analyst — extracting what matters, **qualifying honestly** (including the bad news), and producing the follow-up. The bot can ingest transcripts directly, which makes this a natural fit.

## What this skill produces

**Chat-first.** Default: a structured call summary — pains/goals, objections, buying signals, qualification (MEDDICC/BANT), risks, and clear next steps — plus an optional drafted follow-up email. Honest assessment over optimism; a deal scored green that's actually red helps no one.

## When to use

- "Summarize this sales call / transcript."
- "Qualify this opportunity." / "MEDDICC / BANT on this deal."
- "What are the objections / risks / next steps?"
- "Is this deal real / how likely to close?"
- "Draft a follow-up email." / "Coach me on this call."

## What to extract

1. **Pain & goals** — the *business* problem and desired outcome, not just the feature asked for. Quantify if mentioned ("losing 10 h/week", "missed Q3 target"). The pain's severity and urgency predict close more than interest.
2. **Buying signals (positive & negative)** — timeline urgency, budget mentions, "how soon can we start", involving others = positive; "just exploring", "circle back next quarter", going dark on next steps = negative.
3. **Objections** — surfaced concerns (price, timing, incumbent, risk, missing feature) and crucially **whether each was actually resolved** or just deflected. Unresolved objections are deal-killers hiding in plain sight.
4. **Stakeholders** — who was on the call, their role, who the economic buyer / champion / blocker is. A deal with no identified decision-maker or champion is at risk.
5. **Competition / alternatives** — incumbent, other vendors, or status-quo ("we'd build it"). (→ `competitor-analysis`.)
6. **Next steps** — specific, owned, dated. "We'll be in touch" is not a next step; "demo with the CFO on Thursday" is. The single best leading indicator of a live deal is a concrete, mutually-agreed next step.

## Qualification frameworks

**MEDDICC** (complex/enterprise B2B):
- **M**etrics — the quantified value/ROI the customer cares about.
- **E**conomic buyer — who controls the budget (have you met them?).
- **D**ecision criteria — how they'll choose.
- **D**ecision process — the steps/timeline to a signature.
- **I**dentify pain — the compelling reason to act.
- **C**hampion — an internal advocate with influence.
- **C**ompetition — who/what else.

**BANT** (simpler/faster cycles): **B**udget, **A**uthority, **N**eed, **T**imeline.

Score each element green/yellow/red based on transcript evidence; the **gaps (yellows/reds) are the action items** for the next call. Don't fill in a field you have no evidence for — "unknown" is a finding (it tells you what to ask next).

## Chat output format

```
**Call analysis — Acme Corp** (discovery, 2026-06-08)

🎯 Pain: manual reporting eats ~12 h/wk; missed a board deadline (urgent).
👤 Stakeholders: Maria (VP Ops, champion ✅), CFO = economic buyer (not yet met ⚠️).
💬 Objections: price (partially handled — wants ROI proof); security review needed (open ❌).
📈 Signals: asked about onboarding timeline (+), wants to start "this quarter" (+).
🥊 Competition: evaluating Competitor B + status quo (spreadsheets).

MEDDICC: M ✅ (12h/wk) · E ⚠️ (CFO unmet) · D-criteria 🟡 · D-process ❌ (unknown) ·
         I ✅ · Champion ✅ (Maria) · Competition 🟡 (B)
Verdict: real pain + champion, but unmet economic buyer + unknown process = mid risk.

▶️ Next steps: (1) send ROI one-pager (you, by Wed) (2) Maria to intro CFO
   (3) book security-review call. Draft follow-up email? (say the word)
```

## Workflow

1. **Read the transcript** for pain/goals, signals, objections, stakeholders, competition, next steps.
2. **Qualify** with MEDDICC (complex) or BANT (simple); score each element with evidence; mark unknowns.
3. **Assess deal risk honestly** — the reds/yellows are the gaps to close.
4. **Define concrete next steps** (owned, dated) and optionally **draft the follow-up email** (recap pain, value, agreed next step).
5. **Add coaching** if asked (missed discovery questions, objections left hanging, talk/listen ratio).
6. **Deliver** summary + qualification + next steps; route competitive intel to `competitor-analysis`, pricing pushback to `pricing-strategy`, feature requests to `product-requirements`, broader user insight to `customer-research`.

## Key pitfalls

- **Happy-ears optimism.** Reading interest as intent. Score on evidence; surface the reds. A friendly call with no next step is not a deal.
- **Mistaking the stated ask for the real pain.** Dig to the business problem behind the feature request.
- **Objections marked "handled" when only deflected.** Track resolution explicitly; unresolved objections kill deals late.
- **No economic buyer / champion identified.** A deal without a budget-holder and an internal advocate is fragile — flag it.
- **Vague next steps.** "Follow up" isn't one; require specific, owned, dated actions.
- **Filling in qualification fields without evidence.** "Unknown" is the honest, actionable answer — it becomes the next question.
- **Ignoring the status-quo competitor.** "Do nothing / build it ourselves" is often the real rival.

## Quick reference

- Extract: pain+goals (quantified), buying signals (+/−), objections (resolved?), stakeholders (economic buyer? champion?), competition, next steps (owned+dated).
- Qualify: MEDDICC (enterprise) or BANT (simple); green/yellow/red on evidence; unknowns = next questions.
- Best leading indicator = a concrete mutually-agreed next step. Biggest risks = unmet economic buyer, unresolved objection, no champion.
- Output = summary + qualification + risk + next steps (+ optional follow-up email). Be honest, not optimistic.
