"""Process-level ownership policy for Flowly runtime features.

Named profiles are isolated agent runtimes, not additional owners of
installation-wide services.  Keeping this decision in one small immutable
policy prevents feature registration, RPC exposure, and background services
from drifting apart as new surfaces are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeRole(str, Enum):
    """The authority a process has within one Flowly installation."""

    PRIMARY = "primary"
    NAMED_PROFILE = "named_profile"
    PROFILE_TASK_WORKER = "profile_task_worker"


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Installation-scoped capabilities owned by one runtime process."""

    role: RuntimeRole
    profile_name: str
    owns_shared_board: bool
    owns_flowlets: bool
    owns_channels: bool
    owns_profile_host: bool
    dispatches_profile_tasks: bool
    accepts_profile_tasks: bool

    @property
    def is_primary(self) -> bool:
        return self.role is RuntimeRole.PRIMARY


def resolve_runtime_capabilities(
    *,
    profile_name: str | None = None,
    role: RuntimeRole | str | None = None,
) -> RuntimeCapabilities:
    """Resolve the process authority from stable profile identity.

    The primary/default profile owns installation-wide services. Named
    profiles keep their own sessions, memory, tools, and routines, while
    consuming shared services through authenticated brokers. The explicit
    task-worker role is reserved for a named profile invocation claimed by the
    shared dispatcher; it never gains service ownership.
    """

    if profile_name is None:
        from flowly.profile import current_profile_name

        profile_name = current_profile_name()
    normalized_name = str(profile_name or "default").strip() or "default"

    if role is None:
        resolved_role = (
            RuntimeRole.PRIMARY
            if normalized_name == "default"
            else RuntimeRole.NAMED_PROFILE
        )
    else:
        resolved_role = RuntimeRole(role)

    if resolved_role is RuntimeRole.PRIMARY and normalized_name != "default":
        raise ValueError("Only the default profile can own primary runtime services.")
    if resolved_role is RuntimeRole.PROFILE_TASK_WORKER and normalized_name == "default":
        raise ValueError("A profile task worker requires a named profile.")

    primary = resolved_role is RuntimeRole.PRIMARY
    return RuntimeCapabilities(
        role=resolved_role,
        profile_name=normalized_name,
        owns_shared_board=primary,
        owns_flowlets=primary,
        owns_channels=primary,
        owns_profile_host=primary,
        dispatches_profile_tasks=primary,
        accepts_profile_tasks=not primary,
    )
