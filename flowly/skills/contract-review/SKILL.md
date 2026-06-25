---
name: contract-review
description: "Review a contract for risk — NDA, MSA, SaaS/subscription, employment, consulting, lease, and similar agreements. Summarize the key terms in plain English, flag red-flag clauses, spot missing protections, and produce prioritized negotiation points from the user's side of the deal. Includes a Python helper that scans pasted contract text for present/missing standard clauses and risky language. Use when the user shares an agreement and asks what to watch for, what's unusual, what to push back on, or wants a plain-English summary of legal terms."
metadata: {"flowly":{"emoji":"📝","tags":["legal","contracts","contract-review","nda","msa","saas","negotiation","red-flags","compliance"],"requires":{"bins":["python3"]},"category":"legal","related_skills":["privacy-review","policy-drafting","regulatory-research","ocr-and-documents"]}}
---

# Contract Review — Plain English, Red Flags, and What to Push Back On

A contract review turns dense legalese into three things the user actually needs: **what this says in plain English**, **what's dangerous or one-sided here**, and **what I'd negotiate**. The discipline is reading *from the user's side of the table* — the same clause is a red flag for the customer and a feature for the vendor; always know which seat the user is in.

> **Not legal advice.** This is an informational review to help the user spot issues and ask better questions — it is not a substitute for a qualified attorney, and nothing here creates an attorney–client relationship. Flag anything material with "have a lawyer confirm this," especially for high-value, regulated, or cross-border deals.

## What this skill produces

**Chat-first.** Default: a plain-English summary of the key terms, a prioritized red-flag list (severity-tagged), missing-protection callouts, and concrete negotiation asks — readable on a phone. Offer a full marked-up file (`.md`/`.docx`) for a clause-by-clause redline.

## When to use

- "Review this NDA / MSA / SaaS agreement / contract."
- "What should I watch out for in this?" / "Anything unusual or one-sided?"
- "Explain this contract in plain English." / "What does clause X mean?"
- "What should I negotiate / push back on?"
- "Is this NDA mutual?" / "Can they terminate for convenience?"

## Step 0 — Establish the frame (don't skip)

Before reading clauses, pin:
1. **Which side is the user on?** (customer/vendor, employer/employee, discloser/recipient, landlord/tenant.) This flips the meaning of nearly every clause.
2. **What kind of agreement** and what's at stake (value, duration, data, IP, exclusivity)?
3. **What does the user care about** — speed, protection, a specific risk? Calibrate depth accordingly.

If the contract is a scanned PDF/image, extract text first via the `ocr-and-documents` skill.

## The clauses that matter (read these first)

| Clause | What to check | Red flag (typical) |
|---|---|---|
| **Term & termination** | Length, renewal, termination for cause vs **for convenience**, notice period | Auto-renew with long notice; only the *other* side can exit for convenience |
| **Liability / limitation of liability** | Cap amount, carve-outs, mutual vs one-sided | No cap, or cap only protects the counterparty; uncapped indemnities |
| **Indemnification** | Who indemnifies whom, scope, caps | Broad, uncapped, one-way indemnity against the user |
| **IP ownership** | Who owns what (background vs created IP), license scope | User's pre-existing IP swept in; vendor owns user-created work |
| **Confidentiality** | Mutual vs one-way, duration, definition, residuals | One-way NDA when it should be mutual; perpetual obligations |
| **Payment** | Amount, timing, late fees, increases, auto-escalation | Unilateral price increases; onerous late penalties |
| **Warranties & disclaimers** | What's promised vs "AS IS" | Everything disclaimed; no service warranty |
| **Data & privacy** | Data use, security, breach notice, sub-processors | No breach-notice SLA; broad data-use rights → hand to `privacy-review` |
| **Governing law & disputes** | Jurisdiction, venue, arbitration, class-action waiver | Inconvenient/foreign venue; forced arbitration; fee-shifting against user |
| **Assignment** | Can it be assigned (e.g. on acquisition) without consent? | Counterparty can assign freely; user cannot |
| **Non-compete / non-solicit / exclusivity** | Scope, geography, duration | Overbroad restraints; exclusivity with no minimums |
| **SLA / service levels** | Uptime, remedies, credits | Vague or no remedy; credits as "sole remedy" |
| **Force majeure, modification, entire agreement** | Standard boilerplate | One-side-only modification rights |

