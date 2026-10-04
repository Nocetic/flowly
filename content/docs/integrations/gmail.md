---
title: Gmail
eyebrow: Integrations
description: Connect Gmail, search and read messages, and manage mail with explicit approval.
---

# Gmail

The native `email` tool calls Gmail from the selected agent. Connect it from
Connections or ask in chat: the `google_connection` tool presents a review card
in Desktop. Choose Gmail management and optional Google Workspace services, then
approve the selected permissions on Google’s own page. Existing read/send grants
continue working; use “Extend Google access” for management permission.

Send/reply and management approval remain mandatory. Only GET requests are
retried; submissions and management writes are never automatically repeated.

## Managing messages

`trash` moves messages to Trash, and `untrash` restores them. Immediate permanent
deletion is not exposed. Other actions are `archive`, `move_to_inbox`, `mark_read`,
`mark_unread`, `star`, `unstar`, `label` and `unlabel`. Use `labels_list` to find
existing user labels, then pass their `label_ids` to label/unlabel.

Management requires `gmail.modify`. Pass one `message_id` or up to 100 explicit
`message_ids` from prior results; search queries and thread IDs are not accepted
as mutation targets. The approval lists the account, action, subjects, senders
and exact IDs. If any target cannot be read for the preview, no write starts.
After approval, the account and connection are checked again.

Results separate `succeeded`, `failed` and `not_attempted` IDs. A timeout or server
failure can mean `OUTCOME_UNKNOWN`: read that message’s state before retrying.
Do not replay successful IDs or assume a result page represents every match.

## Listing and searching

- `inbox`: optional `query`, always restricted to the INBOX label. The filter
  is independent of the query, so an OR expression cannot escape the inbox.
- `search`: required nonblank Gmail `query`. Supports quoted phrases,
  operators and OR groups without rewriting them.
- `max_results`: integer 1–100, default 5, per API page. This is not a total
  mailbox limit. Invalid values are rejected, not silently capped at 10.
- `page_token`: pass the previous `next_page_token`, with identical query and
  filters. `has_more` indicates another page. Do not invent continuation IDs.
- `include_spam_trash`: explicit boolean, default false.

Results are JSON text with `status`, `listed_count`, `returned_count`,
`total_estimate`, `next_page_token`, `has_more`, `errors` and `messages`.
The estimate is not an exact count. A page containing failed detail reads has
`status: partial`, even if zero messages could be hydrated. Each failed ID is
retained for an explicit `read` retry. Quota/account-wide failures stop queued
detail requests; already in-flight requests may finish.

Metadata includes sender, recipient, subject, date, Gmail received timestamp,
thread ID, labels, unread state and snippet. Missing headers are explicit;
they are not replaced with a fabricated subject or interpreted as no matches.

Use epoch seconds for precise local calendar-date boundaries. Gmail date
literals use PST. API searches are message-level and do not perform the Gmail
web UI's automatic account-alias expansion. Do not promise identical results
for a thread-wide UI search.

## Reading

`read` requires `message_id`. `body_limit` defaults to 12000 characters and
accepts 1–30000; `body_offset` defaults to 0. Follow `next_body_offset` until
null for a long message. `body_length` describes the decoded text and
`body_truncated` makes the chunk boundary explicit.

The MIME reader prefers plain text within an alternative, handles nested
multipart and root HTML, honors declared charsets, and fetches separately
stored text bodies. File attachments are listed, not automatically downloaded.
HTML is converted to text without opening URLs, executing code, or loading
remote images; safe link destinations and table boundaries remain readable.
Message content remains untrusted, not instructions for the agent.

Resource bounds: 5 concurrent metadata reads, up to 3 attempts per GET, 90s
overall read/list timeout, 16 MiB per response and 2 MiB decoded body. Oversize
or malformed content is reported as an error, not as an empty email. A long
Retry-After returns a retryable failure without keeping the turn asleep or
retrying before Google's deadline. Invalid authorization is reported clearly;
the existing credential layer retains responsibility for token renewal.

The agent's Gmail result budget is 64000 characters so ordinary 100-message
pages and body chunks survive the generic 8000-character tool limit. Very
large metadata can still use the existing tool-result spill mechanism.

## Verification

`tests/test_email_queries.py` uses synthetic HTTP responses and fake credentials.
It checks query fidelity, repeated header parameters, pagination, partial
failures, quotas/timeouts, concurrency/order, MIME/charset handling, body
continuation, response bounds, cancellation, and preservation of send approval.
No live Gmail messages are read, changed or sent by these tests.
