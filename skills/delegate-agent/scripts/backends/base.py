from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class BackendFeatures:
    resume: bool
    checkpoint_steering: bool
    interrupt_steering: bool
    progress_reporting: bool
    reasoning_selection: bool
    session_storage_owner: Literal["backend", "runtime"] = "backend"


@dataclass(frozen=True)
class BackendCapabilities:
    id: str
    name: str
    installed: bool
    usable: bool
    executable: str | None
    version: str | None
    models: tuple[dict[str, Any], ...] = ()
    reasoning_efforts: tuple[str, ...] = ()
    features: BackendFeatures = field(
        default_factory=lambda: BackendFeatures(False, False, False, False, False)
    )
    warnings: tuple[str, ...] = ()
    error: str | None = None
    models_dynamic: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "installed": self.installed,
            "usable": self.usable,
            "executable": self.executable,
            "version": self.version,
            "models": list(self.models),
            "reasoning_efforts": list(self.reasoning_efforts),
            "features": self.features.__dict__,
            "warnings": list(self.warnings),
            "error": self.error,
            "models_dynamic": self.models_dynamic,
        }


@dataclass(frozen=True)
class ResolutionRequest:
    roles: dict[str, dict[str, Any]]
    role_id: str
    complexity: str | None = None
    model: str | None = None
    model_policy: str | None = None
    reasoning: str | None = None
    sandbox: str | None = None


@dataclass(frozen=True)
class LaunchContext:
    metadata: dict[str, Any]
    mailbox: Path
    prompt: str
    mcp_script: Path


@dataclass(frozen=True)
class ResumeContext(LaunchContext):
    session_id: str = ""


@dataclass(frozen=True)
class LaunchSpec:
    command: tuple[str, ...]
    environment: dict[str, str]
    display_command: tuple[str, ...]
    persistence_enabled: bool


@dataclass(frozen=True)
class CanonicalEvent:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ExitResult:
    status: str
    summary: str


class Backend(Protocol):
    id: str

    def probe(self, include_hidden: bool = False) -> BackendCapabilities: ...
    def resolve(self, request: ResolutionRequest) -> dict[str, Any]: ...
    def build_launch(self, context: LaunchContext) -> LaunchSpec: ...
    def build_resume(self, context: ResumeContext) -> LaunchSpec: ...
    def normalize_event(self, payload: dict[str, Any]) -> list[CanonicalEvent]: ...
    def extract_session_id(self, payload: dict[str, Any]) -> str | None: ...
    def classify_exit(self, return_code: int, cancelled: bool) -> ExitResult: ...
