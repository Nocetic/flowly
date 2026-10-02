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

**P2 — Say what happened, never what it contains, for requests to act.**
- Requests to act or answer never carry their payload:
  - approvals: "Approval needed · Flowly wants to run a command. Open Flowly
    to review it." (an action: "…to take an action…");
  - questions: "Question from Flowly · Flowly needs your answer to continue.
    Open Flowly to reply.";
  - plan reviews: "Plan ready for review · Flowly has a plan waiting for your
    approval. Open Flowly to review it."
- Results the user's own agent produced (chat replies, scheduled results,
  Board outcomes, Flowlets) keep their short preview, as chat already does;
  encrypted chats stay generic.

**P3 — Requests that can be answered elsewhere wait.**
- Anything that pauses the agent for the user (an approval, a question, a
  plan review) is pushed only if it is still waiting 60 s after it was asked;
  an answer on any surface cancels it. Keys: `approval:<id>`, `clarify:<id>`,
  `plan:<approval id>`. At the computer or in a call the
  phone stays silent; away, the phone takes over.

**P4 — Notify where the user is (Phase 2).**
- Desktop reports user presence (recent input) to the agents it is connected
  to; while present, informational notifications are not pushed. Relay chat
  replies already follow this per conversation.

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
2. **Presence: P4.** Desktop + Core.
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
