---
title: Bot groups
eyebrow: Features
description: Put two to six bots in one conversation. Who answers, how history is kept, what a turn costs, and how to take the transcript with you.
---

A **group** is one conversation with several bots in it. You write once; the
members you addressed answer. Each keeps its own memory and its own settings —
a group is a room they meet in, not a merger.

Groups are local. The conversation lives in a database on your machine, and
each member keeps its own private session for the group in its own directory.

## Creating a group

Pick a name and choose between **two and six** bots. A group needs at least
two members to be a group, and stops at six because six agents answering at
once is already a lot to read.

Members can be changed later. A member you remove keeps nothing of the group:
its position in the transcript is reset, so adding it back starts it clean.

## Who answers

This is the part worth understanding, because it decides what a group costs
and how loud it is.

| What you write | Who answers |
|---|---|
| An ordinary message | Every member set to **Always replies** |
| `@friday take a look` | Friday, whatever its setting |
| `@everyone stand-up` | Everyone, whatever their setting |

### Reply settings

Each member is either:

- **Always replies** — answers every message in the group. This is the default,
  and what every group did before the setting existed.
- **Only when called** — stays quiet until you `@mention` it.

You set this on the member row when you create or edit a group, on Desktop and
on iOS alike.

The setting exists because the only way to stop a bot from replying to
everything used to be removing it from the group — which also removed what it
could see. A member on call is still present and still reading; it just waits
to be asked.

> [!NOTE]
> `@everyone` reaches members set to **Only when called**. A member who only
> answers when called has, at that moment, been called.

### A group where nobody answers

If every member is set to **Only when called** and you write without
mentioning anyone, nothing runs. The message is still recorded, and whoever
you call next reads it as context — but no reply is coming.

That is a real way to use a group: a set of specialists you summon one at a
time. Both the Desktop dialog and the iOS editor say so while you are building
it, so the silence is never a surprise.

### Bots calling bots

A bot can mention another bot in its answer. The mentioned member then joins
the turn — including one set to **Only when called**, because being called is
being called.

### Writing a mention

Type `@` and the members appear above the composer; pick one and it is
inserted. You mention a bot by the **name you gave it** — `@Flowly`,
`@Jarvis` — not by the identifier it has on disk. The translation happens when
the message is sent, so what you wrote and what you read back are the same
words.

A mention behaves as one thing rather than a run of letters. One backspace at
the end of `@Jarvis` removes the whole mention, `@` included, instead of
walking back through `@Jarvi`, `@Jarv`, `@Jar` — each of which would look like
somebody halfway through choosing a different bot.

Mentions inside code — `` `@jarvis` `` or a fenced block — are left alone. An
`@handle` in a command is data, not an address. The same is true of an email
address: `mail@jarvis` addresses nobody.

## How members answer

Groups run in one of two shapes:

- **Panel** — every selected member answers the same message at the same time,
  in parallel. They do not see each other's answers for that turn.
- **Council** — members answer in sequence and can see what was contributed
  before their turn. Bounded at **3 rounds** and **10 turns** in total, so a
  discussion cannot run away.

## Attachments

You can attach up to **10 files** to a message, each up to **25 MB**. Images
get a thumbnail so the transcript stays readable.

Attachments belong to the group. They are stored once, referenced by the
message that carries them, and are never swept away by the ordinary media
cleanup that ages out generated pictures — a file a message still points at
stays as long as the message does.

## History

A group keeps two things, and the difference matters:

- **The live window** — what the group carries in memory and hands to a client
  in one payload. Bounded at **1,000 messages**, and further bounded by size:
  about 2 MB of text, never fewer than 30 messages.
- **Durable history** — everything the group has ever said. Not bounded by the
  window. Messages that leave the window are marked as trimmed, not deleted.

Scrolling back fetches pages of **50 messages** by default, up to **100** per
page. So a group that has outgrown its window still has all of its past; the
window only decides what arrives without asking.

Each member is given at most **40 messages** of context per turn, which is
what keeps a long group from growing more expensive every time you write.

## What a turn costs

Every group counts what it spends. The meter records, for the group as a whole
and for each member separately:

- Input tokens
- Output tokens
- Cache-read tokens
- Cache-write tokens
- How many turns have run

Money appears alongside the tokens only when the model catalogue can price the
models involved. A group that reports tokens and no cost is not broken — it
means Flowly does not have a price for that model, and would rather show you
nothing than a number it guessed.

> [!NOTE]
> Providers report prompt tokens **including** cache reads. The meter splits
> them so a cached turn does not read as a full-price one.

Per-member figures are kept for up to 24 members, which is more than a group
can hold — a member that left still shows what it spent while it was there.

## Taking the transcript with you

A group can be exported as Markdown, from the three-dot menu beside it. The
export names each speaker the way you know them, keeps the time each message
was sent, notes which tools were used and which files were attached, and says
plainly when an answer was stopped before it finished.

The file bytes stay where they are: an export is a document, not a folder. If
the group is longer than the export could reach, the file says how many of how
many messages it contains rather than leaving you to count.

## Storage

Groups live in a single SQLite database under your Flowly home, written
ahead-of-log so Desktop and the CLI can both use it safely.

Space is given back on its own. Deleted groups leave pages behind; Flowly
reclaims them at most **once every 30 days**, and only when there is a
worthwhile amount to reclaim. Orphaned attachments — files stranded by a crash
between writing the file and committing the message — are swept once per
process start.

You can see what groups are using at any time. Flowly also raises a notice
once group media passes **500 MB**.

## Limits

| | |
|---|---|
| Groups per installation | 200 |
| Members per group | 2–6 |
| Live window | 1,000 messages |
| Context per member per turn | 40 messages |
| History page | 50 default, 100 maximum |
| Attachments per message | 10 |
| Attachment size | 25 MB |
| Council | 3 rounds, 10 turns |
| Member response timeout | 10 minutes |

## When something goes wrong

A group names its failures with a code, and the app translates that code into
your language rather than showing you an English sentence from a log. Busy,
stopped, not found, store limits and member failures each read as themselves.
The full list is in the [bot API reference](/docs/reference/bots-api).
