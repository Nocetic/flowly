---
title: Activity
eyebrow: Features
description: A calm list of the work your agent did — one task per piece of work, written in plain words by your agent's own model, with every step open to inspect. Conversation never lands here, and nothing leaves your machine.
group: Oversight
---

**Activity** is the list of the work your agent did for you. Not a chat log and
not a raw event stream: one row per **task**, titled in plain words, with the
outcome underneath. Open a task to see what it did step by step; open a step to
see exactly what the tool was given and what it returned.

It answers *"what has my agent been doing?"* at a glance — the research it ran
while you were away, the routine that fired at 8:00, the email it sent after you
approved it, the goal it is still working through.

> [!NOTE]
> Activity lives entirely on your machine, in your agent's own home. Every
> agent has its own Activity. Nothing is uploaded, and only someone who can
> open a conversation can see the work done in it.

## Where to find it

| Surface | Where |
| --- | --- |
| Desktop | **Activity** in the sidebar (pick the agent at the top right), and **Activity** in any agent's settings |
| iOS | **Workspace → Activity** for your main agent, and the **Activity** tab of any agent |
| Android | **Workspace → Activity**, and the **Activity** tab of any agent |

All three read the same journal from the agent itself, so a task looks the same
on every device and the unread dot clears everywhere once you have looked.

## Work, not conversation

Activity only records **work**. Saying hello, asking a quick question, or
thanking your agent is conversation: it stays in the chat and never appears in
Activity — not even its words are written down.

A turn becomes a task when:

- **a routine or a goal started it** — those are always tasks; or
- **your agent took a step that is work** — searched or read the web, opened or
  wrote a file, ran a command, used a connected app, generated an image or a
  video, sent a message, asked another agent.

Some tools are part of *talking*, not work, and don't make a task on their own:
recalling from its memory or your past conversations, asking you a question,
drafting a plan for your approval, reading its own skills. A turn that only did
those is still conversation.

A long piece of writing with no tools at all — a cover letter, a plan, a report
of 1,500 characters or more — is a **candidate**: your agent's model decides
whether it was real work. Messages that reach your agent through Telegram,
Slack and other channels follow exactly the same rules as the app.

Your agent's model also gets the last word in the other direction. When it
writes the summary it may judge that a turn which used a tool was really just
conversation — a single quick lookup to answer a passing question — and that
turn is left out. It never does this for a routine or a goal.

## One task, several turns

Real work often takes more than one message. Activity keeps it together:

- **Goals.** Every turn of a [standing goal](goals.md) belongs to one task from
  the start, however many turns it takes.
- **Follow-ups.** If you carry on recent work in the same conversation — *"now
  fix the second issue"*, *"look again"* — within 30 minutes of the earlier work
  ending, your agent's model judges whether the new turn continues it. If it
  does, the steps join the earlier task and its title and summary are rewritten
  to cover the whole thing. A new subject starts a new task.
- **Routines.** Each run of a [routine](cron.md) is its own task, named after
  the routine.

A task keeps its place in the list (where it started). While a follow-up is
still running it can briefly appear as its own row; it folds into the earlier
task a few seconds after it ends, once the summary is written.

## What a row shows

```
 ◌  “find me cheap flights to Rome for the 12th”        now
    • Searching for “Rome flights November”

 ◎  Compare flights to Rome                             14:02
    Found three fares under 200 euros
```

- **The icon.** Three calm drawings cover everything: a **globe in a
  magnifier** for research on the web, a **clock with a turning arrow** for a
  routine, and **four nodes on a chain** — a task's steps — for everything
  else. While a task runs, the chain's nodes fill one after another.
- **The title.** Written by your agent's own model once the task ends, in the
  language you spoke — never copied from your message. Until it arrives, your
  request stands in, quoted and in italics, so you always know which task is
  which.
- **The line under it.** The outcome once there is one. While the task runs,
  what it is doing right now — *Searching for …*, *Reading …*, *Running …* —
  with a small dot.
