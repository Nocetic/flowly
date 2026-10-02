# Notification policy — the Flowly ecosystem

Status: Phase 1 in progress (branch `feat/notification-policy`, Core + Relay).
Applies to every phone notification any Flowly component sends, not only
Live Voice.

## Goal

A notification reaches the user once, on the device where it is useful, says
plainly what happened without leaking what it is about, and never arrives for
something the user is already looking at.

## What exists today (2026-10-02)

| Notification | Sender | Path | When | Content | Tap on iOS |
| --- | --- | --- | --- | --- | --- |
| Chat reply | Relay `sendPushNotification` | account devices (`users/{uid}/devices`) | only if no live socket watches the conversation | reply text; "Yeni mesaj" if encrypted | opens the chat |
| Command / action approval | Core `approval_push` | anonymous registrations | always, at once (now in the background) | raw command, 140 chars | opens the app |
| Scheduled task result | Core gateway (`_notify_cron`) | anonymous | always | job name + first result line | opens the result |
| Named bot's scheduled task | Core `profile_cron_push` | anonymous | always | bot · job + body | opens the result |
| Board card finished | Core `board_push` | anonymous | always (filtered by `should_notify`) | `Board · title` + first line or error | opens the Board |
| Flowlet | Core `flowlet_push` (agent tool) | anonymous | always | agent-written title + body | opens the app |

Clarify questions (including MCP prompts), plan reviews and Live Voice task
notices did not push. Questions and plan reviews pause the agent like
approvals do, so they follow P3 since 2026-10-02 (see below); Live Voice
task notices still do not push.

## Problems found (with evidence)

1. **Duplicates.** Relay creates a new `pushId` on every `/api/push/register`,
   even for a token it already has. iOS and Android register again on every
   reinstall (their stored credentials are wiped) and hand the new id to every
   bot; bots keep the old ones. One phone showed the same approval five times;
   one bot held 43 registrations.
2. **Raw commands on the lock screen** and through Apple/Google push servers:
   a command can contain a secret, a path, or a recipient.
3. **Unconditional.** Every Core notification goes out even while the user is
   at the computer, in a voice call, or answering on the strip.
4. **Approvals were 20 s late** (fixed in Core `0869788f`: pushes no longer
   gate the decision).
5. **Not actionable on the phone yet.** Live Voice and Desktop-local approvals
   cannot be answered on today's mobile apps; iOS Live Voice is unmerged
   (`codex/live-voice-ios`) and has no approval card, Android has no Live Voice.
   Core already serves pending approvals in `voice.history` and accepts the
   owner's decision from any device, so this is client work only.

## Rules

**P1 — One notification per device per event.**
- Relay keeps one registration per device token: registering a token
  supersedes its older registrations, and a push to a superseded one answers
  404, which every agent version already treats as dead.
- Every notification carries a stable event key (`approval:<id>`,
  `cron:<job>:<run>`, `board:<card>:<outcome>`, `flowlet:<id>:<digest>`).
  Core sends an event at most once; APNs collapses repeats by
  `apns-collapse-id`, FCM by `collapseKey`.

