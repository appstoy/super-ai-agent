"""Provider-neutral tool-name normalization shared by adapters and the Worker.

OpenAI-compatible endpoints only accept ``[A-Za-z0-9_-]`` in tool names, while
registered tools often use dotted names (``file.read``).  Models additionally
echo names with prefixes (``functions.file_read``) or whitespace.  Keeping one
canonical implementation avoids adapters and the Worker disagreeing about what
a model call refers to.
"""

from __future__ import annotations

import re

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_REPEATED_UNDERSCORES = re.compile(r"_{2,}")
_PREFIXES = ("functions.", "tools.", "function.")


def normalize_tool_name(name: str) -> str:
    """Return a stable, API-safe identifier for a provider-facing tool name."""

    cleaned = (name or "").strip()
    lowered = cleaned.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    normalized = _REPEATED_UNDERSCORES.sub("_", _INVALID_CHARS.sub("_", cleaned))
    return normalized.strip("_") or "tool"


def _name_tokens(key: str) -> frozenset[str]:
    """Split a normalized tool name into comparable word tokens."""

    return frozenset(token for token in key.split("_") if token)


def resolve_tool_name(requested: str, available: dict[str, str]) -> str | None:
    """Map a model-supplied tool name onto a registered tool.

    Models routinely invent near-miss names for tools they were offered
    (``write_file`` for ``file_write``, ``list_tools`` for ``file_list``,
    ``read`` for ``file_read``). Exact matching then rejects a call the model
    fully intended, the Worker reports "tool is not registered", and the model
    gives up and pastes the artifact into chat instead. Resolving by token set
    keeps those calls working.

    ``available`` maps every accepted alias (lowercased) to the registry name.
    Returns the registry name, or ``None`` when nothing plausibly matches.
    """

    key = normalize_tool_name(requested).lower()
    if not key:
        return None
    exact = available.get(key)
    if exact is not None:
        return exact

    wanted = _name_tokens(key)
    if not wanted:
        return None

    # 1. Same token set regardless of order: write_file -> file.write.
    for alias, registry_name in available.items():
        tokens = _name_tokens(alias)
        if tokens and tokens == wanted:
            return registry_name

    # 2. One name is a superset of the other: write_file vs file_write.
    best: tuple[int, str] | None = None
    for alias, registry_name in available.items():
        tokens = _name_tokens(alias)
        if not tokens:
            continue
        if wanted.issubset(tokens) or tokens.issubset(wanted):
            overlap = len(wanted & tokens)
            if best is None or overlap > best[0]:
                best = (overlap, registry_name)
    if best is not None and best[0] > 0:
        return best[1]
    return None
