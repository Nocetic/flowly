---
name: ops-runbook
description: "Author operational documentation — standard operating procedures (SOPs), incident response playbooks, on-call runbooks, escalation matrices, severity definitions, checklists, and postmortems. Produces clear, executable-under-pressure docs. Use when the user wants an SOP, a runbook, an incident/on-call playbook, an escalation matrix, a checklist, or a postmortem template — or to document an operational process."
metadata: {"flowly":{"emoji":"📒","tags":["business","operations","runbook","sop","incident-response","on-call","postmortem","checklist"],"requires":{"bins":[]},"category":"business","related_skills":["product-requirements","kubernetes","systematic-debugging","policy-drafting"]}}
---

# Ops Runbooks — Docs That Work at 3 AM

Operational docs are written for their worst moment: a stressed on-call engineer, a new hire's first solo task, an incident in progress. So they must be **executable, not explanatory** — numbered steps with exact commands, clear decision points, and known escalation paths. A runbook that requires interpretation has already failed. Write for the tired, the new, and the panicking.

## What this skill produces

**Chat-first.** Default: the SOP / runbook / playbook / checklist / postmortem in clean, copy-pasteable markdown with numbered steps and exact commands. Offer a file for the team wiki. Optimize for scannability and zero ambiguity under pressure.

## When to use

- "Write an SOP / runbook for \<task\>."
- "Incident response playbook / on-call runbook for \<system\>."
- "Define severity levels / an escalation matrix."
- "Make a checklist for \<process\>." / "Postmortem template / write-up."
- "Document this operational process."

## SOP (standard operating procedure)

A repeatable routine task done the same way every time. Structure:
- **Purpose & when to use it** (the trigger).
- **Prerequisites** — access, tools, approvals needed *before* starting (so you don't get stuck mid-way).
- **Numbered steps** — one action each, with the **exact command/click** and the **expected result** ("you should see X"). No "configure the thing" — show how.
- **Verification** — how to confirm success.
- **Rollback / what-if-it-fails** — the undo and who to call.
- **Owner & last-reviewed date** — stale runbooks are dangerous; date them.

## Incident response playbook

For when something is broken. The flow:
1. **Detect & declare** — what triggers an incident; who declares; how (channel, page).
2. **Assess severity** (see matrix) — this drives everyone's response.
3. **Roles** — Incident Commander (coordinates, decides — not necessarily the most technical), Communications lead, Operations/fixers. One IC, clear roles, no chaos.
4. **Communicate** — status cadence to stakeholders/customers (e.g. update every 30 min even if "still investigating"); a single source of truth (status page / channel).
5. **Mitigate first, root-cause later** — stop the bleeding (rollback, failover, disable feature) before diagnosing. Recovery > understanding during the incident. (Diagnosis → `systematic-debugging`.)
6. **Resolve & verify**, then **postmortem**.

## Severity levels (define them concretely)

Make severity unambiguous so people don't argue mid-incident:

| Sev | Meaning | Response |
|---|---|---|
| **SEV1** | Critical: major outage / data loss / security breach; many users | all-hands, page immediately, exec comms, 24/7 |
| **SEV2** | Significant: key feature down / degraded for many | urgent, page on-call, fix in business+ hours |
| **SEV3** | Minor: limited impact, workaround exists | normal queue, next business day |

Tie each sev to **who gets paged**, **response-time targets (SLA)**, and **comms requirements**.

## Escalation matrix

Who to contact, in what order, when. Make it explicit: primary on-call → secondary → team lead → manager → vendor/exec, with **time thresholds** ("if not acked in 15 min, escalate to secondary"). Include contact methods and the rule that **escalating early is encouraged, not a failure**.

## Postmortem (blameless)

After resolution, learn without blame (blame hides truth; you want honest analysis):
- **Summary & impact** (what, how long, who/how many affected, $ if known).
- **Timeline** — detection → mitigation → resolution, with timestamps.
- **Root cause(s)** — the technical and the *contributing* causes (the 5 whys; usually systemic, not "human error").
- **What went well / poorly** — including detection & response, not just the bug.
- **Action items** — concrete, **owned, dated**, prioritized, tracked to completion (a postmortem with no followed-up actions is theater).

## Checklists

For error-prone or high-stakes routines (deploys, releases, onboarding, audits). Atomic, binary items ("✅ DB backup verified"), ordered, with no skippable ambiguity. Separate "do-confirm" (do then verify) from "read-do" (read each then act) as appropriate.

## Chat output format

````
**Runbook: Restart the payments worker** (owner: Payments, rev 2026-06)

When: payment jobs backing up (queue depth > 1000 for >5 min).
Prereqs: kubectl access to prod, PagerDuty ack.

1. Confirm the symptom:
   `kubectl get pods -n payments` — look for CrashLoop/age. Expected: pods Running.
2. Check the queue: `<dashboard link>` — note queue depth.
3. Rolling restart: `kubectl rollout restart deploy/pay-worker -n payments`
   Expected: new pods Ready within ~60s (`kubectl rollout status ...`).
4. Verify: queue depth dropping within 5 min.
If still failing → escalate to secondary on-call (15 min rule); consider SEV2.
Rollback: `kubectl rollout undo deploy/pay-worker -n payments`.
````

## Workflow

1. **Identify the doc type** (SOP / incident playbook / escalation / checklist / postmortem) and the audience (on-call? new hire?).
2. **Get the real steps/commands** — exact, with expected outputs; no hand-waving.
3. **Add the safety rails** — prereqs, verification, rollback, escalation with time thresholds.
4. **For incidents:** severity matrix + roles + comms cadence; mitigate-before-diagnose.
5. **Add owner + last-reviewed date**; keep it scannable.
6. **Deliver** copy-pasteable doc; route live debugging to `systematic-debugging`/`kubernetes`, policy/compliance wording to `policy-drafting`, feature specs to `product-requirements`.

## Key pitfalls

- **Explanatory, not executable.** Prose about how the system works ≠ steps to fix it. Give numbered actions + exact commands + expected results.
- **Missing prerequisites.** Discovering mid-incident you lack access wastes the worst minutes — list prereqs up front.
- **No rollback / failure branch.** Every risky step needs an undo and an escalation path.
- **Vague severity / escalation.** Arguing about sev or who to call during an incident costs time — define them concretely with time thresholds.
- **Diagnose-before-mitigate.** During an incident, stop the bleeding first; root-cause after.
- **Blameful postmortems.** Blame suppresses honesty and repeats the failure — keep it blameless and systemic.
- **Postmortem actions that die.** Owned, dated, tracked — or the incident recurs.
- **Stale docs.** Untested/undated runbooks fail when needed — date, review, and dry-run them.

## Quick reference

- Write for 3 AM: numbered, exact commands, expected results, scannable. Executable > explanatory.
- SOP: purpose/trigger → prereqs → steps(+expected) → verify → rollback → owner/date.
- Incident: detect/declare → severity → roles (IC/comms/ops) → communicate → **mitigate before diagnose** → resolve → postmortem.
- Severity tied to paging + SLA + comms; escalation matrix with time thresholds; escalate early.
- Postmortem: blameless, timeline, root cause (5 whys), owned+dated action items.
- Live debugging → systematic-debugging/kubernetes; policy wording → policy-drafting.