## Spot the *missing* clauses (often the bigger risk)

What *isn't* there can hurt more than what is. Check for absent: liability cap, mutual confidentiality, breach-notification timeline, termination-for-convenience (if the user wants an exit), data-deletion-on-termination, source-code escrow (for critical software), insurance requirements, dispute-resolution clarity, IP indemnity. A silent contract defaults to the background law — which may not favor the user.

## Severity-tagging (so the user knows what matters)

- 🔴 **Deal-breaker** — uncapped liability against the user, IP grab, one-sided indemnity. Negotiate or walk.
- 🟠 **Push back** — one-sided but negotiable (auto-renew, venue, price increases).
- 🟡 **Note** — acceptable but worth knowing (standard boilerplate, minor asymmetry).
Lead with 🔴, don't bury it in a wall of 🟡.

## The helper

`scripts/clause_scan.py` scans pasted contract text and reports which standard clauses appear to be **present vs missing**, and flags **risky language patterns** (uncapped liability, sole discretion, perpetual, irrevocable, indemnify/hold harmless, auto-renew, etc.). It's a triage aid — a fast first pass that tells you where to read closely, **not** a substitute for actually reading the clauses.

```bash
python3 scripts/clause_scan.py contract.txt
python3 scripts/clause_scan.py contract.txt --side customer   # tailor flags to the user's side
cat contract.txt | python3 scripts/clause_scan.py -           # read from stdin
```
Stdlib only. Keyword/heuristic-based — verify every hit and miss by reading.

## Chat output format

```
**Contract review — SaaS MSA** (you = customer)

📋 In plain English: 2-yr term, auto-renews yearly (90-day notice), they
can raise prices annually, you're locked in, disputes in Delaware.

🔴 Deal-breakers:
1. Liability cap protects vendor only; your indemnity is uncapped (§9).
2. Vendor owns "feedback" and any custom work you pay for (§7).

🟠 Push back:
- Auto-renew + 90-day notice → ask for 30-day, or no auto-renew (§3).
- Unilateral annual price increase → cap at CPI or a fixed % (§4).
- Termination for convenience is vendor-only → make it mutual (§3).

🟡 Notes: standard confidentiality (mutual ✅), force majeure standard.
⚠️ Missing: no breach-notification SLA; no data-deletion-on-exit → see privacy-review.

Have a lawyer confirm §9 (indemnity) before signing — that's the big one.
```

## Workflow

1. **Frame it:** which side, what type, what's at stake (Step 0).
2. **OCR if needed** (`ocr-and-documents`), then run `clause_scan.py` for triage.
3. **Read the high-stakes clauses** (the table) from the user's side; verify every scan hit/miss.
4. **List missing protections.**
5. **Severity-tag** findings; draft **specific** negotiation asks (proposed language, not just "this is bad").
6. **Deliver** plain-English summary + red flags + asks; route data clauses to `privacy-review`, regulatory questions to `regulatory-research`; flag what needs a real lawyer.

## Key pitfalls

- **Reading from the wrong seat.** A clause's risk depends entirely on which side the user is on — establish it first.
- **Summarizing without judging.** The user can read the contract; they need *what's dangerous* and *what to do*.
- **Missing the silence.** Absent clauses (no liability cap, no breach SLA) are often the biggest risk.
- **Vague advice.** "Negotiate the indemnity" is weak; "ask for a mutual cap at 12 months' fees" is useful.
- **Burying the lede.** Lead with deal-breakers; don't drown them in boilerplate notes.
- **Over-trusting the scanner.** `clause_scan.py` is keyword triage — confirm by reading; it misses paraphrased and unusual drafting.
- **Practicing law.** Inform and flag; for material/regulated/cross-border terms, tell the user to get an attorney.

## Quick reference

- Always establish **which side** the user is on before judging any clause.
- Highest-risk clauses: **liability cap, indemnification, IP ownership, termination, data**.
- "For convenience" termination, "sole discretion", "uncapped", "perpetual", "irrevocable", "hold harmless" → read closely.
- Missing protections to check: liability cap, mutual NDA, breach-notice SLA, data deletion, exit rights, IP indemnity.
- Severity-tag (🔴/🟠/🟡) and pair each red flag with a concrete ask.
- Data terms → `privacy-review`; regulation/applicability → `regulatory-research`; redline file → `.docx`.
