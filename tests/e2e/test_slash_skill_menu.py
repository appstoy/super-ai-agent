"""The "/" skill menu: the part that decides when it opens and what it eats.

These mirror the regexes in the page exactly. They are here because the failure
mode is invisible in review and obvious in use: a slash that opens the menu in
the middle of a URL, or that leaves a stray "/rel" in the prompt after picking.
"""

from __future__ import annotations

import re

import pytest

# Kept byte-identical to src/super_agent/web/index.html (slashQuery/applySlash).
_OPEN = re.compile(r"(?:^|\s)/([^\s/]*)$")
_STRIP = re.compile(r"(^|\s)/([^\s/]*)$")


def slash_query(value: str, caret: int) -> str | None:
    """Return the text after the opening slash, or None when no menu applies."""

    match = _OPEN.search(value[:caret])
    return match.group(1) if match else None


def strip_command(value: str, caret: int) -> str:
    """Remove the /token the caret sits in, keeping everything else."""

    before, after = value[:caret], value[caret:]
    return re.sub(r"^\s+", "", _STRIP.sub(r"\1", before) + after)


@pytest.mark.parametrize(
    ("value", "caret", "expected"),
    [
        ("/", 1, ""),
        ("/re", 3, "re"),
        ("/rel", 4, "rel"),
        ("\u505A\u4E00\u4E2A\u7F51\u9875 /html", 12, "html"),
        # A slash that is not at the start of the current token is prose.
        ("\u505A\u4E00\u4E2A\u7F51\u9875 /html", 6, None),
        ("http://x.com", 12, None),
        ("a/b", 3, None),
        # Once the token is finished, the menu is gone.
        ("/html-page ", 11, None),
        ("/ ", 2, None),
        ("", 0, None),
    ],
)
def test_slash_opens_only_on_a_leading_slash_token(
    value: str, caret: int, expected: str | None
) -> None:
    assert slash_query(value, caret) == expected


@pytest.mark.parametrize(
    ("value", "caret", "expected"),
    [
        ("/re", 3, ""),
        ("\u505A\u4E00\u4E2A\u7F51\u9875 /html", 12, "\u505A\u4E00\u4E2A\u7F51\u9875 "),
        ("/html-page", 10, ""),
        ("a /b c", 3, "a b c"),
    ],
)
def test_picking_a_skill_leaves_no_command_residue(
    value: str, caret: int, expected: str
) -> None:
    """A leftover "/rel" in the prompt is worse than no menu at all."""

    assert strip_command(value, caret) == expected


def test_page_wires_the_slash_menu_to_the_composer() -> None:
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "super_agent"
        / "web"
        / "index.html"
    ).read_text(encoding="utf-8")

    # The menu must exist, be driven by typing, and own the arrow keys.
    assert 'id="slash-menu"' in page
    assert "function renderSlash" in page
    assert "function applySlash" in page
    assert "ArrowDown" in page
    assert "renderSlash()" in page
    # Opening it on load would hide the hint text for no reason.
    assert 'id="slash-menu" hidden' in page
