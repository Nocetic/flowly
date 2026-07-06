---
name: exposure-cleanup
description: Consent-gated workflow for reducing a person's exposure on data brokers and people-search sites. Use when the user asks to remove, opt out, delete, or monitor exposed personal information; clean up doxxing risk; generate opt-out, CCPA, GDPR, or generic removal drafts; track broker removal status; or create a human-task digest. Includes a local-only Python helper for subject intake, broker planning, state ledger, evidence tracking, and email drafts; it does not send email, read inboxes, bypass CAPTCHAs, or act without recorded consent.
---

# Exposure Cleanup

Reduce a consenting person's exposure on data-broker and people-search sites while keeping the process auditable and conservative. The helper script is deliberately local-only: it stores case state, creates plans, renders drafts, and records outcomes, but the agent must use Flowly/browser tools for live site checks and the operator must explicitly confirm any submission.

This is not legal advice.

## Safety Contract

- Require recorded consent before planning, scanning, drafting, or recording actions for a subject.
- Submit nothing until a listing is confirmed as the subject, not a namesake or relative.
- Disclose only fields listed by the broker plan. Never volunteer SSN, full government ID numbers, unrelated third-party data, credentials, or account secrets.
- Do not bypass CAPTCHAs, anti-bot walls, slider challenges, login walls, or device fingerprinting. Queue a human task instead.
- Draft emails only. Do not send email from the helper, scrape inboxes, store mail credentials, or read `.env` for credentials.
- Mark `confirmed_removed` only after a later re-scan verifies that the listing is gone.

## Quick Start

From this skill directory:

```bash
python3 scripts/exposure_cleanup.py doctor
python3 scripts/exposure_cleanup.py create-subject --full-name "Jane Public" --email jane@example.com --city Oakland --state CA --residency US-CA --consent
python3 scripts/exposure_cleanup.py plan <subject_id> --priority crucial
python3 scripts/exposure_cleanup.py record <subject_id> spokeo found --found true --evidence-json '{"listing_urls":["https://example.com/profile"]}'
python3 scripts/exposure_cleanup.py draft <subject_id> spokeo --kind auto --listing "https://example.com/profile"
python3 scripts/exposure_cleanup.py status <subject_id>
python3 scripts/exposure_cleanup.py tasks <subject_id>
```

State is stored under `${EXPOSURE_CLEANUP_DIR}` when set, otherwise `${FLOWLY_HOME:-$HOME/.flowly}/exposure-cleanup/`. Dossiers, ledgers, drafts, and audit logs are written with restrictive file permissions where the OS supports them.

## Workflow

1. Run `doctor` to confirm the data directory and starter broker catalog.
2. Create a subject with `create-subject --consent`. Collect names, aliases, emails, phones, current city/state, and prior locations in one pass. Ask for date of birth only when a broker plan shows it is required.
3. Run `plan <subject_id>`. Scan read-only first across all search vectors and priority brokers.
4. Record each broker outcome with evidence: `found`, `not_found`, `indirect_exposure`, or `blocked`.
5. Work parent clusters before child sites. If a parent removal can cover children, re-scan the children before filing duplicate requests.
6. Generate a draft only after a verified match: `draft <subject_id> <broker> --kind auto --listing <url>`.
7. After explicit operator confirmation and submission, record `submitted`, `verification_pending`, or `awaiting_processing` with disclosed field names.
8. Re-scan after `next_recheck_at`; record `confirmed_removed` only when the listing is actually gone.
9. Present `tasks <subject_id>` once for all CAPTCHA, phone, ID, fax, mail, account, or blocked-site work.

## Live Site Checks

Treat `references/brokers.json` as a starter catalog, not truth. Broker flows change frequently. Verify current URLs, emails, required fields, and processing windows before acting.

When scanning:

- Prefer official search or opt-out pages over broad web search.
- A 404, bot wall, empty shell, or query-echo page is inconclusive, not `not_found`.
- Record `found` only on a real listing corroborated by address, phone, email, age/DOB range, or another strong match.
- Record `indirect_exposure` when the subject's identifiers appear on someone else's record. Use `draft --kind indirect`; do not opt out the third party's record.
- Record `blocked` when the site demands CAPTCHA/anti-bot work the agent cannot complete normally.

## Legal Basis

Use `draft --kind auto` by default:

- `US-CA` residency renders a CCPA/CPRA draft.
- `EU`, `UK`, or `GB` residency renders a GDPR/UK-GDPR erasure draft.
- Other residency renders a generic removal/suppression request.

The helper refuses explicit `--kind ccpa` or `--kind gdpr` when the subject residency does not support that claim. Use `generic` instead.

## References

- Read `references/workflow.md` for the detailed operating model and safety rules.
- Read `references/state-machine.md` before debugging invalid `record` transitions.
- Read `references/templates.md` when choosing draft kinds or recording disclosure fields.
- Edit `references/brokers.json` only when you have current, verified broker mechanics.

## Verification

Validate the skill after edits:

```bash
python3 /Users/hakanoren/.codex/skills/.system/skill-creator/scripts/quick_validate.py flowly/skills/exposure-cleanup
python3 -m py_compile flowly/skills/exposure-cleanup/scripts/exposure_cleanup.py
python3 flowly/skills/exposure-cleanup/scripts/exposure_cleanup.py doctor
```

In a sandbox that cannot write to `$HOME/.flowly`, prefix runtime checks with
`EXPOSURE_CLEANUP_DIR=/private/tmp/exposure-cleanup-test`.
