---
name: privacy-review
description: "Run a data-privacy review — map data flows and the PII inventory, identify the lawful basis, check consent, retention, minimization, security, breach-notification, sub-processors, cross-border transfers, and data-subject rights against a GDPR/CCPA-style checklist. Includes a Python helper that scans text/code/files for PII patterns and trackers to seed a data inventory. Use when the user asks about GDPR/CCPA compliance, a privacy review, what personal data an app collects, a DPA, consent, data retention, or a privacy-policy gap check."
metadata: {"flowly":{"emoji":"🔒","tags":["legal","privacy","gdpr","ccpa","data-protection","pii","compliance","dpa","consent","security"],"requires":{"bins":["python3"]},"category":"legal","related_skills":["contract-review","policy-drafting","regulatory-research","api-security-audit"]}}
---

# Privacy Review — What Data, Why, and Is It Defensible?

A privacy review answers: **what personal data flows through this system, what's the legal justification for each use, and where are the gaps?** It's data-flow archaeology plus a compliance checklist. The recurring failure isn't malice — it's collecting data nobody mapped, keeping it forever, and having no answer when a regulator (or a user) asks "why do you have this?"

> **Not legal advice.** This is an informational compliance review to surface gaps and questions — not a legal opinion, and not a substitute for a privacy counsel / DPO. Laws vary by jurisdiction and change; flag material/cross-border issues for a qualified professional.

## What this skill produces

**Chat-first.** Default: a data-inventory summary (what PII, where, why), a gap list against the relevant framework (severity-tagged), and concrete remediation steps. Offer a full file for a Record of Processing Activities (RoPA), a data-flow map, or a privacy-policy gap report.

## When to use

- "Is this GDPR / CCPA compliant?" / "Do a privacy review."
- "What personal data does this app/site collect?"
- "Do we need consent for X?" / "What's our lawful basis?"
- "How long can we keep this data?" / "What's our retention policy?"
- "Review this DPA / privacy policy." / "Are we ready for a data-subject request?"
- "We use \<analytics/ad SDK\> — is that a problem?"

## Step 1 — Map the data (you can't protect what you can't see)

