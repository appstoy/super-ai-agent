"""JSON logging with conservative secret redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from super_agent.observability.context import get_context

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token|credential)", re.I)


def redact(value: Any) -> Any:
    """Recursively redact values under sensitive mapping keys."""

    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Format log records as one redacted JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        context = get_context()
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": context.request_id,
            "task_id": context.task_id,
            "run_id": context.run_id,
        }
        extra = getattr(record, "structured", None)
        if isinstance(extra, Mapping):
            payload["fields"] = redact(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the package logger without changing root handlers."""

    logger = logging.getLogger("super_agent")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Emit a structured, redacted log record."""

    logger.log(level, message, extra={"structured": fields})

