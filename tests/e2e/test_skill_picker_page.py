"""The skill picker is part of the product, not decoration.

These assertions are cheap and catch the failure a code review misses: the
composer renders, the page loads, and the user has no way to pick a skill
because a wire got dropped between the API, the state object and the payload.
"""

from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "src" / "super_agent" / "web" / "index.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_page_has_a_skill_bar_container() -> None:
    assert 'id="skills-bar"' in _page()


def test_page_fetches_the_skill_catalogue() -> None:
    assert '"/api/skills"' in _page()


def test_selected_skills_travel_in_the_task_payload() -> None:
    """The whole feature is dead if the selection never leaves the browser."""

    page = _page()
    assert "payload.skills" in page
    assert "pickedSkills" in page


def test_skill_bar_hides_when_the_library_is_empty() -> None:
    """A picker with nothing to pick from is worse than no picker."""

    page = _page()
    assert "bar.hidden = true" in page


def test_skill_chip_reports_its_pressed_state() -> None:
    page = _page()
    assert "aria-pressed" in page
    assert "skill-chip on" in page or 'class="skill-chip' in page


def test_page_has_no_escaped_chinese() -> None:
    """A literal backslash-u sequence renders as text in the browser.

    This failure mode is invisible to code review and instantly obvious to a
    user, so it gets its own assertion.
    """

    page = _page()
    assert "\\u4f60" not in page.lower()
    assert "你想让它做什么" in page
