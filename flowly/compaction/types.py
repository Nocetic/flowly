"""Types for compaction system."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class MemoryFlushConfig:
    """Configuration for pre-compaction memory flush."""

    enabled: bool = True
    soft_threshold_tokens: int = 8000
    prompt: str = (
        "Pre-compaction memory flush. "
        "Store durable memories now (use memory/YYYY-MM-DD.md). "
        "If nothing to store, reply with NO_REPLY."
    )
    system_prompt: str = (
        "Pre-compaction memory flush turn. "
        "The session is near auto-compaction; capture durable memories to disk. "
        "You may reply, but usually NO_REPLY is correct."
    )


@dataclass
class MicrocompactConfig:
    """Configuration for pre-compaction tool result truncation."""

    enabled: bool = True
    # Keep the N most recent tool results at full length
    keep_recent_full: int = 5
    # Truncate older tool results to this many characters
    truncate_chars: int = 200


@dataclass
class KeepRecentConfig:
    """Configuration for keeping recent messages after compaction."""

    enabled: bool = True
    # Minimum tokens of recent messages to preserve
    min_tokens: int = 5_000
    # Minimum number of user/assistant text messages to preserve
    min_messages: int = 3
    # Maximum tokens of recent messages to preserve (hard cap)
    max_tokens: int = 20_000


@dataclass
class CompactionConfig:
    """Configuration for compaction."""

    # Mode: "default" (simple) or "safeguard" (robust with pruning)
    mode: Literal["default", "safeguard"] = "safeguard"

    # Reserve tokens for output + prompts (floor).
    # Higher value = compaction triggers earlier (more room for response).
    # 32K reserve on 128K context = triggers at 75% full (96K).
    reserve_tokens_floor: int = 32_000

    # Max share of context window for history (safeguard mode).
    # 0.6 keeps more history context than 0.5 (less aggressive pruning).
    max_history_share: float = 0.6

    # Context window size (model-specific, will be auto-detected)
    context_window: int = 128_000

    # Memory flush settings
    memory_flush: MemoryFlushConfig = field(default_factory=MemoryFlushConfig)

    # Microcompaction: truncate old tool results before hitting compaction threshold
    microcompact: MicrocompactConfig = field(default_factory=MicrocompactConfig)

    # Keep recent messages after compaction (not just summary)
    keep_recent: KeepRecentConfig = field(default_factory=KeepRecentConfig)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    summary: str
    tokens_before: int
    tokens_after: int
    messages_removed: int
    dropped_chunks: int = 0
    dropped_messages: int = 0
    dropped_tokens: int = 0
    # Recent messages preserved verbatim after compaction
    kept_messages: list = field(default_factory=list)


# Constants (matching moltbot)
BASE_CHUNK_RATIO = 0.4
MIN_CHUNK_RATIO = 0.15
SAFETY_MARGIN = 1.2  # 20% buffer for token estimation inaccuracy

DEFAULT_SUMMARY_FALLBACK = "No prior history."
DEFAULT_PARTS = 2

MERGE_SUMMARIES_INSTRUCTIONS = (
    "Merge these partial summaries into a single cohesive summary. "
    "Preserve decisions, TODOs, open questions, and any constraints."
)

SILENT_REPLY_TOKEN = "NO_REPLY"
