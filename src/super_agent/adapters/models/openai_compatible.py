"""OpenAI-compatible synchronous ModelGateway adapter.

Real "OpenAI-compatible" endpoints disagree about how tool calls are encoded.
This adapter normalizes the common shapes into provider-neutral ``ToolCall``
values so the Worker never loses a tool request because of formatting.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

from super_agent.errors import ExternalDependencyError, ValidationError
from super_agent.ports.model import (
    ChatMessage,
    MessageRole,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from super_agent.ports.tool_names import normalize_tool_name

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_UNQUOTED_KEY = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_.-]*)\s*:')
_JS_LITERAL = re.compile(r"(?<=[:\[,])\s*(true|false|null)\b")
_JS_MAP = {"true": "True", "false": "False", "null": "None"}
_TOOL_NAME = re.compile(r"[\w.\-:]+")
_NAME_KEYS = ("name", "tool", "tool_name", "recipient_name", "function_name")
_ID_KEYS = ("id", "call_id", "tool_use_id", "tool_call_id")
_ARGS_KEYS = ("arguments", "args", "parameters", "input", "tool_input", "arguments_json")
_UNPARSED = object()


@dataclass(slots=True)
class _StreamToolCall:
    """Mutable accumulator for one tool call spread across SSE deltas."""

    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class StreamResult:
    """Complete result of one streamed completion, including tool calls."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    provider_request_id: str | None = None
    finish_reason: str = "stop"


