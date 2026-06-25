"""Exec approval store and allowlist management."""

import fnmatch
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from filelock import FileLock
from loguru import logger

from flowly.exec.types import (
    ExecSecurity,
    ExecAsk,
    ExecConfig,
    ExecRequest,
    AllowlistEntry,
    PendingApproval,
    ExecApprovalDecision,
)
from flowly.exec.safety import analyze_command, DEFAULT_SAFE_BINS
from flowly.profile import get_flowly_home


@dataclass
class ExecApprovalsConfig:
    """Stored exec approvals configuration."""
    version: int = 1
    security: ExecSecurity = "full"
    ask: ExecAsk = "off"
    ask_fallback: ExecSecurity = "deny"
    allowlist: list[AllowlistEntry] = field(default_factory=list)


def _get_approvals_path() -> Path:
    """Get path to exec approvals file."""
    return get_flowly_home() / "credentials" / "exec-approvals.json"


_VALID_SECURITY = {"deny", "allowlist", "full"}
_VALID_ASK = {"off", "on-miss", "always"}


def _legacy_exec_policy_seed() -> tuple[str, str] | None:
    """One-time upgrade seed for the exec-approvals store.

    Older builds wrote the setup wizard's security/ask choice to config.json
    ``tools.exec``, but the executor only ever enforced the approvals store —
    so the user's choice was silently ignored. Current builds removed those
    config fields (they're ignored on load). When the store file does not yet
    exist (fresh install or first run after upgrade), we read any legacy
    ``tools.exec.security``/``ask`` straight from config.json so an upgrading
    user's original intent is finally honoured instead of silently defaulting
    to ``full``/``off``.

    Best-effort and read-only: returns ``None`` on any problem (no config,
    unparseable, or no valid legacy value), in which case the caller falls
    back to the hardcoded defaults.
    """
    try:
        path = get_flowly_home() / "config.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        exec_cfg = ((data.get("tools") or {}).get("exec")) or {}
        security = exec_cfg.get("security")
        if security not in _VALID_SECURITY:
            return None
        ask = exec_cfg.get("ask")
        if ask not in _VALID_ASK:
            ask = "off"
        return security, ask
    except Exception:
        return None


