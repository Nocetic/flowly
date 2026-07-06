# Exposure cleanup state machine

One case is one subject and one broker. `scripts/exposure_cleanup.py record`
validates every transition and appends an audit event.

## States

| State | Meaning |
|---|---|
| `new` | Case exists but no scan has started |
| `searching` | Scan in progress |
| `not_found` | No listing confirmed in this scan |
| `found` | A direct listing was confirmed and needs action |
| `indirect_exposure` | Subject's PII appears on someone else's record |
| `action_selected` | Removal path chosen but not submitted |
| `submitted` | Form or email request sent |
| `verification_pending` | Waiting for email/callback verification |
| `awaiting_processing` | Submitted and waiting for broker processing |
| `confirmed_removed` | A later re-scan verified the listing is gone |
| `reappeared` | Previously removed listing appeared again |
| `human_task_queued` | Human-only step is needed |
| `blocked` | Site blocked automation or mechanics changed |

## Transition rules

```text
new                  -> searching | found | not_found | indirect_exposure | blocked
searching            -> not_found | found | indirect_exposure | blocked
not_found            -> searching | found | indirect_exposure | blocked
found                -> action_selected | submitted | human_task_queued | indirect_exposure | blocked | not_found
indirect_exposure    -> submitted | human_task_queued | not_found | found | blocked
action_selected      -> submitted | human_task_queued | blocked
submitted            -> verification_pending | awaiting_processing | human_task_queued | blocked
verification_pending -> awaiting_processing | confirmed_removed | human_task_queued | blocked
awaiting_processing  -> confirmed_removed | human_task_queued | blocked
confirmed_removed    -> reappeared | confirmed_removed
reappeared           -> found | indirect_exposure
human_task_queued    -> found | indirect_exposure | action_selected | submitted | verification_pending | awaiting_processing | confirmed_removed | blocked
blocked              -> searching | found | not_found | indirect_exposure | action_selected | human_task_queued
```

Same-state transitions are allowed to refresh evidence or follow-up dates.

## Evidence standard

Record `found` only when the page has a real listing that matches the subject by
more than name alone. Strong match signals include current or prior address,
phone, email, age/DOB range, or a unique listing/profile URL. Treat query echoes,
SEO headings, empty search pages, and namesake lists as inconclusive.

Record `confirmed_removed` only after a later scan shows the listing gone. A
submission confirmation page means `submitted` or `awaiting_processing`, not
`confirmed_removed`.
