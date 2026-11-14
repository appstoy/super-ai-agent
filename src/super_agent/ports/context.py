"""Provider-neutral context construction contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ContextPlacement(StrEnum):
    INLINE = "inline"
    SPILL = "spill"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    """Authorized or rejected object available to a model context."""

    object_id: UUID
    object_type: str
    content: str
    priority: int
    authorized: bool = True
    read_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ContextManifestEntry:
    """Audit record for one candidate placement decision."""

    object_id: UUID
    object_type: str
    placement: ContextPlacement
    estimated_tokens: int
    reason: str
    read_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ContextBuildRequest:
    """Budgeted request to build one model context."""

    current_trigger_id: UUID
    candidates: tuple[ContextCandidate, ...]
    token_budget: int


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """Inline text plus manifest for spilled and excluded objects."""

    text: str
    manifest: tuple[ContextManifestEntry, ...]
    estimated_tokens: int


class ContextService(Protocol):
    """Port for deterministic or model-assisted context builders."""

    def build(self, request: ContextBuildRequest) -> BuiltContext:
        """Build a bounded context and manifest."""

