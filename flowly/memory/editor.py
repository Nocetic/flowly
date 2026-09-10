"""Bounded, owner-facing memory editing. Never accepts a filesystem path."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

from flowly.config.transaction import config_write_lock
from flowly.memory.governance import GovernanceStore
from flowly.memory.summary import (
    SENTINEL_END, SENTINEL_START, extract_manual_content, regenerate_memory_md,
)

MAX_TEXT_BYTES = 256 * 1024


class MemoryEditorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_revision(item) -> str:
    # Usage timestamps must not invalidate an otherwise unchanged draft.
    return revision(json.dumps([item.id, item.text, item.status, item.updated_at]))


def read_text(path: Path) -> str:
    if path.is_symlink():
        raise MemoryEditorError("MEMORY_UNSAFE_PATH", "This memory file is a symbolic link.")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return ""
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise MemoryEditorError("MEMORY_UNSAFE_PATH", "This memory document is not a regular file.")
        raw = handle.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            raise MemoryEditorError("MEMORY_TOO_LARGE", "This document is too large for the editor.")
        return raw.decode('utf-8')


def atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".memory-edit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def document_path(workspace: Path, name: str) -> Path:
    if not isinstance(name, str) or name not in {"user", "notes"}:
        raise MemoryEditorError("INVALID_PARAMS", "Choose a memory document.")
    parent = workspace if name == "user" else workspace / "memory"
    if workspace.is_symlink() or parent.is_symlink():
        raise MemoryEditorError("MEMORY_UNSAFE_PATH", "The memory directory is a symbolic link.")
    return parent / ("USER.md" if name == "user" else "MEMORY.md")


def document(workspace: Path, name: str) -> dict:
    text = read_text(document_path(workspace, name))
    if name == "notes":
        text = extract_manual_content(text)
    return {"text": text, "revision": revision(text)}


def save_document(workspace: Path, name: str, text: str, expected: str) -> dict:
    path = document_path(workspace, name)
    if name == "notes" and (SENTINEL_START in text or SENTINEL_END in text):
        raise MemoryEditorError("INVALID_PARAMS", "Generated memory markers cannot be edited here.")
    with config_write_lock(path):
        current = read_text(path)
        manual = extract_manual_content(current) if name == "notes" else current
        if revision(manual) != expected:
            raise MemoryEditorError("MEMORY_CONFLICT", "This content changed. Reload it before saving.")
        output = text
        if name == "notes":
            start, end = current.find(SENTINEL_START), current.find(SENTINEL_END)
            if start >= 0 and end >= start:
                generated = current[start:end + len(SENTINEL_END)]
                output = text.rstrip() + "\n\n" + generated + "\n"
        atomic_write(path, output)
        return document(workspace, name)


def dispatch_editor(action: str, params: dict, workspace: Path, db_path: Path) -> dict:
    if action == "document":
        return document(workspace, params.get("document"))
    if action == "save":
        text, expected = params.get("text"), params.get("revision")
        if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES
                or not isinstance(expected, str) or len(expected) != 64):
            raise MemoryEditorError("INVALID_PARAMS", "The draft or its revision is invalid.")
        kind = params.get("kind")
        if isinstance(kind, str) and kind in {"user", "notes"}:
            return save_document(workspace, kind, text, expected)
        if kind != "entry" or not isinstance(params.get("id"), str) or not text.strip():
            raise MemoryEditorError("INVALID_PARAMS", "Choose a memory and enter its text.")
    elif action != "list":
        raise MemoryEditorError("INVALID_PARAMS", "Unknown memory editor operation.")

    store = GovernanceStore(db_path)
    try:
        if action == "list":
            cursor, query = params.get("cursor", ""), params.get("query", "")
            if not isinstance(cursor, str) or len(cursor) > 128 or not isinstance(query, str) or len(query) > 200:
                raise MemoryEditorError("INVALID_PARAMS", "The memory search is invalid.")
            items = store.editor_page(cursor, query)
            return {
                "items": [{**item.to_dict(), "revision": item_revision(item)} for item in items[:50]],
                "nextCursor": items[49].id if len(items) > 50 else None,
            }
        item = store.editor_correct(params["id"], text, expected)
        # The agent reads the generated summary as well as the database.
        from flowly.agent.memory import MemoryStore
        pending = False
        try:
            regenerate_memory_md(store, MemoryStore(workspace), kg_summary=None)
        except (OSError, TimeoutError, sqlite3.Error):
            # The authoritative edit has committed; do not report a failed save.
            pending = True
        return {"item": {**item.to_dict(), "revision": item_revision(item)}, "summaryPending": pending}
    finally:
        store.close()
