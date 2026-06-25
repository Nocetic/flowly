---
name: policy-drafting
description: "Draft internal policies and governance documents — acceptable use (AUP), information security, data retention, code of conduct, remote work, BYOD, incident response, employee handbook sections, and similar. Produces clear, enforceable, appropriately-scoped policy text with the standard structure (purpose, scope, policy statements, roles, enforcement, review). Use when the user asks to write/draft a policy, an AUP, a handbook section, a security or governance policy, or to turn rules into a formal document."
metadata: {"flowly":{"emoji":"📜","tags":["legal","policy","governance","compliance","aup","security-policy","handbook","procedures"],"requires":{"bins":[]},"category":"legal","related_skills":["privacy-review","contract-review","regulatory-research","doc-coauthoring"]}}
---

# Policy Drafting — Clear Rules People Can Actually Follow

A good policy is short enough to be read, specific enough to be enforced, and scoped so it doesn't promise what the org can't deliver. The failure modes are predictable: vague aspirations nobody can act on, copy-pasted boilerplate that doesn't match how the org actually works, or rules so strict everyone quietly ignores them. Draft for the reader who has to comply and the manager who has to enforce.

> **Not legal advice.** Policies — especially employment, security, and compliance ones — can carry legal weight. This produces solid drafts to work from; have counsel/HR review before adoption, particularly across jurisdictions.

## What this skill produces

**Chat-first.** For a quick ask, draft the policy inline in clean markdown. For a full handbook or a formal document, produce a file (`.md`/`.docx`) and summarize inline. Either way, the text is ready to adapt — not a lorem-ipsum template.

## When to use

- "Draft an acceptable use policy / security policy / data-retention policy."
- "Write a remote-work / BYOD / code-of-conduct policy."
- "Add a section to our employee handbook on X."
- "Turn these rules into a formal policy document."
- "We need an incident-response / password / access-control policy."

## The standard policy structure

Every policy should have these sections (scale the depth to the policy's importance):

1. **Purpose** — *why* this policy exists, in one or two sentences. The problem it solves.
2. **Scope** — *who and what* it covers (employees, contractors, systems, locations, data types) and explicitly what it does **not** cover.
3. **Definitions** — only the terms that carry weight; don't pad. Define "Confidential Data", "Personal Device", etc.
4. **Policy statements** — the actual rules. Use **"must / must not / may / should"** consistently (RFC-2119 style):
   - **Must / must not** = mandatory.
   - **Should / should not** = strong recommendation, exceptions allowed with justification.
   - **May** = permitted/optional.
5. **Roles & responsibilities** — who owns it, who enforces, who approves exceptions, who users escalate to.
6. **Exceptions** — how to request one, who approves, that they're documented and time-bound.
7. **Enforcement / consequences** — what happens on violation (proportionate; tie to the disciplinary process, don't invent penalties).
8. **Related documents** — links to procedures, other policies, regulations.
9. **Review & version** — owner, effective date, review cadence (e.g. annual), version number, change log.

## Principles for good policy

- **Policy vs procedure.** A *policy* states the rule and the why ("All production access must use MFA"). A *procedure* is the step-by-step how ("To enable MFA: …"). Keep them separate — procedures change more often. Offer to write the matching procedure separately.
- **Enforceable, not aspirational.** "Employees should be secure" is unenforceable. "Passwords must be ≥12 characters and unique per system" is. Every statement should be testable.
- **Right altitude.** Don't bake volatile specifics (tool names, exact numbers that change) into a policy that's hard to amend — push those to a procedure or an appendix.
- **Scoped honestly.** Don't write rules the org has no way to monitor or enforce; an ignored policy is worse than none (it's evidence you knew and didn't act).
- **Plain language.** Write for the average employee, not a lawyer. Short sentences, active voice, examples for the tricky parts.
- **Consistent normative verbs.** Pick must/should/may and use them precisely throughout.
- **Match reality.** Reflect how the org actually operates; a borrowed template that contradicts practice creates risk.

## Common policies & their must-haves

| Policy | Don't forget |
|---|---|
| **Acceptable Use (AUP)** | Permitted/prohibited use, personal use limits, monitoring notice, BYOD link, consequences |
| **Information Security** | Access control, MFA, data classification, encryption, device security, reporting |
| **Data Retention** | Retention periods *by data type*, deletion process, legal-hold exception, backups |
| **Code of Conduct** | Expected behavior, harassment/discrimination, conflicts of interest, reporting channel |
| **Remote Work / BYOD** | Eligibility, security requirements, equipment, expenses, availability, data handling |
| **Incident Response** | Definition, severity levels, reporting path, roles, comms, post-mortem |
| **Password / Access** | Length/complexity, rotation stance, MFA, least privilege, deprovisioning |

## Chat output format

For an inline draft, deliver the structured policy directly. For a quick confirmation before a long draft:

```
Drafting: Acceptable Use Policy
Scope: all employees + contractors, all company systems & data
Tone: standard corporate, enforceable
Jurisdiction note: US-based — flag any multi-region staff for handbook variance

Confirm scope/tone or tell me what's different, then I'll draft the full policy.
```
Then produce the full document with all sections.

## Workflow

1. **Clarify:** which policy, the org's context (size, industry, jurisdiction), tone, and what's actually true today (don't invent practices).
2. **Pick the structure** above; tailor sections to the policy's weight.
3. **Draft the policy statements** with consistent must/should/may; keep procedures separate.
4. **Sanity-check enforceability** — can each rule be monitored and enforced? Is it scoped honestly?
5. **Add roles, exceptions, enforcement, review metadata.**
6. **Deliver** inline or as a file; flag where counsel/HR review is needed; route data specifics to `privacy-review`, regulation drivers to `regulatory-research`, and use `doc-coauthoring` for an iterative long document.

## Key pitfalls

- **Aspirational mush.** Unenforceable "should be professional" statements. Make every rule testable.
- **Policy/procedure soup.** Mixing the rule with the step-by-step makes both harder to maintain.
- **Boilerplate that lies.** A template describing controls the org doesn't have creates liability — match reality.
- **Inconsistent normative language.** Sloppy must/should/may makes obligations ambiguous.
- **No exceptions process.** Real orgs need documented, approved, time-bound exceptions; absence breeds shadow non-compliance.
- **Over-strict = ignored.** A policy everyone violates is a liability, not a control. Calibrate to what's actually followable.
- **Forgetting the lifecycle.** No owner, no review date, no version → a stale policy nobody trusts.
- **Skipping review.** For employment/security/regulated policies, say it needs HR/legal sign-off.

## Quick reference

- Sections: Purpose · Scope · Definitions · Policy statements · Roles · Exceptions · Enforcement · Related docs · Review/version.
- Normative verbs: **must/must not** (mandatory), **should/should not** (recommended), **may** (optional) — used consistently.
- Policy = the rule + why; Procedure = the how. Keep separate.
- Every statement must be **enforceable and monitorable**, or it's decoration.
- Always include an **owner, effective date, review cadence, and version**.
- Calibrate strictness to what people will actually follow.
