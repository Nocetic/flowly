"""Obsidian vault integration.

Treats an Obsidian vault as a local-first Markdown knowledge source. The
vault is searched and cited, optionally indexed for fast lookup, and selected
notes can be turned into *review-gated* memory candidates — but vault content
is always treated as untrusted and is never auto-written to memory nor dumped
wholesale into the prompt.
"""

from flowly.obsidian.vault import (
    VaultError,
    VaultNotConfigured,
    VaultPermissionDenied,
    resolve_vault_path,
    safe_resolve,
    iter_notes,
    read_note,
)

__all__ = [
    "VaultError",
    "VaultNotConfigured",
    "VaultPermissionDenied",
    "resolve_vault_path",
    "safe_resolve",
    "iter_notes",
    "read_note",
]
