"""Deterministic Inline/Spill context service."""

from __future__ import annotations

from super_agent.errors import ValidationError
from super_agent.ports.context import (
    BuiltContext,
    ContextBuildRequest,
    ContextManifestEntry,
    ContextPlacement,
)


class InlineSpillContextService:
    """Place authorized candidates within a strict token budget."""

    def __init__(self, *, chars_per_token: int = 4) -> None:
        if chars_per_token < 1:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    def build(self, request: ContextBuildRequest) -> BuiltContext:
        if request.token_budget < 1:
            raise ValidationError("token_budget must be positive")
        ordered = sorted(
            request.candidates,
            key=lambda candidate: (
                candidate.object_id != request.current_trigger_id,
                -candidate.priority,
                str(candidate.object_id),
            ),
        )
        used = 0
        inline: list[str] = []
        manifest: list[ContextManifestEntry] = []
        for candidate in ordered:
            estimated = self._estimate_tokens(candidate.content)
            if not candidate.authorized:
                manifest.append(
                    ContextManifestEntry(
                        candidate.object_id,
                        candidate.object_type,
                        ContextPlacement.EXCLUDED,
                        estimated,
                        "not authorized",
                        candidate.read_ref,
                    )
                )
                continue
            if used + estimated <= request.token_budget:
                inline.append(candidate.content)
                used += estimated
                manifest.append(
                    ContextManifestEntry(
                        candidate.object_id,
                        candidate.object_type,
                        ContextPlacement.INLINE,
                        estimated,
                        "fits budget",
                        candidate.read_ref,
                    )
                )
                continue
            manifest.append(
                ContextManifestEntry(
                    candidate.object_id,
                    candidate.object_type,
                    ContextPlacement.SPILL,
                    estimated,
                    "exceeds remaining budget",
                    candidate.read_ref or f"context:{candidate.object_id}",
                )
            )
        return BuiltContext("\n\n".join(inline), tuple(manifest), used)

    def _estimate_tokens(self, content: str) -> int:
        return max(1, (len(content) + self._chars_per_token - 1) // self._chars_per_token)

