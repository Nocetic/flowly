"""The gateway's own log, as the owner reads it (Settings → Logs).

- ``files``: which file is the log, and a cursor that survives rotation.
- ``bridge``: third-party ``logging`` records into the same log, with known
  noise (internet scanners, reconnects) turned down instead of dumped.
- ``events``: the log as structured events: level, source, message, detail,
  a code for situations clients explain in plain words, repeats collapsed.
"""
