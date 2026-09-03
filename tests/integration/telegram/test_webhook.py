"""The bot's webhook, against a real database.

Two things are under test: that `/start` and `/stop` do what they say to
stored state, and that an unauthenticated delivery does nothing at all —
the second being the more important, because this endpoint is on the
public internet and the only thing separating a real update from a forged
one is the secret token.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from infra.db.schema.telegram import telegram_subscribers
from infra.deploy.config import profile_for
from packages.config.environment import Environment
from services.telegram.app import (
    SECRET_TOKEN_HEADER,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WebhookSecretMissing,
    create_app,
)
from services.telegram.messages import START_TEXT, STOP_TEXT
from tests.integration.telegram.conftest import WEBHOOK_SECRET_VALUE, StubSecrets

CHAT = 987654321


@pytest.fixture
def client(committing_engine: Engine, secrets: StubSecrets, security) -> TestClient:
    return TestClient(create_app(committing_engine, security=security, secrets=secrets))


def _update(text: str, chat_id: int = CHAT) -> dict:
    return {"update_id": 1, "message": {"message_id": 2, "chat": {"id": chat_id}, "text": text}}


def _post(client: TestClient, body: dict, *, secret: str | None = WEBHOOK_SECRET_VALUE):
    headers = {} if secret is None else {SECRET_TOKEN_HEADER: secret}
    return client.post(WEBHOOK_PATH, json=body, headers=headers)


def _row(engine: Engine, chat_id: int):
    with engine.connect() as connection:
        return connection.execute(
            select(telegram_subscribers).where(telegram_subscribers.c.chat_id == chat_id)
        ).one_or_none()


def test_start_from_a_new_chat_creates_a_subscriber(client, committing_engine):
    chat = CHAT + 1

    response = _post(client, _update("/start", chat))

    assert response.status_code == 200
    row = _row(committing_engine, chat)
    assert row is not None
    assert row.unsubscribed_at is None


def test_start_answers_the_webhook_with_the_reply_instead_of_calling_the_api(client):
    """Which is what lets this service hold no bot token at all.

    Telegram performs the `sendMessage` named in the webhook response, so
    a compromise of this public service cannot send messages as the bot.
    """
    body = _post(client, _update("/start", CHAT + 2)).json()

    assert body["method"] == "sendMessage"
    assert body["chat_id"] == CHAT + 2
    assert body["text"] == START_TEXT


def test_start_twice_does_not_duplicate_the_subscriber(client, committing_engine):
    """Telegram's clients make it trivial to send twice, and a user will.

    The row count is asserted rather than the response, because a
    duplicate would be invisible from the outside and would double every
    alert that subscriber receives.
    """
    chat = CHAT + 3

    _post(client, _update("/start", chat))
    _post(client, _update("/start", chat))

    with committing_engine.connect() as connection:
        count = connection.execute(
            select(telegram_subscribers).where(telegram_subscribers.c.chat_id == chat)
        ).all()

    assert len(count) == 1


def test_stop_marks_the_subscriber_inactive_and_keeps_the_row(client, committing_engine):
    """Kept so a resubscribe is not indistinguishable from a first arrival."""
    chat = CHAT + 4
    _post(client, _update("/start", chat))

    response = _post(client, _update("/stop", chat))

    row = _row(committing_engine, chat)
    assert response.json()["text"] == STOP_TEXT
    assert row is not None
    assert row.unsubscribed_at is not None


def test_start_after_stop_reactivates_without_losing_the_original_date(client, committing_engine):
    """`subscribed_at` records when this chat first arrived, which stays true."""
    chat = CHAT + 5
    _post(client, _update("/start", chat))
    first = _row(committing_engine, chat).subscribed_at

    _post(client, _update("/stop", chat))
    _post(client, _update("/start", chat))

    row = _row(committing_engine, chat)
    assert row.unsubscribed_at is None
    assert row.subscribed_at == first


def test_stop_from_a_chat_that_never_subscribed_is_quietly_fine(client, committing_engine):
    """A coherent thing to receive, and the correct answer is to do nothing."""
    chat = CHAT + 6

    response = _post(client, _update("/stop", chat))

    assert response.status_code == 200
    assert _row(committing_engine, chat) is None


@pytest.mark.parametrize(
    "body",
    [
        {"update_id": 1, "message": {"chat": {"id": 55}, "text": "hello"}},
        {"update_id": 1, "message": {"chat": {"id": 55}, "sticker": {"file_id": "x"}}},
        {"update_id": 1, "callback_query": {"data": "/start"}},
        {"update_id": 1},
        {},
    ],
)
def test_everything_else_is_acknowledged_rather_than_errored(client, body):
    """Telegram *retries* a webhook that answers with an error.

    Returning 4xx for a sticker would turn one sticker into a retry loop
    against a public endpoint.
    """
    response = _post(client, body)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_malformed_body_is_ignored_rather_than_400(client):
    response = client.post(
        WEBHOOK_PATH,
        content=b"not json at all",
        headers={SECRET_TOKEN_HEADER: WEBHOOK_SECRET_VALUE, "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_delivery_with_no_secret_token_changes_nothing(client, committing_engine):
    """The forgery this endpoint would otherwise be wide open to.

    Anyone who learned the URL could subscribe arbitrary chat ids —
    making ARGUS's bot message strangers — or unsubscribe real ones.
    """
    chat = CHAT + 7

    response = _post(client, _update("/start", chat), secret=None)

    assert response.status_code == 401
    assert _row(committing_engine, chat) is None


def test_a_delivery_with_the_wrong_secret_token_changes_nothing(client, committing_engine):
    chat = CHAT + 8

    response = _post(client, _update("/start", chat), secret="not-the-secret")

    assert response.status_code == 401
    assert _row(committing_engine, chat) is None


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_a_reachable_deployment_refuses_to_start_without_a_webhook_secret(
    committing_engine, security, environment
):
    """Not a warning, and not a degraded mode.

    A public endpoint that authenticates when configured and accepts
    everything when not is one missing environment variable away from
    being open — and the missing variable is invisible until somebody
    finds the URL. Staging as well as production: a staging URL is on the
    internet too.
    """
    with pytest.raises(WebhookSecretMissing, match=WEBHOOK_SECRET):
        create_app(
            committing_engine,
            security=security,
            secrets=StubSecrets({}),
            profile=profile_for(environment),
        )


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_an_empty_webhook_secret_is_refused_too(committing_engine, security, environment):
    """An empty string would compare equal to an empty header."""
    with pytest.raises(WebhookSecretMissing):
        create_app(
            committing_engine,
            security=security,
            secrets=StubSecrets({WEBHOOK_SECRET: "  "}),
            profile=profile_for(environment),
        )


def test_development_boots_without_the_secret_but_authenticates_nothing(
    committing_engine, security
):
    """The exception, and why it is not a weaker check.

    "Every service boots with only a connection string" is a property the
    rest of the deployment relies on, and this keeps it. What an unset
    secret produces in development is a *random* one — so nothing
    authenticates, rather than everything doing so, and a developer who
    wants a real delivery sets the variable.
    """
    app = create_app(
        committing_engine,
        security=security,
        secrets=StubSecrets({}),
        profile=profile_for(Environment.DEVELOPMENT),
    )
    client = TestClient(app)

    rejected = client.post(
        WEBHOOK_PATH,
        json=_update("/start", CHAT + 9),
        headers={SECRET_TOKEN_HEADER: ""},
    )

    assert rejected.status_code == 401
    assert _row(committing_engine, CHAT + 9) is None
