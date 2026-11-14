"""Stable error categories shared by adapters and application code."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Categories used for retry, user messaging, and audit decisions."""

    CONFIGURATION = "configuration"
    EXTERNAL_DEPENDENCY = "external_dependency"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    INTERNAL = "internal"


class AgentError(Exception):
    """Base application error with a stable category and retry decision."""

    category: ErrorCategory = ErrorCategory.INTERNAL
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable


class ConfigurationError(AgentError):
    category = ErrorCategory.CONFIGURATION


class ExternalDependencyError(AgentError):
    category = ErrorCategory.EXTERNAL_DEPENDENCY
    retryable = True


class PermissionDeniedError(AgentError):
    category = ErrorCategory.PERMISSION


class AgentTimeoutError(AgentError):
    category = ErrorCategory.TIMEOUT
    retryable = True


class ValidationError(AgentError):
    category = ErrorCategory.VALIDATION


class StateTransitionError(AgentError):
    """Raised when a domain entity receives an illegal state transition."""

    category = ErrorCategory.VALIDATION


class VersionConflictError(AgentError):
    """Raised when a stale entity version attempts to write a transition."""

    category = ErrorCategory.VALIDATION

