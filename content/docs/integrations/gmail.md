---
title: Gmail
eyebrow: Integrations
description: Search, paginate and read Gmail messages with explicit results and safe failure handling.
---

# Gmail query contract

The native `email` tool calls Gmail from the selected agent. This read-path
change does not change OAuth scopes, enroll a gateway, or route mail through
a new service. Existing send/reply approval remains mandatory. Only GET
requests are retried; a mail submission is never automatically repeated.

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
