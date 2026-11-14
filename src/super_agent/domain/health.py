"""Health response types owned by the domain layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Small, stable health payload for local and deployment checks."""

    status: str
    service: str
    version: str

    def to_dict(self) -> dict[str, str]:
        """Return a serialization-friendly representation."""

        return asdict(self)

