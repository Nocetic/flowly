---
name: apple-reminders
description: "Apple Reminders via the remindctl CLI on macOS — add, list, complete, manage lists."
homepage: https://github.com/steipete/remindctl
metadata: {"flowly":{"emoji":"✅","platforms":["macos"],"tags":["Reminders","tasks","todo","macOS","Apple"],"requires":{"bins":["remindctl"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/remindctl","tap":"steipete/tap","bins":["remindctl"],"label":"Install remindctl (brew)"}],"related_skills":["apple-notes","cron"]}}
---

# Apple Reminders

This skill wraps the `remindctl` command-line tool, letting the agent read and
write the user's Apple Reminders from a shell. Because Reminders is backed by
iCloud, every item added or completed here shows up on the user's iPhone, iPad,
and other Macs.

## Setup checklist

1. macOS with the built-in Reminders app present.
2. Install the binary: `brew install steipete/tap/remindctl`.
3. Reminders is permission-gated. Check whether access is already granted with
   `remindctl status`; if not, request it with `remindctl authorize` and have the
   user approve the prompt.

## Pick this skill when

- The user literally talks about a "reminder" or the Reminders app.
- They want a personal to-do — often with a due date — that lands on their phone.
- They need a Reminders list created, viewed, or cleaned up.

## Reach for something else when

- The job is really an agent-side timed trigger → use the `cron` tool.
- It is a calendar appointment → that belongs in Apple or Google Calendar.
- It is structured project work → GitHub Issues, Linear, Notion, etc.
- The phrase "remind me" is ambiguous between a synced Reminder and an agent
  alert → ask which one before acting.

## Command cookbook

Run everything below through the `exec` tool.

**Looking at reminders** — bare `remindctl` defaults to today; the rest are named
windows or an explicit date:

```bash
remindctl                    # Today's reminders
remindctl today              # Today
remindctl tomorrow           # Tomorrow
remindctl week               # This week
remindctl overdue            # Past due
remindctl all                # Everything
remindctl 2026-01-04         # Specific date
```

**Working with lists:**

```bash
remindctl list                       # List all lists
remindctl list Work                  # Show specific list
remindctl list Projects --create     # Create list
remindctl list Work --delete         # Delete list
```

**Adding reminders** — a bare string is the title; flags let you set the title,
target list, and due date explicitly:

```bash
remindctl add "Buy milk"
remindctl add --title "Call mom" --list Personal --due tomorrow
remindctl add --title "Meeting prep" --due "2026-02-15 09:00"
```

### Due date is not the same as the notification

`remindctl` exposes two distinct timestamps, and confusing them is a common
mistake:

- `--due` is the moment the task is *due*.
- `--alarm` is when the *notification* fires.

A timed `--due` often auto-creates an alarm at the same instant, but if the user
wants to be nudged ahead of time you must set `--alarm` yourself. For a task due
at 2:00 PM with a heads-up at 1:30 PM:

```bash
remindctl add --title "Hairdresser" --due "2026-05-15 14:00" --alarm "2026-05-15 13:30"
```

The same two flags work when amending an existing item by its ID:

```bash
remindctl edit 87354 --due "2026-05-15 14:00" --alarm "2026-05-15 13:30"
```

One gotcha: the Reminders interface may sort or display the item by its alarm
time, since that is when it pings the user — so do not assume the due date moved.
Confirm against the machine-readable output rather than the UI:

```bash
remindctl today --json
```

In that JSON, `dueDate` carries the real deadline and `alarmDate` carries the
notification time. (Under the hood the alarm capability is inherited from
EventKit's `EKCalendarItem`, which is why it works even though Apple's
`EKReminder` reference only documents reminder-specific fields.)

**Finishing or removing items** — IDs come from the listing commands:

```bash
remindctl complete 1 2 3          # Complete by ID
remindctl delete 4A83 --force     # Delete by ID
```

**Machine-readable output:**

```bash
remindctl today --json       # JSON for scripting
remindctl today --plain      # TSV format
remindctl today --quiet      # Counts only
```

## Accepted date strings

Both `--due` and the date-filter commands understand:

- Relative words: `today`, `tomorrow`, `yesterday`
- Calendar dates: `YYYY-MM-DD`
- Date plus time: `YYYY-MM-DD HH:mm`
- Full ISO 8601, e.g. `2026-01-04T12:34:56Z`

## Operating rules

1. On a vague "remind me", first settle whether they mean a synced Apple Reminder
   or an agent `cron` alert.
2. Read back the title and the due date to the user before committing a new item.
3. When you need to parse results in code, request `--json`.
