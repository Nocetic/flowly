---
name: apple-notes
description: "Manage Apple Notes via the memo CLI on macOS — create, search, edit, move, export."
homepage: https://github.com/antoniorodr/memo
metadata: {"flowly":{"emoji":"📝","platforms":["macos"],"tags":["Notes","Apple","macOS","note-taking"],"requires":{"bins":["memo"]},"install":[{"id":"brew","kind":"brew","formula":"antoniorodr/memo/memo","tap":"antoniorodr/memo","bins":["memo"],"label":"Install memo (brew)"}],"related_skills":["apple-reminders","imessage"]}}
---

# Apple Notes

This skill drives the `memo` command-line tool so the agent can work with the
user's Apple Notes from a shell. Anything created or changed here propagates to
the user's iPhone, iPad, and other Macs through their iCloud account, so treat
every note as live, shared data.

## Setup checklist

Before running any command, confirm the environment is ready:

1. The machine is a Mac running the stock Notes app (`memo` talks to Notes via
   Apple automation — there is no cross-platform fallback).
2. The `memo` binary is installed. The Homebrew route is:
   `brew tap antoniorodr/memo && brew install antoniorodr/memo/memo`
3. The first invocation will trigger a macOS automation consent dialog. The user
   must allow the controlling process to drive Notes under
   System Settings → Privacy & Security → Automation. Until that is granted,
   commands silently fail or hang.

## Pick this skill when

- The request is specifically about Apple Notes — reading them, adding one,
  finding one, tidying folders, or pulling content out.
- The user wants something written down where it will show up on their phone.
- A note needs to be exported into a portable format (HTML or Markdown).

## Reach for something else when

- The note is purely for the agent's own bookkeeping and never needs to reach
  the user's devices → write it with the `memory_append` tool instead.
- The target is a different note-taking app (Bear, Obsidian, etc.) — those are
  out of scope and `memo` cannot touch them.

## Command cookbook

All commands run through the `exec` tool. The verb is always `memo notes`,
followed by a flag that selects the operation.

**Reading and finding**

```bash
memo notes                        # List all notes
memo notes -f "Folder Name"       # Filter by folder
memo notes -s "query"             # Search notes (fuzzy)
```

**Adding**

```bash
memo notes -a                     # Interactive editor
memo notes -a "Note Title"        # Quick add with title
```

**Editing, removing, and reorganizing** — each of these opens an interactive
picker so the operator can choose the target note:

```bash
memo notes -e                     # Interactive selection to edit
memo notes -d                     # Interactive selection to delete
memo notes -m                     # Move note to folder (interactive)
```

**Pulling content out**

```bash
memo notes -ex                    # Export to HTML/Markdown
```

## Known constraints

- Notes that embed images or other attachments cannot be edited through `memo`.
- The edit/delete/move flows are interactive and need a real terminal session;
  drive them with `exec` configured for an interactive/PTY-style stream when the
  wrapper allows it.
- There is no non-macOS support — the Notes app is mandatory.

## Operating rules

1. Steer toward Apple Notes whenever the value is in having the note sync to the
   user's Apple devices.
2. Keep scratch notes that only matter to the agent in `memory_append`, not in
   the user's synced Notes.
3. Deletions and overwrites are destructive across every synced device — get
   explicit confirmation before running `-d` or replacing existing content.
