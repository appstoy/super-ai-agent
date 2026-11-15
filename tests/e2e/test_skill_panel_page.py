"""The skill panel is the discoverable way in; these pin its wiring.

The "/" menu silently assumed the user already knew the feature existed. The
panel is the entry point a first-time user can actually find, so the pieces it
depends on - a visible button, the panel markup, the tabs, the install and
remove actions - each get an assertion. A missing wire here looks like "skills
don't work" rather than "one element is gone".
"""

from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "src" / "super_agent" / "web" / "index.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_composer_has_a_visible_skill_button() -> None:
    """Discoverability is the whole point: no button, no feature."""

    page = _page()
    assert 'id="skill-btn"' in page
    assert "\u2726" in page
    # It lives inside the composer, next to the send button.
    composer = page[page.index('id="composer"') : page.index('id="slash-menu"')]
    assert "skill-btn" in composer


def test_panel_markup_exists_and_starts_closed() -> None:
    page = _page()
    assert 'id="skill-panel" hidden' in page
    assert 'id="sp-body"' in page
    assert 'id="sp-foot"' in page


def test_panel_has_installed_and_market_tabs() -> None:
    page = _page()
    assert 'data-sp-tab="installed"' in page
    assert 'data-sp-tab="market"' in page


def test_panel_has_a_search_box() -> None:
    assert 'id="sp-q"' in _page()


def test_panel_offers_select_install_and_remove() -> None:
    page = _page()
    assert "data-sp-pick" in page
    assert "data-sp-install" in page
    assert "data-sp-remove" in page


def test_removal_goes_through_the_delete_endpoint() -> None:
    """The UI must not pretend to remove a skill without touching the disk."""

    page = _page()
    assert 'method: "DELETE"' in page
    assert '"/api/skills/"' in page


def test_install_switches_to_the_installed_tab() -> None:
    """Landing back on the market list after an install reads as "nothing
    happened"; the new skill has to be visible."""

    page = _page()
    install = page[page.index("async function installFromMarket") :]
    install = install[: install.index("\nfunction closeSlash")]
    assert 'S.spTab = "installed"' in install


def test_install_failure_is_shown_in_the_panel_not_only_a_toast() -> None:
    """Magpie's index can point at a folder that no longer exists; the message
    names the skills the repo does have, which a 3-second toast cannot convey."""

    page = _page()
    assert "S.spError" in page
    assert "sp-error" in page
    install = page[page.index("async function installFromMarket") :]
    install = install[: install.index("\nfunction closeSlash")]
    assert "S.spError" in install


def test_escape_closes_the_panel() -> None:
    page = _page()
    assert 'e.key === "Escape"' in page
    assert "closeSkillPanel()" in page


def test_button_badge_tracks_the_selection() -> None:
    page = _page()
    assert 'id="skill-count"' in page
    render = page[page.index("function renderSkills()") :]
    render = render[: render.index("function toggleSkill")]
    assert "skill-count" in render


def test_opening_one_picker_closes_the_other() -> None:
    """The panel and the "/" menu overlap; letting both open at once hides one."""

    page = _page()
    slash = page[page.index("function renderSlash()") :]
    slash = slash[: slash.index("function closeSlash")]
    assert 'closeSkillPanel()' in slash


def test_page_still_has_no_escaped_chinese() -> None:
    page = _page()
    assert "\\u4f60" not in page.lower()
    assert "\u4F60\u60F3\u8BA9\u5B83\u505A\u4EC0\u4E48" in page


def test_panel_has_three_tabs_including_other_agents() -> None:
    """The agent tab is where most users will get their first skill from."""

    page = _page()
    assert 'data-sp-tab="installed"' in page
    assert 'data-sp-tab="agents"' in page
    assert 'data-sp-tab="market"' in page


def test_panel_has_a_drop_zone_and_file_pickers() -> None:
    page = _page()
    assert 'id="sp-drop"' in page
    assert 'id="sp-file"' in page          # archive picker
    assert 'id="sp-dir"' in page           # folder picker
    assert "webkitdirectory" in page       # lets the picker choose a folder
    assert 'id="sp-add-link"' in page


def test_page_wide_drop_overlay_exists() -> None:
    """Dragging anywhere should be accepted, like a file manager."""

    page = _page()
    assert 'id="page-drop"' in page
    assert 'document.addEventListener("drop"' in page
    # dragenter/dragover are registered in one loop to show the overlay.
    assert '"dragenter", "dragover"' in page
    assert "setDropActive(true)" in page


def test_folder_drop_does_not_need_a_client_zip_library() -> None:
    """Packing in the browser would need JSZip or a CDN, breaking offline use."""

    page = _page()
    assert "JSZip" not in page
    assert "/api/skills/upload-files" in page


def test_agent_sub_filter_pills_are_rendered() -> None:
    page = _page()
    assert "data-agent-filter" in page
    assert "agentPillsHtml" in page


def test_import_goes_through_the_agent_endpoint() -> None:
    page = _page()
    assert '"/api/skills/import"' in page
    assert "from_agent: true" in page


def test_agents_tab_loads_before_you_open_it() -> None:
    """The tab badge is the hint that importing is worth doing."""

    page = _page()
    opener = page[page.index("function openSkillPanel"):]
    opener = opener[: opener.index("function closeSkillPanel")]
    assert "loadAgentSkills" in opener


def test_import_button_markup_has_no_stray_separator() -> None:
    """A literal '|' in the generated attribute produced 'claude|' and made
    every agent import fail with 'unknown agent'."""

    page = _page()
    block = page[page.index("function agentRowHtml"):]
    block = block[: block.index("function marketRowHtml")]
    assert "esc(item.agent) + '|" not in block
    assert 'esc(item.agent) + \'" data-sp-name="' in block


def test_unpicking_repaints_every_picker() -> None:
    """Cancelling from 本次套用 must clear the panel rows and badge too."""

    page = _page()
    block = page[page.index("function renderPicked"):]
    block = block[: block.index("function renderSkills")]
    assert "renderSkillPanel()" in block
    assert "renderSlash()" in block


def test_toggling_from_the_panel_updates_all_pickers() -> None:
    page = _page()
    block = page[page.index("function toggleSkill"):]
    block = block[: block.index("function slashQuery")]
    assert "renderSkills()" in block
    assert "renderSkillPanel()" in block
    assert "renderSlash()" in block
