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

Type `@` in the message box and the members appear above it. Pick one and it
is added to what you are writing. You mention a bot by the **name you gave it** — `@Flowly`,
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

Everyone answers **at the same time**. The members you addressed each get your
message and each write their own reply, in parallel. They do not see each
other's answers while they are writing, so what you get back are independent
takes rather than a discussion.

That is what a group does today, and there is no setting to change it.

> [!NOTE]
> Flowly also has a **council** shape, where members answer one after another
> and can read what was said before their turn, bounded at 3 rounds and 10
> turns. It works, but no app offers it yet — so nothing you create in Desktop
> or on your phone will use it. It is here because you may see it named in the
> host's capabilities; do not go looking for a switch.

## While a turn is running

You can watch it happen. Each member shows what it is doing — thinking,
reaching for a tool, writing — and its answer appears as it is written rather
than all at once at the end. You can stop a turn at any point; an answer that
was stopped is marked as stopped, so a half answer never reads as a whole one.

### When a member needs you

A member can pause and ask you something before it carries on:

- **Approval** — it wants to run a command and is waiting for you to allow it.
  Whether it has to ask depends on what you set for that bot in
  [its permissions](/docs/features/bots#permissions).
- **A question** — it needs something only you know before it can continue.

The group marks itself as needing you, so you can tell at a glance which of
your groups is waiting on you and which is simply working. You answer in the
group; the member picks up where it left off.

### If Flowly stops mid-turn

A turn interrupted by a quit or a restart is not left looking like it is still
running. When Flowly comes back, that turn is recorded as interrupted and the
group is idle again — so you never watch a spinner for an answer that stopped
being written yesterday.

## Attachments

You can attach up to **10 files** to a message, each up to **25 MB**. Images
get a thumbnail so the transcript stays readable.

Attachments belong to the group. Each file is stored once and belongs to the
message you sent it with.

Flowly clears out old generated pictures over time, but it never touches
these. A file a message still points at stays for as long as that message
does.

## History

Nothing you say in a group is ever thrown away. But a group does not hand you
its entire past every time you open it, so there are two things to know about:

- **What opens with the group** — the most recent part of the conversation,
  up to **1,000 messages** and about 2 MB of text, and never fewer than 30
  messages however long they are.
- **Everything else** — the rest of the conversation, kept on disk. Older
  messages move out of the part that opens with the group; they are not
  deleted.

Scroll up and the older messages are fetched, **50** at a time by default and
at most **100**. So a long group still has all of its past — the limit above
only decides how much arrives without you asking for it.

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

You will see a cost in money too, but only when Flowly knows the price of the
models involved. If a group shows you tokens and no cost, nothing is wrong —
Flowly simply does not have a price for that model, and would rather show you
nothing than a made-up number.

> [!NOTE]
> Some of what a bot reads has been read before, and providers charge less for
> it. They report it mixed in with everything else; Flowly separates it, so a
> cheap turn does not look like an expensive one.

Per-member figures are kept for up to 24 members, which is more than a group
can hold — a member that left still shows what it spent while it was there.

## Taking the transcript with you

In Flowly Desktop, a group can be exported as Markdown from the three-dot menu
beside it. (There is no export on the phone yet.) The
export names each speaker the way you know them and keeps the time each
message was sent. It notes which tools were used and which files were
attached. And it says plainly when an answer was stopped before it finished,
so a half answer never reads as a whole one.

Attached files stay where they are — an export is a document, not a folder. If
the group is longer than the export could reach, the file says how many of how
many messages it contains rather than leaving you to count.

## Storage

Every group you have lives in one file on your machine, which the app and the
command line can both use at the same time without getting in each other's
way.

Space comes back on its own. A deleted group leaves a gap behind, and Flowly
tidies those up **at most once every 30 days** — and only when there is enough
to be worth doing. A file left behind by a crash, written but never attached
to a message, is cleaned up the next time Flowly starts.

You can see what your groups are using whenever you like, and Flowly tells you
once group files pass **500 MB**.

## Deleting

Deleting a group removes its conversation, its attachments, and the private
session each member kept for it. Nothing of it is left on any member.

Removing a **member** from a group is different: the group carries on without
it, and that member keeps nothing of the group.

> [!WARNING]
> A group needs two members. If you delete a **bot** that is in a group of two,
> the group goes with it — along with its transcript and its attachments —
> because a group of one is not a group. A group of three loses only that
> member and continues.

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
