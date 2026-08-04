"""Compaction system for context management."""

from flowly.compaction.estimator import estimate_messages_tokens, estimate_tokens
from flowly.compaction.pruning import (
    chunk_messages_by_max_tokens,
    prune_history_for_context_share,
    split_messages_by_token_share,
)
from flowly.compaction.service import CompactionService
from flowly.compaction.summarizer import (
    summarize_in_stages,
    summarize_messages,
    summarize_with_fallback,
)
from flowly.compaction.types import (
    CompactionConfig,
    CompactionResult,
    MemoryFlushConfig,
)

__all__ = [
    # Estimator
    "estimate_tokens",
    "estimate_messages_tokens",
    # Summarizer
    "summarize_messages",
    "summarize_in_stages",
    "summarize_with_fallback",
    # Pruning
    "prune_history_for_context_share",
    "split_messages_by_token_share",
    "chunk_messages_by_max_tokens",
    # Service
    "CompactionService",
    # Types
    "CompactionConfig",
    "CompactionResult",
    "MemoryFlushConfig",
]
