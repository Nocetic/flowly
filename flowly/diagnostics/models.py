"""Result and execution types for ``flowly doctor``.

The context contains snapshots rather than live runtime objects. A default
Doctor run must not invoke Flowly's self-healing config loader, credential
migration, token refresh, network clients, or service commands.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from flowly.config.schema import Config


def _default_data_dir() -> Path:
    from flowly.profile import get_flowly_home

    return get_flowly_home()


def _default_config_path() -> Path:
    return _default_data_dir() / "config.json"


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    FIXED = "fixed"
    SKIPPED = "skipped"
    INTERNAL = "internal"


class RepairRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class DoctorResult:
    name: str
    status: Status
    message: str
    detail: str = ""
    fixable: bool = False
    category: str = "general"
    risk: RepairRisk = RepairRisk.NONE
    repair_command: str = ""
    duration_ms: float = 0.0
    changed_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["risk"] = self.risk.value
        return payload


@dataclass
class DoctorContext:
    config_path: Path = field(default_factory=_default_config_path)
    data_dir: Path = field(default_factory=_default_data_dir)
    fix: bool = False
    read_only: bool = True
    online: bool = False
    timeout: float = 5.0
    results: list[DoctorResult] = field(default_factory=list)
    raw_config: dict[str, Any] | None = None
    config: Config | None = None
    config_error: str = ""
    duplicate_keys: tuple[str, ...] = ()
    current_category: str = "general"

    def record(self, result: DoctorResult) -> None:
        if result.category == "general":
            result.category = self.current_category
        self.results.append(result)

    def ok(self, name: str, message: str, detail: str = "") -> None:
        self.record(DoctorResult(name=name, status=Status.OK, message=message, detail=detail))

    def warn(
        self,
        name: str,
        message: str,
        detail: str = "",
        *,
        fixable: bool = False,
        risk: RepairRisk = RepairRisk.NONE,
        repair_command: str = "",
    ) -> None:
        self.record(
            DoctorResult(
                name=name,
                status=Status.WARN,
                message=message,
                detail=detail,
                fixable=fixable,
                risk=risk,
                repair_command=repair_command,
            )
        )

    def error(
        self,
        name: str,
        message: str,
        detail: str = "",
        *,
        fixable: bool = False,
        risk: RepairRisk = RepairRisk.NONE,
        repair_command: str = "",
    ) -> None:
        self.record(
            DoctorResult(
                name=name,
                status=Status.ERROR,
                message=message,
                detail=detail,
                fixable=fixable,
                risk=risk,
                repair_command=repair_command,
            )
        )

    def skipped(self, name: str, message: str, detail: str = "") -> None:
        self.record(
            DoctorResult(name=name, status=Status.SKIPPED, message=message, detail=detail)
        )

    def fixed(self, name: str, message: str, *, changed_paths: list[Path]) -> None:
        self.record(
            DoctorResult(
                name=name,
                status=Status.FIXED,
                message=message,
                changed_paths=[str(path) for path in changed_paths],
            )
        )

    def internal(self, name: str, exc: Exception) -> None:
        # Exception messages can contain provider request headers or malformed
        # config values. Only expose the exception class in normal output.
        self.record(
            DoctorResult(
                name=name,
                status=Status.INTERNAL,
                message=f"Check could not complete ({type(exc).__name__})",
                detail="Re-run with debug logging for a traceback.",
            )
        )


CheckFunction = Callable[[DoctorContext], None]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    category: str
    function: CheckFunction
    online_only: bool = False