class ExecApprovalStore:
    """
    Manages exec approvals and allowlist.

    Handles:
    - Allowlist storage and matching
    - Pending approval requests
    - Approval callbacks (for Telegram integration)
    """

    def __init__(self):
        self._config: ExecApprovalsConfig | None = None
        self._pending: dict[str, PendingApproval] = {}
        self._approval_callback: Callable[[PendingApproval], Awaitable[ExecApprovalDecision | None]] | None = None
        # mtime of the file as we last loaded/saved it, so a long-lived store
        # (e.g. the running gateway's exec tool) can notice and pick up policy
        # edits made by another writer — the TUI policy editor, `flowly
        # approvals set`, or a hand edit — without a restart.
        self._loaded_mtime: float | None = None

    @staticmethod
    def _file_mtime() -> float | None:
        try:
            return _get_approvals_path().stat().st_mtime
        except OSError:
            return None

    def refresh_if_changed(self) -> None:
        """Reload from disk if the file changed since we last read/wrote it.

        Cheap (one stat); only re-parses when the mtime actually moved.
        """
        if self._config is None:
            return
        mtime = self._file_mtime()
        if mtime is not None and mtime != self._loaded_mtime:
            self.load()

    def set_approval_callback(
        self,
        callback: Callable[[PendingApproval], Awaitable[ExecApprovalDecision | None]]
    ) -> None:
        """Set callback for requesting approvals (e.g., via Telegram)."""
        self._approval_callback = callback

    def load(self) -> ExecApprovalsConfig:
        """Load approvals config from disk."""
        path = _get_approvals_path()

        if not path.exists():
            seed = _legacy_exec_policy_seed()
            if seed is not None:
                security, ask = seed
                self._config = ExecApprovalsConfig(security=security, ask=ask)
                logger.info(
                    "Seeded exec-approvals store from legacy config.json "
                    "tools.exec (security=%s, ask=%s)",
                    security,
                    ask,
                )
            else:
                self._config = ExecApprovalsConfig()
            self.save()  # Create file (seeded from legacy config or defaults)
            return self._config

        try:
            with FileLock(path.with_suffix(".lock"), timeout=10):
                data = json.loads(path.read_text(encoding="utf-8"))
                allowlist = [
                    AllowlistEntry(
                        pattern=e.get("pattern", ""),
                        last_used_at=e.get("last_used_at"),
                        last_used_command=e.get("last_used_command"),
                        last_resolved_path=e.get("last_resolved_path"),
                    )
                    for e in data.get("allowlist", [])
                ]
                self._config = ExecApprovalsConfig(
                    version=data.get("version", 1),
                    security=data.get("security", "full"),
                    ask=data.get("ask", "off"),
                    ask_fallback=data.get("ask_fallback", "deny"),
                    allowlist=allowlist,
                )
        except Exception as e:
            logger.warning(f"Error loading exec approvals: {e}")
            self._config = ExecApprovalsConfig()

        self._loaded_mtime = self._file_mtime()
        return self._config

    def save(self) -> None:
        """Save approvals config to disk."""
        if not self._config:
            return

        path = _get_approvals_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._config.version,
            "security": self._config.security,
            "ask": self._config.ask,
            "ask_fallback": self._config.ask_fallback,
            "allowlist": [
                {
                    "pattern": e.pattern,
                    "last_used_at": e.last_used_at,
                    "last_used_command": e.last_used_command,
                    "last_resolved_path": e.last_resolved_path,
                }
                for e in self._config.allowlist
            ],
        }

        with FileLock(path.with_suffix(".lock"), timeout=10):
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            from flowly.utils.file_security import secure_file
            secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
        except OSError:
            pass
        # Record our own write so refresh_if_changed() doesn't reload it.
        self._loaded_mtime = self._file_mtime()

    @property
    def config(self) -> ExecApprovalsConfig:
        """Get current config, loading if needed."""
        if not self._config:
            self.load()
        return self._config

    def add_to_allowlist(self, pattern: str, command: str | None = None, resolved_path: str | None = None) -> None:
        """Add a pattern to the allowlist."""
        config = self.config

        # Check if pattern already exists
        for entry in config.allowlist:
            if entry.pattern == pattern:
                entry.last_used_at = int(time.time() * 1000)
                if command:
                    entry.last_used_command = command
                if resolved_path:
                    entry.last_resolved_path = resolved_path
                self.save()
                return

        # Add new entry
        config.allowlist.append(AllowlistEntry(
            pattern=pattern,
            last_used_at=int(time.time() * 1000),
            last_used_command=command,
            last_resolved_path=resolved_path,
        ))
        self.save()

    def remove_from_allowlist(self, pattern: str) -> bool:
        """Remove a pattern from the allowlist."""
        config = self.config
        original_len = len(config.allowlist)
        config.allowlist = [e for e in config.allowlist if e.pattern != pattern]

        if len(config.allowlist) != original_len:
            self.save()
            return True
        return False

    def check_allowlist(self, resolved_path: str | None) -> bool:
        """Check if a resolved path matches the allowlist."""
        if not resolved_path:
            return False

        config = self.config

        for entry in config.allowlist:
            pattern = entry.pattern

            # Expand home directory
            if pattern.startswith("~"):
                pattern = str(Path(pattern).expanduser())

            # Use fnmatch for glob matching
            if fnmatch.fnmatch(resolved_path, pattern):
                # Update last used
                entry.last_used_at = int(time.time() * 1000)
                entry.last_resolved_path = resolved_path
                self.save()
                return True

        return False

    def create_pending(self, request: ExecRequest, timeout_seconds: int = 120) -> PendingApproval:
        """Create a pending approval request."""
        now = time.time()
        approval_id = secrets.token_hex(8)

        # Get resolved path and risk reasons from analysis so the approver
        # sees *why* this command triggered a prompt (e.g. "Matches dangerous
        # command pattern", "Multi-line command"). Without this the user gets
        # a generic approve/deny with no context.
        analysis = analyze_command(request.command)

        pending = PendingApproval(
            id=approval_id,
            request=request,
            created_at=now,
            expires_at=now + timeout_seconds,
            session_key=request.session_key,
            resolved_path=analysis.resolved_path,
            risk_reasons=list(analysis.risk_reasons),
            # "allow-always" only persists when we have a path to allowlist
            # (see resolve_pending). Pipelines / builtins resolve to no path,
            # so remembering them is impossible — don't offer it.
            supports_always=analysis.resolved_path is not None,
        )

        self._pending[approval_id] = pending
        return pending

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        """Get a pending approval by ID."""
        pending = self._pending.get(approval_id)
        if pending and time.time() > pending.expires_at:
            del self._pending[approval_id]
            return None
        return pending

    def resolve_pending(self, approval_id: str, decision: ExecApprovalDecision) -> bool:
        """Resolve a pending approval."""
        pending = self._pending.pop(approval_id, None)
        if not pending:
            return False

        if decision == "allow-always" and pending.resolved_path:
            # Add to allowlist
            self.add_to_allowlist(
                pattern=pending.resolved_path,
                command=pending.request.command,
                resolved_path=pending.resolved_path,
            )

        return True

    def prune_expired(self) -> int:
        """Remove expired pending approvals."""
        now = time.time()
        expired = [k for k, v in self._pending.items() if now > v.expires_at]
        for k in expired:
            del self._pending[k]
        return len(expired)

    async def request_approval(self, pending: PendingApproval) -> ExecApprovalDecision | None:
        """Request approval via callback (e.g., Telegram)."""
        if not self._approval_callback:
            logger.warning("No approval callback set")
            return None

        return await self._approval_callback(pending)


def check_allowlist(store: ExecApprovalStore, resolved_path: str | None, executable: str | None) -> bool:
    """
    Check if command is allowed via allowlist or safe bins.

    Returns True if allowed.
    """
    # Check safe bins (always allowed for stdin-only operations)
    if executable:
        name = Path(executable).name if '/' in executable else executable
        if name in DEFAULT_SAFE_BINS:
            return True

    # Check allowlist
    return store.check_allowlist(resolved_path)


def requires_approval(
    config: ExecApprovalsConfig,
    analysis_ok: bool,
    allowlist_satisfied: bool,
) -> bool:
    """
    Determine if a command requires explicit approval.

    Returns True if approval is needed.
    """
    ask = config.ask
    security = config.security

    # Always ask mode
    if ask == "always":
        return True

    # On-miss mode: ask only if not in allowlist
    if ask == "on-miss" and security == "allowlist":
        if not analysis_ok or not allowlist_satisfied:
            return True

    return False
