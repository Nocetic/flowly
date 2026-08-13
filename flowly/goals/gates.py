"""Bounded execution of deterministic goal quality gates."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flowly.goals.models import MAX_GATE_OUTPUT_CHARS, GoalGate


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    exit_code: int
    output_tail: str
    fingerprint: str
    skipped_unchanged: bool = False


async def workspace_fingerprint(cwd: Path) -> str:
    """Hash git HEAD and full porcelain status; return empty outside git."""
    try:
        head = await _small_command(["git", "rev-parse", "HEAD"], cwd, timeout=10)
        if head[0] != 0:
            return ""
        status = await _small_command(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd,
            timeout=30,
        )
        if status[0] != 0:
            return ""
    except (OSError, asyncio.TimeoutError):
        return ""
    return hashlib.sha256(head[1] + b"\0" + status[1]).hexdigest()


async def _small_command(command: list[str], cwd: Path, *, timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return int(process.returncode or 0), stdout


class GateRunner:
    async def run(self, gate: GoalGate, *, cwd: Path) -> GateResult:
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            return GateResult(False, -1, f"[gate cwd does not exist: {cwd}]", "")

        fingerprint = await workspace_fingerprint(cwd)
        if (
            fingerprint
            and gate.last_exit_code not in (None, 0)
            and gate.last_failed_fingerprint == fingerprint
        ):
            return GateResult(
                passed=False,
                exit_code=int(gate.last_exit_code or -1),
                output_tail=gate.last_output_tail,
                fingerprint=fingerprint,
                skipped_unchanged=True,
            )

        with tempfile.TemporaryFile(mode="w+b") as output:
            process = None
            try:
                process = await asyncio.create_subprocess_shell(
                    gate.command,
                    cwd=str(cwd),
                    stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=gate.timeout_seconds)
                except asyncio.TimeoutError:
                    await _terminate_process(process)
                    output.flush()
                    tail = _read_tail(output)
                    suffix = f"\n[gate timed out after {gate.timeout_seconds}s]"
                    return GateResult(
                        False,
                        -1,
                        (tail + suffix)[-MAX_GATE_OUTPUT_CHARS:],
                        fingerprint,
                    )
                output.flush()
                tail = _read_tail(output)
                exit_code = int(process.returncode or 0)
                return GateResult(exit_code == 0, exit_code, tail, fingerprint)
            except Exception as exc:
                if process is not None and process.returncode is None:
                    await _terminate_process(process)
                return GateResult(
                    False,
                    -1,
                    f"[gate could not run: {type(exc).__name__}: {exc}]"[-MAX_GATE_OUTPUT_CHARS:],
                    fingerprint,
                )


def _read_tail(handle: object) -> str:
    stream = handle
    stream.seek(0, os.SEEK_END)  # type: ignore[attr-defined]
    size = stream.tell()  # type: ignore[attr-defined]
    stream.seek(max(0, size - MAX_GATE_OUTPUT_CHARS * 4))  # type: ignore[attr-defined]
    raw = stream.read()  # type: ignore[attr-defined]
    return raw.decode("utf-8", "replace")[-MAX_GATE_OUTPUT_CHARS:]


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows CI
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows CI
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
