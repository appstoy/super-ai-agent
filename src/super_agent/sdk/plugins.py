"""Versioned plugin metadata and failure-isolated loading."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SDK_API_VERSION = "1"


class PluginKind(StrEnum):
    MODEL = "model"
    CHANNEL = "channel"
    TOOL = "tool"
    EVENT_BUS = "event_bus"


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    name: str
    version: str
    api_version: str
    kind: PluginKind
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PluginResult:
    metadata: PluginMetadata
    instance: Any | None
    loaded: bool
    error: str | None = None


PluginFactory = Callable[[], Any]


class PluginLoader:
    """Load compatible factories independently so one failure is isolated."""

    def __init__(self, *, api_version: str = SDK_API_VERSION) -> None:
        self.api_version = api_version
        self.results: list[PluginResult] = []

    def load(self, metadata: PluginMetadata, factory: PluginFactory) -> PluginResult:
        """Validate metadata, invoke a factory, and capture failures."""

        if metadata.api_version != self.api_version:
            result = PluginResult(
                metadata,
                None,
                False,
                f"incompatible api version: {metadata.api_version}",
            )
            self.results.append(result)
            return result
        try:
            instance = factory()
        except Exception as exc:
            result = PluginResult(metadata, None, False, str(exc))
        else:
            result = PluginResult(metadata, instance, True)
        self.results.append(result)
        return result

