"""Explicit, transactional repair primitives for ``flowly doctor``."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from flowly.config.loader import convert_to_camel
from flowly.config.schema import Config
from flowly.diagnostics.config import (
    deduplicate_preserving_runtime_values,
    read_config_snapshot,
)
from flowly.diagnostics.models import DoctorContext, RepairRisk, Status

Validator = Callable[[Path], None]


@dataclass(frozen=True)
class RepairOutcome:
    name: str
    message: str
    changed_paths: tuple[Path, ...]


def _secure_file(path: Path) -> None:
    from flowly.utils.file_security import secure_file

    secure_file(path)


def _secure_dir(path: Path) -> None:
    from flowly.utils.file_security import secure_dir

    secure_dir(path)


def _unique_sibling(path: Path, label: str) -> Path:
    return path.with_name(
        f"{path.name}.doctor-{label}-{int(time.time())}-{secrets.token_hex(4)}"
    )


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _same_file_version(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _publish_new(path: Path, data: bytes, validate: Validator) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to replace existing path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = _unique_sibling(path, "tmp")
    try:
        _write_private(temp, data)
        validate(temp)
        # Hard-link publication fails atomically if another process created the
        # destination after our existence check. The temporary file is on the
        # same filesystem because it is a sibling.
        os.link(temp, path)
        _secure_file(path)
        validate(path)
    except Exception:
        if path.exists() and temp.exists():
            try:
                if os.path.samefile(path, temp):
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        temp.unlink(missing_ok=True)


def _restore_from_backup(path: Path, backup: Path, validate: Validator) -> None:
    rollback = _unique_sibling(path, "rollback")
    try:
        _write_private(rollback, backup.read_bytes())
        validate(rollback)
        os.replace(rollback, path)
        _secure_file(path)
        validate(path)
    finally:
        rollback.unlink(missing_ok=True)


def _transactional_install(path: Path, data: bytes, validate: Validator) -> Path | None:
    """Install validated bytes, backing up and rolling back an existing file."""
    if path.is_symlink():
        raise ValueError(f"refusing to repair through symbolic link: {path}")
    if not path.exists():
        _publish_new(path, data, validate)
        return None
    if not path.is_file():
        raise ValueError(f"repair target is not a regular file: {path}")

    original_stat = path.stat()
    original = path.read_bytes()
    backup = _unique_sibling(path, "backup")
    temp = _unique_sibling(path, "tmp")
    replaced = False
    try:
        _write_private(backup, original)
        _secure_file(backup)
        _write_private(temp, data)
        validate(temp)
        if not _same_file_version(original_stat, path.stat()):
            raise RuntimeError("repair target changed concurrently; retry after stopping writers")
        os.replace(temp, path)
        replaced = True
        _secure_file(path)
        validate(path)
        return backup
    except Exception:
        temp.unlink(missing_ok=True)
        if replaced:
            _restore_from_backup(path, backup, validate=lambda candidate: None)
        raise


def _validate_config(path: Path) -> None:
    snapshot = read_config_snapshot(path)
    if snapshot.config is None:
        raise ValueError(snapshot.error or "config candidate did not validate")


def _fix_state_directory(ctx: DoctorContext) -> RepairOutcome:
    path = ctx.data_dir
    if path.is_symlink():
        raise ValueError(f"refusing to repair symbolic state directory: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"state path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_dir(path)
    return RepairOutcome("state_dir", f"Secured state directory: {path}", (path,))


def _fix_missing_config(ctx: DoctorContext) -> RepairOutcome:
    path = ctx.config_path
    if path.exists() or path.is_symlink():
        raise ValueError("config exists; missing-config repair will not overwrite it")
    config = Config()
    config.agents.defaults.workspace = str(ctx.data_dir / "workspace")
    raw = convert_to_camel(config.model_dump())
    encoded = (json.dumps(raw, indent=4, ensure_ascii=False) + "\n").encode()
    _publish_new(path, encoded, _validate_config)
    return RepairOutcome("config_file", f"Created validated config: {path}", (path,))


def _fix_config_permissions(ctx: DoctorContext) -> RepairOutcome:
    path = ctx.config_path
    if path.is_symlink() or not path.is_file():
        raise ValueError("config permission repair requires a regular file")
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.is_symlink():
        raise ValueError("config backup is a symbolic link")
    changed = [path]
    if backup.is_file():
        changed.append(backup)
    for candidate in changed:
        _secure_file(candidate)
    if os.name != "nt":
        insecure = [
            candidate
            for candidate in changed
            if stat.S_IMODE(candidate.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        ]
        if insecure:
            raise PermissionError("config permissions remain broader than owner-only")
    return RepairOutcome(
        "config_permissions",
        f"Restricted permissions on {len(changed)} config file(s)",
        tuple(changed),
    )


def _builtin_workspace() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "workspace",
        here.parent.parent.parent / "workspace",
    ]
    return next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])


def _ensure_directory(path: Path, changed: list[Path]) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to seed symbolic directory: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"workspace item is not a directory: {path}")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        changed.append(path)


def _copy_missing(source: Path | None, destination: Path, fallback: str) -> bool:
    if destination.is_symlink():
        raise ValueError(f"refusing to seed symbolic file: {destination}")
    if destination.exists():
        if not destination.is_file():
            raise ValueError(f"workspace item is not a regular file: {destination}")
        return False
    data = source.read_bytes() if source is not None and source.is_file() else fallback.encode()
    _publish_new(destination, data, validate=lambda candidate: candidate.read_bytes())
    return True


def _fix_workspace(ctx: DoctorContext) -> RepairOutcome:
    if ctx.config is None:
        raise ValueError("workspace repair requires a valid config snapshot")
    workspace = Path(ctx.config.agents.defaults.workspace).expanduser().resolve()
    if not workspace.is_relative_to(ctx.data_dir.resolve()):
        raise ValueError("automatic workspace seeding is restricted to the active profile")
    changed: list[Path] = []
    _ensure_directory(workspace, changed)
    for name in ("memory", "skills", "personas"):
        _ensure_directory(workspace / name, changed)

    builtin = _builtin_workspace()
    for name in ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "HEARTBEAT.md"):
        if _copy_missing(builtin / name, workspace / name, f"# {name}\n"):
            changed.append(workspace / name)
    memory = workspace / "memory" / "MEMORY.md"
    if _copy_missing(
        builtin / "memory" / "MEMORY.md",
        memory,
        "# Long-term Memory\n",
    ):
        changed.append(memory)
    return RepairOutcome(
        "workspace",
        f"Seeded {len(changed)} missing workspace item(s): {workspace}",
        tuple(changed),
    )


_SAFE_FIXERS = {
    "state_dir": _fix_state_directory,
    "config_file": _fix_missing_config,
    "config_permissions": _fix_config_permissions,
    "workspace": _fix_workspace,
}


def apply_safe_fixes(ctx: DoctorContext) -> tuple[list[RepairOutcome], list[tuple[str, Exception]]]:
    """Apply only findings explicitly marked low-risk and allowlisted here."""
    outcomes: list[RepairOutcome] = []
    failures: list[tuple[str, Exception]] = []
    by_name = {result.name: result for result in ctx.results}
    for critical in ("state_dir", "config_file", "config_permissions"):
        finding = by_name.get(critical)
        if (
            finding is not None
            and finding.status in {Status.WARN, Status.ERROR}
            and (not finding.fixable or finding.risk != RepairRisk.LOW)
        ):
            return outcomes, failures
    for name, fixer in _SAFE_FIXERS.items():
        finding = by_name.get(name)
        if (
            finding is None
            or finding.status not in {Status.WARN, Status.ERROR}
            or not finding.fixable
            or finding.risk != RepairRisk.LOW
        ):
            continue
        duplicate = by_name.get("duplicate_keys")
        if name == "workspace" and duplicate is not None and duplicate.status == Status.ERROR:
            continue
        try:
            outcomes.append(fixer(ctx))
        except Exception as exc:  # Keep repair failures isolated and redacted.
            failures.append((name, exc))
    return outcomes, failures


def repair_config_duplicates(config_path: Path) -> RepairOutcome:
    snapshot = read_config_snapshot(config_path)
    if snapshot.config is None or snapshot.raw is None:
        raise ValueError("duplicate repair requires a schema-valid config")
    if not snapshot.duplicates:
        return RepairOutcome("config_duplicates", "No duplicate keys need repair", ())

    repaired = deduplicate_preserving_runtime_values(snapshot.raw)
    expected = snapshot.config.model_dump(mode="json")

    def validate(candidate: Path) -> None:
        candidate_snapshot = read_config_snapshot(candidate)
        if candidate_snapshot.config is None:
            raise ValueError(candidate_snapshot.error or "candidate config is invalid")
        if candidate_snapshot.duplicates:
            raise ValueError("candidate config still contains duplicate aliases")
        if candidate_snapshot.config.model_dump(mode="json") != expected:
            raise ValueError("candidate config changed effective runtime values")

    encoded = (json.dumps(repaired, indent=4, ensure_ascii=False) + "\n").encode()
    backup = _transactional_install(config_path, encoded, validate)
    changed = (config_path, backup) if backup is not None else (config_path,)
    return RepairOutcome(
        "config_duplicates",
        f"Removed {len(snapshot.duplicates)} duplicate collision(s)",
        changed,
    )


def repair_config_backup(config_path: Path) -> RepairOutcome:
    backup_source = config_path.with_suffix(config_path.suffix + ".bak")
    if backup_source.is_symlink() or not backup_source.is_file():
        raise ValueError(f"valid config backup is unavailable: {backup_source}")
    backup_snapshot = read_config_snapshot(backup_source)
    if backup_snapshot.config is None or backup_snapshot.duplicates:
        raise ValueError("config backup is invalid or contains duplicate keys")
    expected = backup_snapshot.config.model_dump(mode="json")

    def validate(candidate: Path) -> None:
        candidate_snapshot = read_config_snapshot(candidate)
        if candidate_snapshot.config is None or candidate_snapshot.duplicates:
            raise ValueError(candidate_snapshot.error or "restored config is invalid")
        if candidate_snapshot.config.model_dump(mode="json") != expected:
            raise ValueError("restored config differs from the validated backup")

    forensic = _transactional_install(config_path, backup_source.read_bytes(), validate)
    changed = (config_path, forensic) if forensic is not None else (config_path,)
    return RepairOutcome(
        "config_backup",
        f"Restored validated backup: {backup_source}",
        changed,
    )


def _salvage_session_candidate(path: Path) -> tuple[bytes, int] | None:
    """Return a conservative valid prefix, or ``None`` when no repair is needed."""
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError(f"session exceeds 100 MiB salvage limit: {path.name}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"session is not valid UTF-8: {path.name}") from exc

    payload_lines = [line for line in lines if line.strip()]
    records: list[dict] = []
    issue = False
    for line in payload_lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            issue = True
            break
        if not isinstance(record, dict):
            issue = True
            break
        records.append(record)

    expected: set[str] = set()
    issuing_index = -1
    keep = len(records)
    for index, record in enumerate(records):
        if record.get("_type") == "metadata":
            continue
        role = record.get("role")
        if expected:
            tool_id = record.get("tool_call_id") if role == "tool" else None
            if not isinstance(tool_id, str) or tool_id not in expected:
                keep = issuing_index
                issue = True
                break
            expected.remove(tool_id)
            continue
        if role == "tool":
            keep = index
            issue = True
            break
        calls = record.get("tool_calls") if role == "assistant" else None
        if calls:
            if not isinstance(calls, list):
                keep = index
                issue = True
                break
            ids = [call.get("id") for call in calls if isinstance(call, dict)]
            valid_ids = (
                len(ids) == len(calls)
                and all(isinstance(item, str) and item for item in ids)
                and len(set(ids)) == len(ids)
            )
            if not valid_ids:
                keep = index
                issue = True
                break
            expected = set(ids)
            issuing_index = index
    if expected and keep == len(records):
        keep = issuing_index
        issue = True
    if not issue:
        return None

    candidate = records[:keep]
    message_count = sum(record.get("_type") != "metadata" for record in candidate)
    if not message_count:
        raise ValueError(f"session has no safely salvageable message prefix: {path.name}")
    encoded = b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for record in candidate
    )
    removed = max(1, len(payload_lines) - len(candidate))
    return encoded, removed


def _validate_session_file(path: Path) -> None:
    from flowly.diagnostics.checks import _scan_session

    _, errors = _scan_session(path)
    if errors:
        raise ValueError("session candidate failed semantic verification")


def repair_sessions(data_dir: Path) -> RepairOutcome:
    sessions_dir = data_dir / "sessions"
    if sessions_dir.is_symlink() or not sessions_dir.is_dir():
        raise ValueError(f"session directory is unavailable or symbolic: {sessions_dir}")
    candidates: list[tuple[Path, bytes, int]] = []
    for path in sorted(sessions_dir.glob("*.jsonl")):
        if path.name.endswith(".full.jsonl"):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"refusing to salvage symbolic/non-file transcript: {path.name}")
        candidate = _salvage_session_candidate(path)
        if candidate is not None:
            encoded, removed = candidate
            candidates.append((path, encoded, removed))
    if not candidates:
        return RepairOutcome("session_salvage", "No session transcript needs salvage", ())

    installed: list[tuple[Path, Path]] = []
    changed: list[Path] = []
    removed_total = 0
    try:
        for path, encoded, removed in candidates:
            backup = _transactional_install(path, encoded, _validate_session_file)
            if backup is None:  # Existing canonical transcripts always produce a backup.
                raise RuntimeError("session repair unexpectedly lacked a forensic backup")
            installed.append((path, backup))
            changed.extend((path, backup))
            removed_total += removed
    except Exception:
        for path, backup in reversed(installed):
            _restore_from_backup(path, backup, validate=lambda candidate: None)
        raise
    return RepairOutcome(
        "session_salvage",
        f"Salvaged {len(candidates)} transcript(s); removed {removed_total} unsafe record(s)",
        tuple(changed),
    )


def _open_immutable_db(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"canonical database is unavailable or symbolic: {path}")
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError(
            f"database has an active WAL; stop the gateway before repair: {path.name}"
        )
    uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    return connection


def _read_memory_items(path: Path) -> list:
    from flowly.memory.governance import STATUS_ACTIVE, MemoryItem

    connection = _open_immutable_db(path)
    try:
        rows = connection.execute(
            "SELECT id, kind, text, status, confidence, privacy_level "
            "FROM memory_items WHERE status = ? ORDER BY created_at, id",
            (STATUS_ACTIVE,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("memory governance database schema is invalid") from exc
    finally:
        connection.close()
    return [
        MemoryItem(
            id=str(row["id"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            privacy_level=str(row["privacy_level"] or "normal"),
        )
        for row in rows
        if str(row["privacy_level"] or "normal") != "secret"
    ]


def _read_kg_summary(path: Path, *, max_entities: int = 20) -> str:
    if not path.exists():
        return ""
    from flowly.memory.knowledge_graph import VALUE_PREDICATES

    connection = _open_immutable_db(path)
    try:
        entities = connection.execute(
            """
            SELECT e.id, e.name, e.type, COUNT(t.id) AS cnt
            FROM entities e
            LEFT JOIN triples t ON t.subject = e.id AND t.valid_to IS NULL
            GROUP BY e.id
            HAVING cnt > 0
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (max_entities,),
        ).fetchall()
        lines: list[str] = []
        for entity in entities:
            triples = connection.execute(
                """
                SELECT t.predicate, COALESCE(e.name, t.object) AS object_display
                FROM triples t
                LEFT JOIN entities e ON t.object = e.id
                WHERE t.subject = ? AND t.valid_to IS NULL
                ORDER BY t.predicate
                """,
                (entity["id"],),
            ).fetchall()
            parts = [
                f"{row['predicate']}={row['object_display']}"
                if row["predicate"] in VALUE_PREDICATES
                else f"{row['predicate']} → {row['object_display']}"
                for row in triples
            ]
            if parts:
                lines.append(f"- {entity['name']} ({entity['type']}): {', '.join(parts)}")
        return "\n".join(lines)
    except sqlite3.Error as exc:
        raise ValueError("knowledge graph database schema is invalid") from exc
    finally:
        connection.close()


