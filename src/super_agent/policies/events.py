"""Fast Path event filtering before model invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FilterDecision(StrEnum):
    ACCEPT = "accept"
    IGNORE = "ignore"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    source: str
    scope: str
    actor: str
    text: str
    explicit_request: bool = False


class EventFilter:
    """Apply ordered deterministic rules with a versioned policy."""

    def __init__(
        self,
        *,
        version: str = "fast-path-v1",
        allowed_sources: frozenset[str] = frozenset(),
        allowed_scopes: frozenset[str] = frozenset(),
        ignored_actors: frozenset[str] = frozenset(),
        max_text_length: int = 16 * 1024,
    ) -> None:
        self.version = version
        self.allowed_sources = allowed_sources
        self.allowed_scopes = allowed_scopes
        self.ignored_actors = ignored_actors
        self.max_text_length = max_text_length

    def decide(self, event: NormalizedEvent) -> FilterDecision:
        if not event.text.strip() or len(event.text.encode("utf-8")) > self.max_text_length:
            return FilterDecision.IGNORE
        if event.actor in self.ignored_actors:
            return FilterDecision.IGNORE
        if self.allowed_sources and event.source not in self.allowed_sources:
            return FilterDecision.IGNORE
        if self.allowed_scopes and event.scope not in self.allowed_scopes:
            return FilterDecision.IGNORE
        if event.explicit_request:
            return FilterDecision.ACCEPT
        return FilterDecision.CANDIDATE

