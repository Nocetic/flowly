# Flowly video operator

This is the deterministic run ledger for the one-week autonomous YouTube
experiment. Flowly and its connected tools remain responsible for creative
work; this directory makes production timing, publication timing, retries and
audit state durable across gateway restarts.

The two jobs are intentionally separate:

- Production starts before the publication slot and finishes in `READY`.
- Publication starts at the exact slot and only accepts a `READY` run.

A completed video therefore waits safely. An incomplete or late video is
skipped rather than published.

## State

```text
PLANNED -> PRODUCING -> READY -> PUBLISHING -> PUBLISHED
                  |         |             |
                  v         v             v
                FAILED    LATE          READY (retry)
```

Run artifacts live under `runs/<run-id>/` and the SQLite ledger is
`operator.sqlite3`.

## Basic commands

```bash
python3 operator.py init
python3 operator.py schedule --days 7
python3 operator.py claim-production
python3 operator.py mark-ready --run <run-id>
python3 operator.py claim-publish
python3 operator.py mark-published --run <run-id> --video-id <id>
python3 operator.py status
```

The production and publication prompts are in `PRODUCTION.md` and
`PUBLISH.md`.
