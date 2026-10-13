"""The agent's agenda: follow-ups it plans for itself and the limits on them.

- :mod:`.store` — pending wake-ups, their history, per-conversation reply
  state and the owner's proactivity settings.
- :mod:`.policy` — the limits code enforces before any turn runs.
- :mod:`.budget` — one daily budget shared by every agent on the machine.
- :mod:`.service` — the runner wired into each gateway process.
- :mod:`.timeparse`, :mod:`.prompts` — reading times, writing wake-up prompts.

See ``docs/engineering/agent-goals.md``.
"""
