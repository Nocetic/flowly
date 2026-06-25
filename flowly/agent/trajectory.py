"""Trajectory saving — export conversation turns as ShareGPT-format JSONL.

Each trajectory captures the full message exchange (user → assistant → tool
calls → assistant) for a single session turn.  Output is appended to a JSONL
file so it can be streamed into RL training pipelines or analyzed offline.

Two output files are used:
  - trajectory_samples.jsonl  — completed turns
  - failed_trajectories.jsonl — turns that errored or timed out
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default output directory
def _default_trajectory_dir() -> Path:
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "trajectories"


def convert_to_sharegpt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert Flowly session messages to ShareGPT format.

    Mapping:
        user    → human
        assistant → gpt
        system  → system
        tool    → tool
    """
    role_map = {
        "user": "human",
        "assistant": "gpt",
        "system": "system",
        "tool": "tool",
    }
    trajectory: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content:
            continue
        mapped = role_map.get(role, role)
        trajectory.append({"from": mapped, "value": content})
    return trajectory


def save_trajectory(
    messages: list[dict[str, Any]],
    model: str,
    completed: bool = True,
    output_dir: Path | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Append a trajectory entry to the appropriate JSONL file.

    Args:
        messages: Raw session messages (Flowly format).
        model: Model identifier used for this turn.
        completed: True for successful turns, False for errors/timeouts.
        output_dir: Override output directory (default: ~/.flowly/trajectories/).
        extra_metadata: Optional dict merged into the trajectory entry.
    """
    out = output_dir or _default_trajectory_dir()
    out.mkdir(parents=True, exist_ok=True)

    filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"
    filepath = out / filename

    trajectory = convert_to_sharegpt(messages)
    if not trajectory:
        return

    entry: dict[str, Any] = {
        "conversations": trajectory,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "completed": completed,
    }
    if extra_metadata:
        entry.update(extra_metadata)

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("Trajectory saved to %s (%d turns)", filepath, len(trajectory))
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)