For each category of personal data, capture the **lifecycle**:
- **What** (category + sensitivity — basic PII vs **special category**: health, biometric, religion, sexual orientation, etc.; children's data is its own regime).
- **Source** (collected from user, observed/tracked, derived, bought).
- **Purpose** (the specific reason — "marketing" is too vague; "send order confirmations" is right).
- **Where it lives** (DB, logs, analytics, third-party SaaS, backups).
- **Who can access** it (internal roles, sub-processors).
- **Retention** (how long, and the deletion mechanism).
- **Transfers** (does it leave the origin region/country?).

`scripts/pii_scan.py` seeds this by scanning code/text/files for PII patterns and embedded trackers — a starting inventory, not the whole map.

## Step 2 — The compliance checklist

| Area | The question | Common gap |
|---|---|---|
| **Lawful basis** (GDPR) | Which of the 6 bases for each purpose? (consent, contract, legal obligation, vital interest, public task, **legitimate interest**) | No basis identified; "consent" used where it isn't freely given |
| **Consent** | Freely given, specific, informed, **opt-in** (not pre-ticked), withdrawable as easily as given | Implied/bundled consent; no withdrawal path; cookie walls |
| **Data minimization** | Only what's necessary for the purpose | Collecting "just in case"; over-broad fields |
| **Purpose limitation** | Used only for the stated purpose | Repurposing data (e.g. support data → marketing) |
| **Retention** | Defined limits + actual deletion | "Keep forever"; no deletion job; backups never purged |
| **Transparency** | Clear privacy notice covering all of the above | Policy missing purposes/retention/rights/sub-processors |
| **Data-subject rights** | Access, deletion, portability, rectification, objection — with a process & timeline | No DSAR process; can't actually delete a user |
| **Security** | Encryption (rest/transit), access control, pseudonymization | Plaintext PII; PII in logs; broad internal access → see `api-security-audit` |
| **Breach notification** | Detection + ~72h (GDPR) regulator notice process | No incident process; no notification timeline |
| **Sub-processors / vendors** | DPAs in place; documented list; flow-down obligations | No DPA with a processor → see `contract-review` |
| **Cross-border transfers** | Mechanism for transfers out of region (SCCs, adequacy) | EU→US data with no transfer mechanism |
| **DPIA** | Done for high-risk processing? | Skipped for profiling/large-scale/sensitive data |
| **Children** | Age gating + parental consent where required (GDPR-K, COPPA) | Service used by minors with no controls |
| **CCPA/CPRA specifics** | "Do Not Sell/Share" link, opt-out of sale, notice at collection | No opt-out; "sale" via ad SDKs unaddressed |

## Step 3 — Frameworks (apply the ones that fit)

- **GDPR** (EU/EEA): broadest; lawful basis, DSR, DPO, DPIA, 72h breach, transfers. Extraterritorial — applies if you target/monitor EU residents.
- **CCPA/CPRA** (California): opt-out of sale/sharing, sensitive-PI limits, notice at collection. (Other US states: VCDPA, CPA, etc. — similar shape.)
- **HIPAA** (US health), **GLBA** (US finance), **COPPA** (US children), **PIPEDA** (Canada), **LGPD** (Brazil) — note when the data type triggers a sector law.
- Determine applicability by **where the users are and what data it is**, not where the company is. Route deep applicability questions to `regulatory-research`.

## The helper

`scripts/pii_scan.py` scans a file, directory, or stdin for likely PII (emails, phones, SSNs, credit-card-shaped numbers, IPs, names-in-fields, DOB) and common **third-party trackers/SDKs** (Google Analytics, Meta Pixel, ad networks, session recorders). Output is a starter inventory + flags.

```bash
python3 scripts/pii_scan.py ./src                 # scan a codebase
python3 scripts/pii_scan.py privacy_policy.txt
cat data_sample.json | python3 scripts/pii_scan.py -
```
Stdlib only. Heuristic — it finds candidates to investigate, not a definitive PII census; verify and expect false positives/negatives.

## Chat output format

```
**Privacy review — <app>** (frameworks: GDPR + CCPA)

📦 Data inventory (from scan + you):
- Email, name, IP, device ID (analytics), payment token (Stripe)
- Trackers found: Google Analytics, Meta Pixel ⚠️

🔴 Gaps:
1. Meta Pixel = data "sharing" under CPRA with no opt-out + no consent (EU). 
2. No retention limits — user data kept indefinitely, no deletion job.
3. PII (email) written to application logs (§ found in scan).

🟠 Address soon:
- Privacy notice missing sub-processor list + retention periods.
- No documented DSAR process (access/deletion) or 72h breach plan.

✅ OK: TLS in transit, payment tokenized (no raw card data).

Next: add consent management for trackers; define + enforce retention;
get DPAs with Stripe/Google/Meta (→ contract-review). Confirm EU transfer
basis with counsel.
```

## Workflow

1. **Scope:** which users (regions), what data, which frameworks apply.
2. **Map the data** — run `pii_scan.py` on code/policies/samples, then fill the lifecycle gaps with the user.
3. **Run the checklist** per data category and purpose.
4. **Severity-tag gaps** (🔴 regulator-risk / fines · 🟠 fix soon · 🟡 hygiene) and write concrete remediations.
5. **Deliver** inventory + gaps + steps; route DPAs/vendor terms to `contract-review`, the privacy *policy text* to `policy-drafting`, security depth to `api-security-audit`, applicability to `regulatory-research`; flag what needs counsel.

## Key pitfalls

- **Reviewing the policy, not the practice.** A great privacy policy over a system that does something else is the worst outcome — map what the system *actually* does.
- **Vague purposes.** "For business purposes" isn't a purpose; tie each data use to a specific reason and a lawful basis.
- **Consent where it doesn't apply (or is invalid).** Pre-ticked boxes, cookie walls, and bundled consent aren't valid; and not everything needs consent (legitimate interest/contract may fit).
- **Forgetting it must be deletable.** Rights mean nothing if you can't actually find and delete a user's data (including backups/logs).
- **Trackers as an afterthought.** Analytics/ad SDKs are often the biggest "sharing/sale" and consent exposure.
- **Over-trusting the scanner.** `pii_scan.py` is heuristic — verify; it misses semantic PII and free-text.
- **Giving legal opinions.** Surface gaps and questions; send material/cross-border calls to counsel.

## Quick reference

- Map every data category's full lifecycle: what · source · purpose · location · access · retention · transfer.
- GDPR lawful bases (need one per purpose): consent · contract · legal obligation · vital interest · public task · legitimate interest.
- Valid consent = freely given, specific, informed, unambiguous opt-in, easily withdrawn.
- Special-category & children's data = stricter rules; flag immediately.
- Core rights to enable: access, deletion, portability, rectification, objection (+ CCPA opt-out of sale/share).
- Breach notice (GDPR): ~72h to the regulator — needs a process before you need it.
- Applicability follows the **user's location + data type**, not the company's HQ.
- DPAs with all processors; transfer mechanism (SCCs/adequacy) for cross-border.