class OpenAICompatibleGateway(ModelGateway):
    """Call an OpenAI-compatible chat completions endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: float = 60.0) -> None:
        if not base_url or not api_key or not model:
            raise ValidationError("base_url, api_key, and model are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, request_data: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request_data.model or self.model,
            "messages": [self._message(message) for message in request_data.messages],
        }
        if request_data.tools:
            payload["tools"] = self._tool_payload(request_data)
            if request_data.tool_choice and request_data.tool_choice != "auto":
                payload["tool_choice"] = request_data.tool_choice
        if request_data.temperature is not None:
            payload["temperature"] = request_data.temperature
        if request_data.max_tokens is not None:
            payload["max_tokens"] = request_data.max_tokens
        body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "super-ai-agent/0.1",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ExternalDependencyError(f"model HTTP {exc.code}: {detail[:500]}") from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ExternalDependencyError(f"model request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ExternalDependencyError("model response is not an object")
        return self._response(decoded)

    def _tool_payload(self, request_data: ModelRequest) -> list[dict[str, object]]:
        """Build API-safe tool declarations shared by complete() and streams."""

        return [
            {
                "type": "function",
                "function": {
                    "name": normalize_tool_name(tool.name),
                    "description": tool.description,
                    "parameters": (
                        tool.parameters
                        if isinstance(tool.parameters, dict)
                        else {"type": "object", "properties": {}}
                    ),
                },
            }
            for tool in request_data.tools
        ]

    def _stream_payload(self, request_data: ModelRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request_data.model or self.model,
            "messages": [self._message(message) for message in request_data.messages],
            "stream": True,
        }
        if request_data.tools:
            # Without this, streaming providers never emit tool_calls at all.
            payload["tools"] = self._tool_payload(request_data)
            if request_data.tool_choice and request_data.tool_choice != "auto":
                payload["tool_choice"] = request_data.tool_choice
        if request_data.temperature is not None:
            payload["temperature"] = request_data.temperature
        if request_data.max_tokens is not None:
            payload["max_tokens"] = request_data.max_tokens
        return payload

    def _open_stream(self, request_data: ModelRequest) -> Any:
        http_request = request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(self._stream_payload(request_data)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "super-ai-agent/0.1",
            },
            method="POST",
        )
        return request.urlopen(http_request, timeout=self.timeout)

    def stream(self, request_data: ModelRequest) -> StreamResult:
        """Stream text and assemble fragmented tool calls into one response.

        Providers emit a call across several SSE deltas: the first carries
        id/name and later deltas append argument fragments. ``stream_text``
        keeps yielding only text; this method returns the complete calls.
        """

        text_parts: list[str] = []
        fragments: dict[int, _StreamToolCall] = {}
        usage: ModelUsage = ModelUsage()
        provider_request_id: str | None = None
        finish_reason = "stop"
        try:
            with self._open_stream(request_data) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    data = line[5:].strip() if line.startswith("data:") else line
                    if data == "[DONE]":
                        break
                    try:
                        decoded = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ExternalDependencyError(
                            "stream response contained invalid JSON"
                        ) from exc
                    if not isinstance(decoded, dict):
                        continue
                    if decoded.get("id") and provider_request_id is None:
                        provider_request_id = str(decoded["id"])
                    raw_usage = decoded.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = ModelUsage(
                            int(raw_usage.get("prompt_tokens", 0) or 0),
                            int(raw_usage.get("completion_tokens", 0) or 0),
                            int(raw_usage.get("total_tokens", 0) or 0),
                        )
                    choices = decoded.get("choices", [])
                    if not isinstance(choices, list) or not choices:
                        continue
                    first = choices[0]
                    if not isinstance(first, dict):
                        continue
                    if isinstance(first.get("finish_reason"), str):
                        finish_reason = str(first["finish_reason"])
                    delta = first.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str):
                        text_parts.append(content)
                    elif isinstance(content, list):
                        text_parts.append(OpenAICompatibleGateway._content_text(content))
                    raw_calls = delta.get("tool_calls")
                    if isinstance(raw_calls, list):
                        OpenAICompatibleGateway._accumulate_stream_calls(raw_calls, fragments)
                    message_part = first.get("message")
                    if isinstance(message_part, dict) and isinstance(
                        message_part.get("tool_calls"), list
                    ):
                        OpenAICompatibleGateway._accumulate_stream_calls(
                            message_part["tool_calls"], fragments
                        )
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ExternalDependencyError(f"model HTTP {exc.code}: {detail[:500]}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise ExternalDependencyError(f"model stream failed: {exc}") from exc
        tool_calls = tuple(
            ToolCall(
                id=fragment.id or f"call-stream-{index}",
                name=normalize_tool_name(fragment.name),
                arguments=OpenAICompatibleGateway._parse_arguments(fragment.arguments),
            )
            for index, fragment in sorted(fragments.items())
            if fragment.name
        )
        return StreamResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )

    def stream_text(self, request_data: ModelRequest) -> Iterator[str]:
        """Yield text deltas from an OpenAI-compatible SSE response.

        Tool-call deltas are consumed (and validated) but never yielded as
        text; use :meth:`stream` when tools are requested.
        """

        fragments: dict[int, _StreamToolCall] = {}
        try:
            with self._open_stream(request_data) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    data = line[5:].strip() if line.startswith("data:") else line
                    if data == "[DONE]":
                        return
                    try:
                        decoded = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ExternalDependencyError(
                            "stream response contained invalid JSON"
                        ) from exc
                    if not isinstance(decoded, dict):
                        continue
                    choices = decoded.get("choices", [])
                    if not isinstance(choices, list) or not choices:
                        continue
                    first = choices[0]
                    if not isinstance(first, dict):
                        continue
                    delta = first.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
                    raw_calls = delta.get("tool_calls")
                    if isinstance(raw_calls, list):
                        OpenAICompatibleGateway._accumulate_stream_calls(raw_calls, fragments)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ExternalDependencyError(f"model HTTP {exc.code}: {detail[:500]}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise ExternalDependencyError(f"model stream failed: {exc}") from exc

    @staticmethod
    def _accumulate_stream_calls(
        raw_calls: list[object], fragments: dict[int, _StreamToolCall]
    ) -> None:
        """Merge one SSE delta's tool_calls into the call accumulator."""

        for position, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = raw
            raw_index = item.get("index")
            index = raw_index if isinstance(raw_index, int) else position
            fragment = fragments.setdefault(index, _StreamToolCall())
            for key in _ID_KEYS:
                if isinstance(item.get(key), str) and item[key] and not fragment.id:
                    fragment.id = str(item[key])
                    break
            function = item.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    fragment.name = fragment.name + name if fragment.name else name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    fragment.arguments += arguments
                elif isinstance(arguments, dict):
                    fragment.arguments = json.dumps(arguments, ensure_ascii=False)
            for key in _NAME_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value and not fragment.name:
                    fragment.name = value
                    break
            for key in _ARGS_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value and not fragment.arguments:
                    fragment.arguments = value
                    break
                if isinstance(value, dict) and not fragment.arguments:
                    fragment.arguments = json.dumps(value, ensure_ascii=False)
                    break

    @staticmethod
    def _message(message: ChatMessage) -> dict[str, object]:
        """Serialize one message with names consistent across tool call turns."""

        content: object = message.content
        if message.role is MessageRole.ASSISTANT and message.tool_calls and not message.content:
            # Strict providers reject an empty string alongside tool_calls.
            content = None
        result: dict[str, object] = {"role": message.role.value, "content": content}
        if message.name:
            result["name"] = normalize_tool_name(message.name)
        if message.tool_call_id:
            result["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": normalize_tool_name(call.name),
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return result

    @staticmethod
    def _response(payload: dict[str, object]) -> ModelResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ExternalDependencyError("model response did not contain choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ExternalDependencyError("model response choice is invalid")
        message = first.get("message", {})
        if not isinstance(message, dict):
            message = {}
        text = OpenAICompatibleGateway._content_text(message.get("content"))
        tool_calls = OpenAICompatibleGateway._collect_tool_calls(payload, first, message, text)
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return ModelResponse(
            text=text,
            tool_calls=tuple(tool_calls),
            usage=ModelUsage(
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                int(usage.get("total_tokens", 0) or 0),
            ),
            provider_request_id=str(payload.get("id")) if payload.get("id") else None,
            finish_reason=str(first.get("finish_reason", "stop")),
        )

    @staticmethod
    def _collect_tool_calls(
        payload: dict[str, object],
        first: dict[str, object],
        message: dict[str, object],
        text: str,
    ) -> list[ToolCall]:
        """Merge structured, legacy, content-part and textual tool calls."""

        counter = [0]
        structured: list[object] = []
        delta_first = first.get("delta")
        delta_message = message.get("delta")
        for container in (
            message.get("tool_calls"),
            payload.get("tool_calls"),
            delta_first.get("tool_calls") if isinstance(delta_first, dict) else None,
            delta_message.get("tool_calls") if isinstance(delta_message, dict) else None,
        ):
            if isinstance(container, list):
                structured.extend(container)
        legacy = message.get("function_call")
        if isinstance(legacy, dict):
            structured.append(legacy if "function" in legacy else {"function": legacy})
        content_raw = message.get("content")
        if isinstance(content_raw, list):
            structured.extend(OpenAICompatibleGateway._content_tool_uses(content_raw))

        calls: list[ToolCall] = []
        for raw in structured:
            calls.extend(OpenAICompatibleGateway._coerce_calls(raw, counter, strict=False))
        if not calls:
            # Text-embedded calls are a best-effort fallback for providers that
            # do not implement the tools API. Only explicit tool-shaped JSON is
            # accepted, so ordinary prose containing {} stays plain text.
            for candidate_text in (
                text,
                str(message.get("reasoning_content") or ""),
                str(first.get("text") or ""),
            ):
                for snippet in OpenAICompatibleGateway._json_candidates(candidate_text):
                    loaded = OpenAICompatibleGateway._loads_lenient(snippet)
                    if loaded is _UNPARSED:
                        continue
                    found = OpenAICompatibleGateway._coerce_calls(loaded, counter, strict=True)
                    if found:
                        calls.extend(found)
                        break
                if calls:
                    break
        seen: set[tuple[str, str]] = set()
        unique: list[ToolCall] = []
        for call in calls:
            key = (call.id, call.name)
            if key in seen:
                continue
            seen.add(key)
            unique.append(call)
        return unique

    @staticmethod
    def _coerce_calls(value: object, counter: list[int], *, strict: bool) -> list[ToolCall]:
        """Coerce one provider-shaped call (or a list/dict of them) to ToolCall."""

        if isinstance(value, list):
            result: list[ToolCall] = []
            for item in value:
                result.extend(OpenAICompatibleGateway._coerce_calls(item, counter, strict=strict))
            return result
        if not isinstance(value, dict):
            return []
        raw: dict[str, Any] = value
        nested = raw.get("tool_calls")
        if isinstance(nested, list):
            merged: list[ToolCall] = []
            for item in nested:
                merged.extend(OpenAICompatibleGateway._coerce_calls(item, counter, strict=strict))
            return merged
        function = raw.get("function")
        function_dict: dict[str, Any] = function if isinstance(function, dict) else {}
        name_source: dict[str, Any] = function_dict or dict(raw)
        if isinstance(function, str) and function:
            name_source = dict(raw)
            name_source["name"] = function
        name_value = ""
        for key in _NAME_KEYS:
            candidate = name_source.get(key)
            if isinstance(candidate, str) and candidate.strip():
                name_value = candidate.strip()
                break
        if not name_value:
            return []
        call_type = str(raw.get("type") or "").lower()
        has_marker = (
            "function" in raw
            or bool(function_dict)
            or call_type in {"function", "tool_use", "tool_call", "tool_calls"}
            or any(key in raw for key in _ARGS_KEYS)
            or any(key in function_dict for key in _ARGS_KEYS)
        )
        if strict and not has_marker:
            return []
        if len(name_value) > 128 or not _TOOL_NAME.fullmatch(name_value):
            return []
        call_id = ""
        for key in _ID_KEYS:
            candidate = raw.get(key) or function_dict.get(key)
            if candidate not in (None, ""):
                call_id = str(candidate)
                break
        if not call_id:
            call_id = f"call-{counter[0]}"
        counter[0] += 1
        args_raw: object = None
        for key in _ARGS_KEYS:
            if key in function_dict:
                args_raw = function_dict[key]
                break
            if key in raw:
                args_raw = raw[key]
                break
        return [
            ToolCall(
                id=call_id,
                name=normalize_tool_name(name_value),
                arguments=OpenAICompatibleGateway._parse_arguments(args_raw),
            )
        ]

    @staticmethod
    def _parse_arguments(raw: object) -> dict[str, object]:
        """Parse provider arguments without ever raising or silently losing data."""

        if isinstance(raw, dict):
            return {str(key): value for key, value in raw.items()}
        if raw is None:
            return {}
        parsed: object = raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return {}
            fences = _FENCE.findall(text)
            if fences:
                text = fences[0].strip()
            loaded = OpenAICompatibleGateway._loads_lenient(text)
            if loaded is _UNPARSED:
                start, end = text.find("{"), text.rfind("}")
                if 0 <= start < end:
                    loaded = OpenAICompatibleGateway._loads_lenient(text[start : end + 1])
            if loaded is _UNPARSED:
                return {}
            parsed = loaded
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
        if isinstance(parsed, (list, str, int, float, bool)):
            return {"input": parsed}
        return {}

    @staticmethod
    def _loads_lenient(text: str) -> object:
        """Try strict JSON, Python literals, then conservative repairs."""

        candidate = text.strip()
        if not candidate:
            return _UNPARSED
        repaired = _UNQUOTED_KEY.sub(r'\1"\2":', _TRAILING_COMMA.sub(r"\1", candidate))
        repaired = _JS_LITERAL.sub(
            lambda match: match.group(0).replace(match.group(1), _JS_MAP[match.group(1)]),
            repaired,
        )
        attempts = (
            candidate,
            _TRAILING_COMMA.sub(r"\1", candidate),
            repaired,
        )
        for attempt in attempts:
            try:
                return json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                pass
            try:
                return ast.literal_eval(attempt)
            except (ValueError, SyntaxError, TypeError, MemoryError):
                pass
        return _UNPARSED

    @staticmethod
    def _json_candidates(text: str) -> list[str]:
        """Return plausible JSON snippets embedded in prose or code fences."""

        if not text:
            return []
        candidates = [match.strip() for match in _FENCE.findall(text) if match.strip()]
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        start, end = text.find("["), text.rfind("]")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        return candidates

    @staticmethod
    def _content_text(raw: object) -> str:
        """Extract plain text from string, dict or multi-part content."""

        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            value = raw.get("text") or raw.get("content")
            return value if isinstance(value, str) else ""
        if isinstance(raw, list):
            parts: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    value = item.get("text") or item.get("content")
                    if isinstance(value, str):
                        parts.append(value)
            return "".join(parts)
        return ""

    @staticmethod
    def _content_tool_uses(raw: list[object]) -> list[dict[str, Any]]:
        """Collect Anthropic-style tool_use parts from multi-part content."""

        uses: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            part: dict[str, Any] = item
            kind = str(part.get("type") or "").lower()
            if kind in {"tool_use", "tool_call"} or (
                "name" in part and ("input" in part or "arguments" in part)
            ):
                uses.append(part)
        return uses
