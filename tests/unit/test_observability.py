import io
import json
import logging

from super_agent.errors import (
    AgentTimeoutError,
    ConfigurationError,
    ErrorCategory,
    ExternalDependencyError,
)
from super_agent.observability.context import get_context, set_context
from super_agent.observability.logging import JsonFormatter, log


def test_error_categories_and_retry_defaults() -> None:
    assert ConfigurationError("bad").category is ErrorCategory.CONFIGURATION
    assert not ConfigurationError("bad").retryable
    assert ExternalDependencyError("retry").retryable
    assert AgentTimeoutError("retry").category is ErrorCategory.TIMEOUT


def test_context_updates_only_requested_fields() -> None:
    set_context(request_id="req-1", task_id="task-1")
    updated = set_context(run_id="run-1")

    assert updated.request_id == "req-1"
    assert updated.task_id == "task-1"
    assert updated.run_id == "run-1"
    assert get_context() == updated


def test_json_formatter_redacts_sensitive_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test-observability")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    set_context(request_id="req-2", task_id="task-2", run_id="run-2")

    log(logger, logging.INFO, "tool called", api_key="secret", nested={"token": "hidden"}, ok=True)

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == "req-2"
    assert payload["fields"] == {
        "api_key": "[REDACTED]",
        "nested": {"token": "[REDACTED]"},
        "ok": True,
    }
    assert "secret" not in stream.getvalue()
    assert "hidden" not in stream.getvalue()
