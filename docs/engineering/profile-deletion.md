# Deleting a named profile

Status: `codex/agent-home-core` (2026-09-28). Desktop counterpart:
`docs/engineering/profile-deletion.md` in flowly-desktop.

## What went wrong

Two symptoms, one cause: a runtime could start in a profile while it was being
deleted, or after.

- **`[Errno 66] Directory not empty`.** `delete_profile` checked the runtime
  lease and then ran `shutil.rmtree` without a lock. A runtime starting in
  between wrote files (logs, databases) into the tree being removed, and the
  removal failed halfway, leaving a half-deleted bot.
- **A deleted bot came back broken.** Nothing stopped `flowly --profile X
  serve` in a missing profile: start-up created `profiles/X/` (`logs/`,
  `media.sqlite`) and `claim_runtime_lease` `mkdir`ed it. Listing only asks
  "is it a directory", so the bot reappeared with no `profile.json` (no seed,
  shape or colour, so a different avatar) and a runtime that could not start
  (no config), shown as "Needs attention".

Reproduced before the fix: `HOME=<tmp> flowly --profile ghost serve` created
`profiles/ghost/logs/gateway.log` and `profiles/ghost/media.sqlite`.

## What holds now

1. **The CLI never runs in a missing profile** (`flowly/cli/entry.py`). An
   explicit `-p/--profile` or `FLOWLY_PROFILE` naming a profile that does not
   exist exits with code 2 before any Flowly module is imported, so nothing is
   created. A sticky `active_profile` that was deleted elsewhere falls back to
   the default. The profile directory's identity (`device:inode`) is recorded
   in `FLOWLY_PROFILE_DIR_ID`.
2. **Deleting retires the name atomically** (`delete_profile`). Under the
   profile mutation lock it re-checks the lease and renames the directory to
   `profiles/.trash/<name>.<uuid>`. From then on nothing can list, start or
   write into the profile, and the name is free again. Removing the renamed
   tree is best effort with short retries; leftovers are swept on the next
   delete. `.trash` is not a valid profile name, so listings and the capacity
   count never see it.
3. **A runtime claims its lease only for the directory it started in**
   (`claim_runtime_lease`). For a profile under `profiles/`, the claim runs
   under the same lock a delete retires the profile under. A missing
   directory is refused. A directory whose identity differs from
   `FLOWLY_PROFILE_DIR_ID` was deleted and recreated by this runtime's own
   start-up writes: it is removed (when it has no `profile.json`) and the
   claim is refused. The default home and an explicit `FLOWLY_HOME` outside
   `profiles/` keep the old behaviour.

## Tests

`tests/test_profile_delete_retirement.py`: a delete that keeps meeting
ENOTEMPTY still succeeds and frees the name, and the next delete sweeps the
leftover; retired trees never count as bots; a runtime cannot claim a
deleted profile, and removes what it recreated; a live profile is still
claimed; the CLI never creates a missing profile (subprocess). Each guard was
removed once to confirm its test fails without it.
