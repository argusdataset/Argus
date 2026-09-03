"""The two menus over a real database and a real webhook.

The property that matters most here is negative: the Alerts menu must
not be a second subscription mechanism. Its buttons reach the same
`subscribers` functions `/start` and `/stop` do, and its status line is
read from `telegram_subscribers` rather than from anything the menu
keeps for itself. Both are asserted by driving one and observing the
other.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from infra.db.schema.telegram import telegram_subscribers
from services.telegram import subscribers
from services.telegram.app import SECRET_TOKEN_HEADER, WEBHOOK_PATH, create_app
from services.telegram.menus import (
    ALERTS_BUTTON,
    STATS_BUTTON,
    SUBSCRIBE_BUTTON,
    UNSUBSCRIBE_BUTTON,
)
from services.telegram.stats import StatsSnapshot, StatsState
from tests.integration.telegram.conftest import WEBHOOK_SECRET_VALUE, StubSecrets

CHAT = 555_000_001


class StubStats:
    """A stats client that answers without HTTP. See test_statistics.py."""

    def __init__(self, snapshot: StatsSnapshot | None = None) -> None:
        self.snapshot = snapshot or StatsSnapshot(state=StatsState.NOT_PUBLISHED)
        self.calls = 0

    def get(self, path: str):  # pragma: no cover - read_statistics is stubbed instead
        raise AssertionError("unexpected HTTP call")


@pytest.fixture
def client(committing_engine: Engine, secrets: StubSecrets, security, monkeypatch) -> TestClient:
    stub = StubStats()
    monkeypatch.setattr("services.telegram.app.read_statistics", lambda _client: stub.snapshot)
    app = create_app(committing_engine, security=security, secrets=secrets, stats_client=stub)
    return TestClient(app)


def _press(client: TestClient, text: str, chat_id: int = CHAT) -> dict:
    response = client.post(
        WEBHOOK_PATH,
        json={"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}},
        headers={SECRET_TOKEN_HEADER: WEBHOOK_SECRET_VALUE},
    )
    assert response.status_code == 200
    return response.json()


def _labels(body: dict) -> list[str]:
    markup = body.get("reply_markup") or {}
    return [button["text"] for row in markup.get("keyboard", []) for button in row]


def _row(engine: Engine, chat_id: int):
    with engine.connect() as connection:
        return connection.execute(
            select(telegram_subscribers).where(telegram_subscribers.c.chat_id == chat_id)
        ).one_or_none()


def test_start_shows_the_two_menu_keyboard(client):
    body = _press(client, "/start", CHAT + 1)

    assert body["method"] == "sendMessage"
    assert _labels(body) == [ALERTS_BUTTON, STATS_BUTTON]


def test_the_keyboard_travels_on_the_reply_rather_than_a_second_api_call(client):
    """Which is the whole reason this is a reply keyboard and not an inline one.

    A webhook response carries exactly one method call. An inline
    keyboard's buttons produce callback queries that must each be
    answered, so answering *and* replying would need two calls — and the
    second would need the bot token this service deliberately does not
    hold.
    """
    body = _press(client, "/start", CHAT + 2)

    assert set(body) == {"method", "chat_id", "text", "reply_markup"}


def test_the_alerts_menu_reflects_the_stored_state_not_a_copy_of_it(client, committing_engine):
    """Driven by writing the table directly and reading the menu.

    If the menu kept its own notion of "subscribed", this would show the
    stale one — which is exactly the failure a second copy of state
    produces and the reason there is not one.
    """
    chat = CHAT + 3

    unsubscribed = _press(client, ALERTS_BUTTON, chat)
    assert "not subscribed" in unsubscribed["text"]
    assert _labels(unsubscribed) == [SUBSCRIBE_BUTTON, "⬅️ Back"]

    with committing_engine.begin() as connection:
        subscribers.subscribe(connection, chat)

    subscribed = _press(client, ALERTS_BUTTON, chat)
    assert "not subscribed" not in subscribed["text"]
    assert _labels(subscribed) == [UNSUBSCRIBE_BUTTON, "⬅️ Back"]


def test_the_subscribe_button_writes_the_same_row_start_does(client, committing_engine):
    """The menu is a UI over the existing path, not a second mechanism."""
    chat = CHAT + 4

    _press(client, SUBSCRIBE_BUTTON, chat)

    row = _row(committing_engine, chat)
    assert row is not None
    assert row.unsubscribed_at is None


def test_the_unsubscribe_button_marks_the_same_row_stop_does(client, committing_engine):
    chat = CHAT + 5
    _press(client, "/start", chat)

    _press(client, UNSUBSCRIBE_BUTTON, chat)

    row = _row(committing_engine, chat)
    assert row is not None
    assert row.unsubscribed_at is not None


def test_subscribing_from_the_menu_leaves_you_in_the_menu(client):
    """Whereas `/start` — the way in — shows the main menu.

    Same action, different place, so a different keyboard comes back.
    That is the only difference between the two commands.
    """
    chat = CHAT + 6

    from_menu = _press(client, SUBSCRIBE_BUTTON, chat)
    assert _labels(from_menu) == [UNSUBSCRIBE_BUTTON, "⬅️ Back"]

    from_start = _press(client, "/start", chat + 100)
    assert _labels(from_start) == [ALERTS_BUTTON, STATS_BUTTON]


def test_back_returns_to_the_main_menu(client):
    body = _press(client, "⬅️ Back", CHAT + 7)

    assert _labels(body) == [ALERTS_BUTTON, STATS_BUTTON]


def test_the_statistics_button_answers_with_text_and_the_main_menu(client):
    body = _press(client, STATS_BUTTON, CHAT + 8)

    assert "📊" in body["text"]
    assert _labels(body) == [ALERTS_BUTTON, STATS_BUTTON]


def test_a_subscription_made_by_command_is_visible_to_the_menu(client, committing_engine):
    """The two surfaces over one table, from the other direction."""
    chat = CHAT + 9
    _press(client, "/start", chat)

    body = _press(client, ALERTS_BUTTON, chat)

    assert "not subscribed" not in body["text"]
    assert _row(committing_engine, chat).unsubscribed_at is None
