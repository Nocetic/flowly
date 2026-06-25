---
title: Heartbeat
eyebrow: Features
group: Automation
description: A recurring proactive check-in — Flowly reads your HEARTBEAT.md and acts on it within your active hours, without you asking.
---

## What the heartbeat is

The heartbeat is a recurring, proactive check-in. On a fixed interval — and only inside the hours you mark as active — Flowly wakes the agent, has it read a file called `HEARTBEAT.md` in your workspace, and acts on whatever you've written there. You don't have to send a message to trigger it.

Each tick is a fresh, isolated turn: the heartbeat's session history is cleared before every run, so an old check-in never bleeds into the next one.

If there's nothing actionable in `HEARTBEAT.md`, the heartbeat stays quiet. It only does work when the file gives it something to do.

> [!NOTE]
> The heartbeat is a **task poller**, not a health or liveness monitor. "Heartbeat" here means "Flowly periodically checks in on its to-do file" — not "ping to see if the service is alive."

## HEARTBEAT.md — your standing to-do file

The heartbeat reads a single file:

```text
<workspace>/HEARTBEAT.md
```

The default workspace is `~/.flowly/workspace`, so the file is usually at `~/.flowly/workspace/HEARTBEAT.md`. Create it if it doesn't exist — a missing file simply means "nothing to do."

Put standing instructions or recurring tasks in it, in plain Markdown. For example:

```markdown
# My standing tasks

- Check if any GitHub issue assigned to me was updated in the last hour.
- If my calendar has a meeting starting within 15 minutes, remind me.
```

On each tick the agent is told to read `HEARTBEAT.md` and follow any instructions or tasks listed there.

### What counts as "nothing to do"

Before waking the agent, the heartbeat checks whether the file actually has actionable content. A file is treated as **empty** (and the tick is skipped) when every line is one of:

- Blank
- A heading (starts with `#`)
- An HTML comment (starts with `<!--`)
- An empty or completed checkbox on its own line: `- [ ]`, `* [ ]`, `- [x]`, `* [x]`

The first line that isn't one of those makes the file actionable.

> [!TIP]
> Leave your headings and structure in place — they won't trigger a run on their own. The heartbeat only wakes the agent once there's a real instruction or a non-trivial line in the file.

## Active hours and interval

The heartbeat runs on a fixed interval (`everyMinutes`, default **30 minutes**). When the gateway starts, it fires one tick immediately, then continues on that interval.

If you set active hours, every tick first checks the current time against your window. Outside the window, the tick is skipped — no file read, no agent wake-up.

- `start` / `end` are `HH:MM` in 24-hour format (defaults `09:00`–`23:00`).
- `timezone` is an IANA name like `Europe/Istanbul`. Leave it empty to use the machine's local time.
- Overnight windows work: if `start` is later than `end` (for example `22:00`–`06:00`), the window wraps across midnight.

If you don't configure `activeHours` at all, the heartbeat runs around the clock on its interval.

> [!NOTE]
> The window is inclusive of both endpoints — `start` and `end` minutes both count as "active."

## Delivery

When a tick has actionable work, the agent runs it. What happens with the result depends on `deliver`:

- **`"none"`** (default) — the agent does the work but doesn't proactively message you. Use this for tasks that act silently (updating notes, housekeeping) where you don't want a chat ping every time.
- **`"message_tool"`** — the agent is instructed to send any result or update to you using its message tool. This is what you want for reminders and digests that should land in a channel.

When there's genuinely nothing to report, the agent replies with the internal token `HEARTBEAT_OK`, which Flowly suppresses — so you never get an empty "all clear" message.

> [!TIP]
> Delivering to a channel with `"message_tool"` needs a channel configured (for example Telegram or web), so the agent has somewhere to send the message.

## Configuration keys

All heartbeat settings live under `agents.defaults.heartbeat`. On disk the keys are camelCase:

```json
{
  "agents": {
    "defaults": {
      "heartbeat": {
        "enabled": true,
        "everyMinutes": 30,
        "activeHours": {
          "start": "09:00",
          "end": "23:00",
          "timezone": "Europe/Istanbul"
        },
        "deliver": "none"
      }
    }
  }
}
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Turn the heartbeat on or off. |
| `everyMinutes` | integer | `30` | Minutes between ticks. |
| `activeHours` | object or omitted | omitted | Window during which ticks run. Omit to run around the clock. |
| `activeHours.start` | string `HH:MM` | `"09:00"` | Start of the active window (24h). |
| `activeHours.end` | string `HH:MM` | `"23:00"` | End of the active window (24h). |
| `activeHours.timezone` | string (IANA) | `""` | Timezone for the window. Empty = machine local time. |
| `deliver` | string | `"none"` | `"none"` (act silently) or `"message_tool"` (message the result to a channel). |

## How this differs from Cron

Both the heartbeat and [cron](cron.md) let Flowly act without you asking, but they're different tools:

- **Cron** runs **explicit schedules**. Each cron job has its own schedule (a one-shot time, a fixed interval, or a 5-field cron expression) and its own prompt or tool call. You create many jobs, each fired at its own moment.
- **The heartbeat** is a single **recurring poll of one file**. There's just one interval and one `HEARTBEAT.md`. Instead of scheduling distinct jobs, you edit a standing to-do file, and the heartbeat checks it on every tick within your active hours.

Reach for cron when you want something to happen at a specific time or on a precise repeating schedule. Reach for the heartbeat when you want an always-on assistant that periodically looks at a list of standing tasks and acts on whatever currently applies.

## Related

- [Cron — scheduled tasks](cron.md)
- [Board](board.md)
