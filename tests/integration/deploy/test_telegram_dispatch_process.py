"""Alert dispatch as a deployed process.

Same shape as `test_ingestion_process.py`: what the wrapper refuses, what
it exits with, and — the one specific to this module — that the public
web service and the cron do not share a credential.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from infra.deploy import telegram_dispatch
from infra.deploy.cli import UnexpectedArguments
from infra.deploy.migrate import upgrade_to_head
from infra.deploy.processes import PROCESSES
from infra.deploy.railway import SECRET_VARIABLES
from packages.config.secrets import SecretNotFoundError, SecretsProvider
from services.telegram.app import WEBHOOK_SECRET
from services.telegram.client import BOT_TOKEN_SECRET


class EmptySecrets(SecretsProvider):
    def get_secret(self, key: str) -> str:
        raise SecretNotFoundError(key)


@pytest.fixture
def migrated(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def test_a_missing_bot_token_is_a_prerequisite_failure_not_a_run_failure(migrated: Engine):
    """Exit 2, the same code the scanner and ingestion use for "cannot start".

    A platform's alerting should be able to tell an unset variable from a
    Telegram outage; collapsing them means somebody is woken for the
    wrong one.
    """
    with pytest.raises(telegram_dispatch.DispatchNotReady, match=BOT_TOKEN_SECRET):
        telegram_dispatch.run_scheduled_dispatch(migrated, secrets=EmptySecrets())


def test_the_entrypoint_exits_two_when_the_token_is_missing(migrated: Engine, monkeypatch):
    monkeypatch.delenv(BOT_TOKEN_SECRET, raising=False)
    monkeypatch.setattr(telegram_dispatch, "create_db_engine", lambda **kwargs: migrated)
    monkeypatch.setattr(telegram_dispatch, "TelegramClient", _raising_client(BOT_TOKEN_SECRET))

    assert telegram_dispatch.main([]) == 2


def test_the_entrypoint_refuses_arguments_it_cannot_use():
    """The `&&` failure that already happened once, on a different service."""
    with pytest.raises(UnexpectedArguments):
        telegram_dispatch.main(["&&", "uvicorn", "infra.deploy.asgi:telegram_app"])


def test_the_process_definition_and_the_module_agree():
    assert PROCESSES["telegram_dispatch"].command() == "python -m infra.deploy.telegram_dispatch"
    assert PROCESSES["telegram_dispatch"].kind == "cron"


def test_the_public_service_and_the_cron_do_not_share_a_credential():
    """The whole reason the webhook answers with `sendMessage` instead of calling it.

    The web service is on the public internet; the cron is not. Only the
    cron holds the bot token, so a compromise of the web service cannot
    send messages as the bot. Asserted against the deployment's own
    variable map so that granting the token to the web service later
    fails here rather than going unnoticed.
    """
    assert BOT_TOKEN_SECRET in SECRET_VARIABLES["telegram_dispatch"]
    assert BOT_TOKEN_SECRET not in SECRET_VARIABLES["telegram"]

    assert WEBHOOK_SECRET in SECRET_VARIABLES["telegram"]
    assert WEBHOOK_SECRET not in SECRET_VARIABLES["telegram_dispatch"]


def test_the_dispatch_runs_after_the_scanner_on_the_same_weekdays():
    """The chain is ingestion -> scanner -> dispatch, and the order is the point.

    Asserted as inequalities between the definitions rather than as three
    literal times, so moving any one of them keeps the relationship
    checked.
    """

    def _at(name: str) -> tuple[int, int]:
        minute, hour, _, _, _ = PROCESSES[name].schedule.split()
        return int(hour), int(minute)

    assert _at("ingestion") < _at("scanner") < _at("telegram_dispatch")
    for name in ("ingestion", "scanner", "telegram_dispatch"):
        assert PROCESSES[name].schedule.split()[-1] == "1-5"


def _raising_client(key: str):
    def _factory(*args, **kwargs):
        raise SecretNotFoundError(key)

    return _factory
