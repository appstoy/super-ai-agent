"""The task page must announce a finished task, once, and only when useful.

Browser notifications are frontend behaviour, so these tests assert the
contract the page encodes rather than executing it: the page must expose a
permission control, must gate on visibility, must announce from exactly one
place, and must not re-announce the same task. They also guard the encoding
regression that shipped once: Chinese text must be real UTF-8 in the HTML, not
literal \\uXXXX escapes rendered as text.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings


def _page(tmp_path) -> str:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'page.db'}",
        model_name="test-model",
        workspace_root=str(tmp_path / "ws"),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        return response.text


def test_chinese_is_real_text_not_escape_sequences(tmp_path) -> None:
    """A literal \\u4F60 renders as those six characters, not as 你."""

    body = _page(tmp_path)
    assert "\\u4F60" not in body
    assert "你想让它做什么" in body
    # Spot-check the copy a user actually reads.
    for phrase in ("说一句话就行", "开始", "结果", "运维"):
        assert phrase in body, phrase


def test_the_page_offers_a_notification_control(tmp_path) -> None:
    body = _page(tmp_path)
    assert 'id="notify-toggle"' in body
    assert "Notification.requestPermission" in body


def test_notification_is_gated_on_page_visibility(tmp_path) -> None:
    """A visible page already shows the card; a popup would be duplicate news."""

    body = _page(tmp_path)
    assert 'document.visibilityState === "visible"' in body
    assert "document.visibilityState !== \"visible\"" in body


def test_announcement_happens_in_one_place_and_only_once(tmp_path) -> None:
    body = _page(tmp_path)
    # One announcement function, guarded by a flag carried on the result.
    assert body.count("function announce(") == 1
    assert "_announced" in body
    # Both observers (submit poller and feed poller) go through it.
    assert body.count("announce(") >= 3
    # The old ad-hoc toast on success must be gone, or a task announces twice.
    assert 'if (r.status === "succeeded") toast("做好了")' not in body


def test_a_task_finished_before_load_does_not_notify(tmp_path) -> None:
    """History must not fire a burst of popups on every page load."""

    body = _page(tmp_path)
    assert "if (TERMINAL.includes(item.status)) item._announced = true;" in body


def test_clicking_a_notification_reveals_that_result(tmp_path) -> None:
    body = _page(tmp_path)
    assert "revealResult" in body
    # The card must be addressable by task id for the reveal to work.
    assert 'id="r-' in body
    assert "scrollIntoView" in body


def test_hidden_tab_keeps_polling_so_the_notification_is_prompt(tmp_path) -> None:
    body = _page(tmp_path)
    assert 'document.visibilityState !== "visible"' in body
    assert "loadFeed();" in body


def test_upsert_preserves_the_announced_flag(tmp_path) -> None:
    """Re-rendering replaces the object, so the flag has to be carried over.

    Without this the same finished task re-announced on every poll - a new
    object each refresh with the guard unset.
    """

    body = _page(tmp_path)
    start = body.index("function upsert(result)")
    end = body.index("function ", start + 10)
    upsert = body[start:end]
    assert "known._announced" in upsert
    assert "result._announced = true" in upsert


def test_a_given_up_task_still_tells_the_user(tmp_path) -> None:
    """If the page stops watching, the user who walked away is still waiting."""

    body = _page(tmp_path)
    start = body.index("function finish(tempId, reason)")
    end = body.index("function ", start + 10)
    finish = body[start:end]
    assert "announce(" in finish