def repair_memory(data_dir: Path, config_path: Path) -> RepairOutcome:
    from flowly.memory.summary import (
        SENTINEL_END,
        SENTINEL_START,
        extract_manual_content,
        render_generated_block,
        splice_generated_block,
    )

    snapshot = read_config_snapshot(config_path)
    if snapshot.config is None:
        raise ValueError("memory regeneration requires a valid config")
    configured_workspace = Path(snapshot.config.agents.defaults.workspace).expanduser()
    if configured_workspace.is_symlink():
        raise ValueError(f"workspace is symbolic: {configured_workspace}")
    workspace = configured_workspace.resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace is unavailable: {workspace}")
    memory_dir = workspace / "memory"
    memory_path = memory_dir / "MEMORY.md"
    if memory_dir.is_symlink() or (memory_dir.exists() and not memory_dir.is_dir()):
        raise ValueError(f"memory directory is unsafe: {memory_dir}")
    if memory_path.is_symlink() or (memory_path.exists() and not memory_path.is_file()):
        raise ValueError(f"refusing to regenerate symbolic/non-file memory: {memory_path}")

    governance_path = data_dir / "memory_governance.sqlite3"
    items = _read_memory_items(governance_path)
    kg_summary = _read_kg_summary(data_dir / "knowledge_graph.sqlite3")
    existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    block = render_generated_block(items, kg_summary=kg_summary)
    content = splice_generated_block(existing, block)
    expected_manual = extract_manual_content(existing).rstrip("\n")

    def validate(candidate: Path) -> None:
        candidate_text = candidate.read_text(encoding="utf-8")
        if candidate_text != content:
            raise ValueError("MEMORY.md candidate changed after generation")
        if candidate_text.count(SENTINEL_START) != 1 or candidate_text.count(SENTINEL_END) != 1:
            raise ValueError("MEMORY.md candidate has invalid generated sentinels")
        if extract_manual_content(candidate_text).rstrip("\n") != expected_manual:
            raise ValueError("MEMORY.md candidate changed manual content")

    memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = _transactional_install(memory_path, content.encode(), validate)
    changed = (memory_path, backup) if backup is not None else (memory_path,)
    return RepairOutcome(
        "memory_regenerate",
        f"Regenerated MEMORY.md from {len(items)} active non-secret item(s)",
        changed,
    )
