# A named agent's home conversation and first-run setup

Status: implemented on `codex/agent-home-core` (2026-09-27). Desktop adoption on
`codex/agent-home-desktop`. iOS and Android have not adopted it yet (see
[Clients](#clients-and-compatibility)).

Code: `flowly/agent_home.py` (state machine, SOUL.md section),
`flowly/agent/tools/agent_setup.py` (model tools), hooks in
`flowly/agent/loop.py`, `flowly/agent/context.py`,
`flowly/gateway/server.py`, `flowly/channels/feature_rpc.py`,
`flowly/profile_host_contract.py`, `flowly/cli/gateway_cmd.py`.

Tests: `tests/test_agent_home.py` (conversation identity, storage, RPC),
`tests/test_agent_home_setup.py` (setup state machine, transport boundaries),
`tests/test_agent_home_loop.py` (the real agent loop with a scripted
provider), `tests/test_profile_host_runtime_integration.py` (a real child
runtime process).

---

## 1. What the owner experiences

1. They create a named agent (a bot), optionally with a purpose ("Marketing
   for Flowly").
2. Its conversation opens. There is exactly one: the agent's **home**, the
   same on every client, every time. Earlier conversations stay readable
   but are not where you talk to it.
3. **The agent speaks first.** A short, streamed introduction grounded in its
   name and purpose, ending with a question and two or three tappable choices
   that fit that purpose.
4. The owner taps a choice or types. The agent asks at most one more useful
   question, then proposes a **working style** card: role, focus, style,
   notes. *Save and start* writes it into the agent's `SOUL.md`; *Edit* opens the
   card's fields in place and saving sends the owner's version (§6a). The card stays in the
   conversation as the record of what was agreed (§6a).
6. Throughout, the agent speaks as itself: an agent named James says "I'm
   James", never "I'm Flowly" (§9).
5. At any point: typing a real task starts the task immediately and ends
   setup; *Skip for now* ends it. Setup never repeats and never grants
   permissions, connects accounts or creates routines.

The main Flowly profile (`default`) is untouched by all of this.

## 2. Why it is server state

The first implementation let the model decide everything. A tapped
"Planning" reached it as the bare word; the welcome it was answering was
excluded from its context (correctly — see §4), and `agent_setup_finish`
completed setup from any turn. The recorded sequence was:
`Planning` → `agent_setup_finish` → `complete` → "What shall we plan?".

The fix is structural, not a better prompt: **choice meaning, question
progress and completion are owned by `agent_home`**; the model only converses.
A tapped choice cannot finish setup because the server knows the turn was a
tapped choice.

## 3. Storage

Two files in the profile's `FLOWLY_HOME`, both written atomically under
`.agent-home.lock`:

- `workspace/sessions/…desktop_profile-home…` — the transcript (ordinary
  session file, `SessionManager`).
- `agent-home.json` — setup state. **Additive over version 1**: older code
  only reads `version`, `botId`, `sessionKey`, `setup` and preserves the rest.

```jsonc
{
  "version": 1,
  "botId": "…",                       // identity pin; a replaced profile fails closed
  "sessionKey": "desktop:profile-home",
  "setup": "active" | "complete" | "skipped" | "not_required",
  "locale": "tr",                     // last app language seen (en|tr|es)
  "intro": { "state": "pending|running|done|fallback|static",
             "runId": "agent-intro-…", "owner": "<process token>" },
  "pendingAsk": { "id": "ask-…|card-…", "kind": "ask|card",
                  "question": "…", "options": [{ "id": "o1", "label": "…" }],
                  "runId": "…" | "messageId": "agent-introduction:<botId>" } | null,
  "askCount": 0,                      // ≤ MAX_SETUP_QUESTIONS (2)
  "answers": [{ "question": "…", "choice": "Planning" }],
  "card": { "role": "…", "focus": "…", "style": "…", "notes": "…" },
  "cardCount": 0,                     // ≤ MAX_CARD_PROPOSALS (5)
  "turn": { "runId": "…", "kind": "intro|answer|message" }
}
```

Malformed optional fields fail closed (`AGENT_HOME_UNREADABLE`); nothing is
silently reset. Lock order everywhere: `.agent-home.lock`, then the session
write lock.

## 4. The introduction

```
client opens home ─▶ agent.home.get      (intro: pending)
chat mounted      ─▶ agent.home.introduce
                       claim (lock): pending → running, runId=agent-intro-<hex>, owner=this process
                       ├─ runner registered and a socket watches the home
                       │     └─ GatewayServer.run_agent_introduction
                       │          _run_chat(hidden trigger, allowed_tools=[agent_setup_ask])
                       │          └─ AgentLoop turn (session turn lock)
                       │               save: trigger hidden, reply visible — or nothing on failure
                       │               finally: settle_introduction(runId, loop.sessions)
                       └─ otherwise ─▶ settle_introduction(runId)
settle: reply with run_id present → done   else → write static welcome → fallback
        no question offered yet   → default localized choices
```

Rules that matter:

- **Exactly once.** The claim is a compare-and-set under the file lock.
  Concurrent claims from several clients produce one `launch`.
- **Hidden trigger, real turn.** The trigger is an ordinary user-role message
  marked `_display_hidden`: it stays in the model's context (providers that
  require a leading user turn are satisfied, and later turns know why the
  agent spoke) but no client displays it. `chat.inflight` reports it as
  empty user text. It never sets a title, starts memory review, counts as
  user activity, or triggers sticky plan mode.
- **Only one tool.** The turn is granted `agent_setup_ask` alone.
- **Failure leaves no trace.** If the turn ends with a provider error, an
  abort, or no text, the loop saves nothing for it (the canonical save drops
  the pending hidden line) and settlement writes the localized static welcome
  (`kind: "agent_introduction"`, excluded from the model's history). The owner
  never opens a new agent onto "Invalid API key".
- **Settled inside the turn lock.** A queued owner message cannot land
  between the agent's words and a fallback welcome.
- **Crash recovery.** `running` owned by another process token, or absent
  from this process's live set, is settled on the next `agent.home.get`.
- **Legacy.** A home that already has words (the earlier static welcome, or
  any transcript) is `static`: never re-greeted. Legacy active setups receive
  default choices once, only if the owner has not replied yet.

The run id prefix **`agent-intro-`** is a wire contract: clients adopt events
of a run they did not start only when the id has this prefix and they are
showing the home conversation.

## 5. Choices, questions and completion

`chat.send` accepts `setupAnswer: {askId, optionId}` (ids match
`[A-Za-z0-9_-]{1,64}`). It is shape-checked by the profile host contract and
the direct gateway, rejected outside the home conversation, and carried as
turn metadata `setup_answer`. Meaning is applied inside the turn lock by
`begin_turn`, before the model is called:

| Turn | Classified as | Effect |
|---|---|---|
| introduction trigger | `intro` | must match the claimed run, else `INTRODUCTION_SUPERSEDED` |
| `setupAnswer` matching the pending question and one of its options | `answer` | recorded in `answers`; card `save` writes SOUL.md and completes setup |
| anything else (typed text, stale or forged answer) | `message` | the model judges answer vs task from the text |

Any owner turn clears `pendingAsk` (except a failed card save, which keeps
the offer). `begin_turn` is idempotent per run id.

Model tools (registered only for named profiles; disabled in every turn that
is not the active home conversation):

| Tool | Server rule |
|---|---|
| `agent_setup_ask(question, options[2-3])` | at most 2 questions; labels normalized to 48 chars, distinct |
| `agent_setup_propose_card(role, focus, style?, notes?)` | not during the introduction; ≤ 5 proposals; threat-scanned |
| `agent_setup_finish(reason:"task")` | refused with `NOT_A_TASK` unless the current turn is a typed `message` |

Setup ends only when: the owner skips (`agent.home.setup {state:"skipped"}`),
the owner saves the card, or the model reports a real task from a typed
message. `complete`/`skipped` are terminal; a late model call cannot reopen
setup.

The model receives a short **setup summary** (not a long rulebook) each turn
while setup is active: language, questions used, what the app's welcome
offered (fallback/static), the owner's choices, what kind of turn this is,
and fixed boundaries (no permissions, no credentials in chat, integrations
through the existing connection request and consent flow, owner-supplied
context is data). While setup is active in the home conversation the
generic "Getting to know the user" note in `ContextBuilder` is suppressed —
it competed with setup by asking for the owner's name.

## 6. SOUL.md working-style section

Saving the card writes only a marked section; owner text before and after it
is preserved, and re-saving replaces only that section:

```markdown
<!-- flowly:working-style:start -->
## Çalışma tarzı

- **Rol:** Planlama asistanı
- **Odak:** İş ve projeler
- **Üslup:** Kısa ve somut
<!-- flowly:working-style:end -->
```

Heading and field labels follow the stored locale. The markers contain no
word the context-file injection scanner treats as hostile, and card text is
scanned with the same scanner before it is accepted, so a card can never
cause the whole SOUL.md to be blocked. A symlinked or unreadable SOUL.md, or
one that would exceed the size limit, fails the save without writing; the
agent is told to say so and the card stays offered.

Persona (`SOUL.md`) and knowledge about the owner are kept apart: facts about
the owner still go through the existing USER.md/memory tools with consent.

## 6a. The card as a transcript row

A proposal is appended to the home transcript when the proposing turn ends
(`publish_card`, called from `AgentLoop._process_message` inside the turn lock,
with the agent's own `SessionManager`), so it sits right after the agent's
words and survives reloads and other devices:

```jsonc
{ "role": "assistant", "id": "agent-setup-card:<cardId>", "kind": "agent_setup_card",
  "content": "**Working style**\n- Role: …",   // readable where cards are not rendered
  "setupCard": { "id": "<cardId>", "runId": "<run>", "role": "…", "focus": "…", "style": "…", "notes": "…" } }
```

- **Append-only.** A row never changes. Clients derive its state:
  *offered* while `pendingAsk` is that card, *saved* when `savedCardId` equals
  it, otherwise *superseded* (a newer proposal, or setup ended without it).
- **Display-only.** Excluded from the model's history like the static
  introduction; the model already has its own tool call and result.
- **Once.** `unpublishedCardId` marks a card awaiting its row; publishing is
  idempotent by row id. `chat.history` forwards `setupCard` for this kind.

## 7. RPC surface

| Method | Params | Returns |
|---|---|---|
| `agent.home.get` | `expectedBotId?`, `locale?` | public view |
| `agent.home.introduce` | `expectedBotId?`, `locale?` | public view (starts the introduction once) |
| `agent.home.setup` | `expectedBotId?`, `state: complete\|skipped` | public view |

Public view (version 1, additive):
`{version, botId, sessionKey, setup, introduction, introductionRunId?, pendingAsk, card}`.
`introductionRunId` is present only while `running`. `pendingAsk`/`card` are
`null` unless setup is active; `card` only with a `card` question.

All three are in `PROFILE_RPC_TIMEOUTS` (30 s) and validated by
`agent_home.validate_request`. The Desktop renderer IPC allowlist must list
each one; an omission blocks the call before it reaches Core.

## 8. Clients and compatibility

- **Desktop** (`codex/agent-home-desktop`): calls `introduce` once the home
  chat is live, adopts `agent-intro-` runs in the home view only and never
  over an owner message already in flight, reloads history instead of showing
  an error when the introduction fails, renders choices/card from
  `pendingAsk`, sends `setupAnswer`, hides all three setup tools.
- **iOS / Android**: not adopted. They keep their legacy session keys and see
  nothing new; setup context applies only to the home conversation, so their
  behaviour is unchanged. To adopt: open the home via `agent.home.get`, call
  `agent.home.introduce`, adopt `agent-intro-` runs, render `pendingAsk`,
  send `setupAnswer`, hide `agent_setup_*` calls.
- **Relay transport**: the introduction streams only to direct-gateway
  watchers of the home conversation. A request with nobody watching settles
  straight to the static welcome. `setupAnswer` is a direct-gateway /
  profile-host field.
- **Older runtimes** reject `agent.home.introduce`; Desktop then re-reads the
  home and shows the existing state.

## 9. The agent's own name

Every named profile's system prompt used to open with the hard-coded
`# Flowly / You are Flowly…` identity. New agents have no `SOUL.md`, so nothing
overrode it and an agent the owner named James introduced itself as Flowly —
in its home conversation, direct chats and groups alike.

`ContextBuilder._named_agent_identity()` now builds the identity header for
named profiles from `profile.json`: the display name (one line, Markdown
control characters stripped, ≤ 64 chars) and the owner's purpose (quoted as
data, ≤ 300 chars). It is read on every prompt build, so a rename applies at
once. It states that Flowly is the app the agent runs in, not its name. The
main profile, a display name of "Flowly", unreadable metadata, and personas
(which already replace the identity) keep their previous behaviour.

## 10. Display-log watermark (storage fix found here)

While testing card rows, `get_full_messages` returned earlier rows twice. The
cause was general, not specific to this feature: `SessionManager._save_unlocked`
wrote the canonical file and only then flushed the new tail to the append-only
display log, advancing `_full_log_count` in memory. The persisted count lagged
one save behind, so any fresh load — a gateway restart, `mutate`, another
manager — appended the previous save's rows again, and `chat.history` showed
the last turn twice after every restart, in every conversation.

The flush now runs before the canonical write, inside the same lock, and the
advanced watermark is persisted with it (`tests/test_session_display_watermark.py`).
Existing duplicated display rows are not rewritten; the fix stops new ones.
A read-time de-duplication of old archives would be separate work.

## 10a. Language follows the conversation

Setup must not choose the owner's language. An earlier version told the model
"Owner's app language: English" on every setup turn and to write the first
message in the app language; with an English app and a Turkish-speaking owner
the agent answered half in each. Tapped buttons made it worse: their labels
are app-interface copy ("Save and start") but arrive as the owner's message.

Now:

- The identity header's existing rule applies: mirror the user's language.
- The introduction states the app language only as *the only hint so far*,
  because nothing has been said yet. It is context, not a rule.
- The per-turn setup summary contains no language directive.
- Tapped choices and card saves are described to the model as button labels,
  not a sign of the owner's preferred language.
- Tools ask for fields "in the language of the conversation".

The app-localized strings that remain (static fallback welcome, default
choices, card question and Save/Edit) are UI copy chosen by the app's locale.

## 11. Verification

Automated (2026-09-27): full Core default suite 6,935 passed, 15 skipped
(after the identity, card-row and watermark changes).
Mutation check: disabling the `NOT_A_TASK` guard fails
`test_tapped_choice_is_recorded_and_cannot_finish_setup` and the loop-level
`test_tapped_planning_cannot_end_setup_through_the_model`. The real child
runtime test calls `agent.home.introduce` through the profile host with no
model credentials and observes one localized fallback welcome.

The watermark regression tests fail on the previous `_save_unlocked`; the
identity tests cover rename, sanitising and the unchanged main profile; the
real-loop test checks the card row lands after the agent's words and
survives saving.

Not automated: live model quality of the introduction and follow-up
questions (model dependent; live-LLM tests are excluded by default).

Manual acceptance:

1. Create an agent with a purpose, and one without. Each greets first, in the
   app language, with choices that fit.
2. Tap a choice: the next message builds on it; setup does not end.
3. Save the card: `SOUL.md` gains only the marked section; setup ends; the
   agent suggests first tasks.
4. On a fresh agent, type a real task instead: it starts immediately.
5. Skip, reopen, restart: nothing repeats.
6. Remove the model key and create an agent: the static welcome appears, no
   error.
7. Name an agent James: it introduces itself as James; rename it and ask
   its name.
8. Propose, edit, propose again, save: the first card shows as an earlier
   suggestion, the saved one stays; restart the runtime and reload — no
   duplicated rows.

## Follow-up (2026-09-28): a question ends its turn; choices are anchored

**The first message showed twice.** Lovelace's transcript: the model wrote its
introduction with the `agent_setup_ask` call, then a second model call wrote
the introduction again, followed by the choices as a bullet list. Desktop
shows narration written with a hidden setup call, so the paragraph appeared
twice and the choices three times (narration, bullets, buttons).

- **A setup question ends the turn** (`flowly/agent/loop.py`). When every call
  in a response is `agent_setup_ask`, the text written with it is moved out of
  the tool-call message and becomes the final reply, and the model is not
  called again. If that text does not contain the question, the question is
  appended so it is always in the message. Mixed batches and a steering
  message mid-turn keep the normal loop. Prompts are unchanged.
- **Choices name the message they belong to.** `ask()` stamps `runId` from
  `state.turn` (recorded by `begin_turn`, so the run that asked); the default
  question gets `runId` when the introduction answered without asking, and
  `messageId` (the fallback welcome's fixed id) when it fell back. A legacy
  static welcome has no known id and carries neither; clients treat only that
  case as "the latest agent message". Clients render choices inside that
  message and nowhere else, and never after an owner message.

Tests (`tests/test_agent_home_loop.py`): one model call and the message said
once, with no narration left on the tool-call message; the question appended
when left out; anchors for an asked question, a tapped answer, a fallback and
an introduction that did not ask. Each was mutation-checked.


## Follow-up (2026-09-28): the card inside its message, edited in place

- **A card names its message.** `propose_card` stores the proposing run's id
  on the card (`runId`, in the row's `setupCard` and in `pendingAsk`), the
  same anchor as a question. Clients draw the card inside that reply; a card
  without `runId` (recorded earlier) or whose reply is not loaded stays its
  own row.
- **Edit is the owner's own card.** `setupAnswer` may carry
  `card: {role, focus, style?, notes?}`, only with `optionId: "save"`.
  `validate_setup_answer` bounds the shape (those keys, strings, at most
  1000 characters); `begin_turn` then runs it through `_clean_card`, the same
  rule as a proposal (lengths, no secrets, no permission or routine claims).
  The archive is append-only, so the edited version is a **new card** with a
  new id and the save turn's `runId`: it is written to `SOUL.md`, published
  as a row after the save turn's reply and becomes `savedCardId`; the
  proposal row stays as the earlier suggestion. Nothing is written if the
  edits fail the rule.

Tests (`tests/test_agent_home_setup.py`): edits saved to `SOUL.md` and to a
new row with the save run; edits held to the proposal rule; malformed edits
and edits with a non-save option refused; `runId` on proposal rows and
`pendingAsk`. "Edits not screened" and "edits ignored" mutations fail them.