**P2 — A notification reads as a message from the agent; nothing secret leaves.**
(Revised 2026-10-02: the first version hid all content and read as noise.)
- The title is the agent's name ("Flowly", or a named bot's display name).
  The body is what it is waiting for or what happened:
  - command approval: "Needs your OK to run: git push origin main";
  - action approval: "Needs your OK: Send email to ali@example.com" (the
    first line only; an email's preview stays in the app);
  - question: the question itself, plus its choices while they are few and
    short: "Move the meeting to 10? (Yes / No)";
  - plan review: "Plan ready for your OK: Move the blog to Next.js (5 steps)";
  - results (chat replies, scheduled results, Board outcomes, Flowlets):
    their short preview, as before.
- Every title and body goes through one pipeline (`flowly/push/display.py`),
  applied centrally in `notifications.deliver` whoever built the text:
  1. invisible and spoofing characters (zero-width, bidi overrides, control
     characters) are removed first, so a secret split by one is still
     recognised; a command holding any of them, or an unusual space, is not
     shown at all ("…a command with hidden characters…");
  2. credentials are redacted on the full text: the conversation redactor
     (keys with known prefixes, `Authorization`/`Bearer`, URL userinfo, JWTs,
     PEM blocks, `password=`-style settings) plus command shapes
     (`API_KEY=…`, `--token …`, `curl -u user:…`, `mysql -p…`,
     `sshpass -p`, webhook URLs, unterminated private keys, 32+ character
     mixed-case tokens);
  3. only then is the text put on one line (a command's line breaks become
     `↵`) and cut, so a cut never exposes the start of a secret.
- The owner can switch content off: `notifications.preview: "minimal"` in the
  agent's config sends only the agent's name and a fixed line per kind
  ("Has a question for you. Open Flowly to reply."). Unknown values read as
  `full`. The phone's own preview setting still applies on the lock screen.
- Relay applies the same redaction to what it sends (chat replies it builds,
  and anything an older agent sends), so the guarantee does not depend on
  the agent's version.

**P3 — Requests that can be answered elsewhere wait.**
- Anything that pauses the agent for the user (an approval, a question, a
  plan review) is pushed only if it is still waiting 60 s after it was asked;
  an answer on any surface cancels it. Keys: `approval:<id>`, `clarify:<id>`,
  `plan:<approval id>`. At the computer or in a call the
  phone stays silent; away, the phone takes over.

**P4 — Notify where the user is (Phase 2, 2026-10-03).**
- Flowly Desktop reports to every agent it is connected to (the local agent,
  which also sends named agents' scheduled results, and each direct
  gateway) every 30 s: whether its user is active at that computer (input in
  the last 2 minutes, screen not locked, not asleep) and which kinds it shows
  there itself, from its own notification settings (`chat`, `cron`, `board`,
  `flowlet`). Lock, sleep and quit report absence at once.
- `presence.report` (feature RPC, owner's own connection only, never through
  shared voice access) keeps each computer's report for its TTL (90 s from
  Desktop, bounded 15 to 300 s). While a fresh report covers a kind,
  `notifications.deliver` does not ring the phone for it; the event still
  counts as delivered, so it is not pushed later.
- Only informational kinds are held. Approvals, questions and plans keep P3.
- Everything fails toward ringing the phone: a stale report expires, a
  Desktop that has not reported its settings holds nothing, an older agent
  rejects the unknown method and pushes as before, and relay-only agents
  (whose scheduled results Desktop does not show) are not reported to.

**P5 — The notification leads to the thing (Phase 3, mobile).**
- Tapping an approval opens the conversation that asked (including Live Voice
  conversations); a settled request removes its notification; text is
  localized on the device.

## Architecture

- **Core `flowly/push/notifications.py`** is the only way Core pushes:
  `send(Notice)` and `schedule(Notice, delay)` / `cancel(key)`. A `Notice` has
  a kind, an event key, title, body and data. It runs in the background,
  never on a decision path; it de-duplicates event keys for ten minutes and
  adds the key to the payload as `eventKey`.
- **Senders** (approval, cron, profile cron, Board, Flowlet) build a `Notice`
  and call it; approvals, questions and plan reviews schedule on request and
  cancel on close (`approval_push.wire_waiting_pushes` for the latter two).
- **Relay** keeps the per-token registration invariant and maps `eventKey`
  to the collapse identifiers. No database schema change: registrations keep
  their fields; superseding deletes documents.

## Phases

1. **Server (now): P1, P2, P3.** Core + Relay. No app changes; old apps and
   old agents keep working (unknown `eventKey` is ignored by apps; old agents
   drop superseded registrations on 404).
2. **Presence: P4.** Desktop + Core (done 2026-10-03).
3. **Mobile: P5.** iOS (with Live Voice) and Android.

## Compatibility

- Old agents talking to the new Relay: they still post per `pushId`; the
  superseded ones get 404 and are dropped.
- New agents talking to an old Relay: `eventKey` rides in `data` and is
  ignored; dedupe of registrations waits for the Relay deploy.
- Apps: no change in Phase 1. The approval text is English, like today's
  notifications, until device-side localization (Phase 3).

## Test plan (Phase 1)

- Core: one push per event key; approval push cancelled when settled before
  60 s, sent after 60 s when not; text never contains the command; every
  sender goes through `notifications`.
- Relay: registering a token supersedes older registrations; a superseded
  `pushId` answers 404; collapse identifiers come from `eventKey`, bounded,
  and are absent when it is missing.
- Live: approve on the strip within 60 s → no phone notification; leave it
  → one notification; reinstall the app → still one.
