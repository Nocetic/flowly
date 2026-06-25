"""Feature flags for Flowly — controls free/pro feature gating.

Usage:
    from flowly.features import is_enabled

    if is_enabled("verification_agent"):
        await spawn_verifier(...)

Flags are read from ~/.flowly/features.json (local overrides) and can be
set remotely by flowly-desktop when a user's plan is known. If no override
exists, the registry default is used.
"""

from flowly.features.flags import is_enabled, set_flag, list_flags

__all__ = ["is_enabled", "set_flag", "list_flags"]
