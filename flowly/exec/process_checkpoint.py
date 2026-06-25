"""Disk-backed checkpoint for the process registry.

Persists running ``ProcessSession`` metadata to ``~/.flowly/processes.json``
so that a Flowly restart can re-probe surviving subprocesses (started with
``start_new_session=True``, so they outlive the parent CLI/gateway).

A recovered session is marked ``detached``: we know the PID, command, and
session_key, but no live stdout pipe and no reader task — the user can
poll/kill it but not read past output.

Pattern adapted from ``flowly.cron.service`` (atomic temp+os.replace, blank
state on corrupt JSON, immediate save on every mutation).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.exec.process_registry import ProcessSession


CHECKPOINT_VERSION = 1


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class ProcessCheckpoint:
    def __init__(self, store_path: Path):
        self.store_path = store_path

    def save(self, sessions: list[ProcessSession]) -> None:
        running = [s for s in sessions if not s.exited and s.pid is not None]
        data: dict[str, Any] = {
            "version": CHECKPOINT_VERSION,
            "processes": [
                {
                    "id": s.id,
                    "command": s.command,
                    "sessionKey": s.session_key,
                    "pid": s.pid,
                    "cwd": s.cwd,
                    "startedAt": s.started_at,
                    "watchPatterns": list(s.watch_patterns),
                    "notifyOnComplete": s.notify_on_complete,
                }
                for s in running
            ],
        }

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.store_path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(self.store_path))
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def load(self) -> list[dict[str, Any]]:
        """Load checkpoint entries. Returns raw dicts (caller probes liveness)."""
        if not self.store_path.exists():
            return []
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            entries = data.get("processes", [])
            if not isinstance(entries, list):
                return []
            return entries
        except Exception as e:
            logger.warning(f"[ProcessCheckpoint] failed to load {self.store_path}: {e}")
            return []

    def recover(self) -> list[ProcessSession]:
        """Probe each persisted PID; return ProcessSession for survivors only.

        Survivors are marked exited=False but with no process handle and no
        reader — caller must register them in the registry's ``_running``
        dict and treat them as observable-but-not-readable detached sessions.
        """
        recovered: list[ProcessSession] = []
        for entry in self.load():
            pid = entry.get("pid")
            if not isinstance(pid, int) or not _is_pid_alive(pid):
                continue
            session = ProcessSession(
                id=entry.get("id", ""),
                command=entry.get("command", ""),
                session_key=entry.get("sessionKey"),
                started_at=float(entry.get("startedAt", 0.0)),
                cwd=entry.get("cwd"),
                pid=pid,
                process=None,
                exited=False,
                notify_on_complete=bool(entry.get("notifyOnComplete", False)),
                watch_patterns=list(entry.get("watchPatterns") or []),
            )
            recovered.append(session)
        if recovered:
            logger.info(
                f"[ProcessCheckpoint] recovered {len(recovered)} surviving "
                f"process(es) from {self.store_path}"
            )
        return recovered
