# Exposure cleanup workflow

## Scope

Use this workflow only for a consenting subject: yourself, a family member,
client, or employee who explicitly authorized the cleanup. Do not use it as a
people-search workflow for third parties.

## Operating model

1. Create a subject dossier with `create-subject --consent`.
2. Run `plan` and scan all brokers read-only before submitting anything.
3. Record each broker as `found`, `not_found`, `indirect_exposure`, or `blocked`
   with evidence.
4. Work parent clusters before child sites.
5. Generate drafts with `draft`; submit only after explicit operator
   confirmation.
6. Record `submitted`, `verification_pending`, or `awaiting_processing`.
7. Re-scan after the processing window before `confirmed_removed`.
8. Present `tasks` once for CAPTCHA, phone, ID, fax, mail, or blocked-site work.

## Safety rules

- No consent, no action.
- Never bypass CAPTCHAs, anti-bot checks, slider challenges, login walls, or
  device fingerprinting.
- Never volunteer SSN, full government ID numbers, unrelated third-party data,
  or credentials.
- Do not send requests through this helper. It only drafts and records.
- Do not claim CCPA or GDPR unless the subject's residency supports that basis.
- Treat broker playbooks as perishable. Verify current forms, emails, and
  requirements before acting.

## Scan guidance

Start with the broker's official search or opt-out flow. If a constructed URL
404s or returns an empty shell, record neither `found` nor `not_found`; try the
on-site search or mark `blocked` with evidence. Before any removal request,
confirm the listing belongs to the subject, not a namesake or relative.

## Indirect exposure

`indirect_exposure` means the subject's name, phone, email, or address appears
inside a third party's listing. Do not submit a normal opt-out for the third
party's record. Draft a narrow request asking the broker to delete only the
subject's own identifiers from that associated record.

## Human tasks

Queue human work instead of interrupting repeatedly. Use `tasks` at the end of a
run to show all manual steps together: CAPTCHA, phone callback, ID demand, fax,
postal mail, account creation, or blocked site review from the operator's own
browser.