- **Status.** Done tasks stay quiet. Anything else is labelled: running,
  waiting for you, stopped, failed, blocked, interrupted.
- **The unread dot.** Tasks that need a look — failed, blocked, waiting on you,
  or cut short — carry a dot until you have opened Activity.

The list refreshes itself: every couple of seconds while something is running,
otherwise every ten seconds.

### Statuses

| Status | Meaning |
| --- | --- |
| Running | Your agent is working on it now. |
| Waiting | It is paused on you — an approval or a question in its conversation. |
| Done | It finished. |
| Stopped | You stopped it. |
| Failed | It ended with an error. |
| Blocked | Its last step failed right after an approval or question was refused or timed out. |
| Interrupted | Your agent stopped mid-task — it was quit, restarted or updated. The task stays in the list so you know it happened; a task's steps are written when it ends, so the steps it had taken are not kept. |

## Opening a task

A task opens with:

- its title, outcome and current status — live while it runs, with new steps
  appearing as they happen;
- a short **summary** in your agent's own voice;
- the **steps** it took, each with a one- or two-sentence note on what it found
  or did;
- the **approvals and questions** it raised, and how each was answered;
- when it started, how long it actually worked (time spent waiting on you is
  not counted), who started it (you, a routine, a goal, a channel), the model
  and the tokens it used;
- **Open conversation**, to jump to the chat where it happened.

## Opening a step

Open any step to see it in full — exactly as the tool panel in the chat shows
it:

- **what the tool was given** (the search query, the file, the command, the
  request), and
- **what it returned**, laid out the same way the chat lays it out: search hits
  as results, files as files, command output as output.

These details are not copied into Activity. They are read back from the
conversation itself when you open the step, so they are only as available as
the conversation is. A step from a conversation you have deleted, or recorded
by an older version, simply says the details are no longer kept.

## Privacy

- **Local.** The journal is plain files in your agent's home, one per month:
  `~/.flowly/activity/<YYYY-MM>.jsonl` for your main agent and
  `~/.flowly/profiles/<name>/activity/` for each other agent. Nothing is sent
  anywhere.
- **No conversation.** A turn that is only conversation writes nothing at all.
- **No arguments or results.** A step records its tool, a short target (the
  search query, a file's name, a website's host, a program's name — never its
  arguments, and never anything that looks like a key or token), whether it
  worked and how long it took. What a tool was given and returned stays in the
  conversation.
- **Same access as the chat.** A task, and each of its steps, can only be read
  by someone who may open the conversation it happened in.

## Summaries and their cost

Titles, outcomes, summaries and step notes are written by **your agent's own
model**, in one short request per task after it ends — never while it is
replying. The request carries a compact account of the task (what was asked,
each step and the start of its result, the start of the reply), never the whole
conversation. The judgements — *was this work?*, *does it continue earlier
work?* — ride in the same request at no extra cost. Conversation is never sent,
because it is never recorded.

You can switch summaries off. Tasks are still recorded; each turn that did work
is then its own task, its title is the routine's name or your request, and
borderline turns (long writing with no tools) are left out.

## Settings

In `~/.flowly/config.json` (or the agent's own profile):

```json
{
  "activity": {
    "summaries": true,
    "retentionDays": 90
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `summaries` | `true` | Let your agent's model write titles and summaries, and judge borderline turns and follow-ups. Read each time a summary is about to run, so it needs no restart. |
| `retentionDays` | `90` | How long the journal is kept. Whole months older than this are removed. `-1` keeps everything. |

## Activity and the audit log

The [audit log](audit-log.md) is the forensic record: every model call and tool
call, for debugging and review. Activity is the owner's view: only the work, put
together into tasks, in plain words. They are independent — turning one off
never affects the other.

## Related

- [Standing goals](goals.md)
- [Cron](cron.md)
- [Sandbox and approvals](../using-flowly/sandbox-and-approvals.md)
- [Audit log](audit-log.md)
