"""Fixtures for Module 27: a real database, a fake Telegram.

The database is real because everything under test is stored state — a
subscriber row, an `unsubscribed_at` that dispatch reads, and a unique
constraint that is the entire idempotency mechanism. A stubbed connection
would prove none of it.

Telegram is fake because what matters is *how many* messages were sent
and *to whom*, which a recording sender answers exactly and a live API
never could.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from core.data_validation.calendar import is_trading_day
from core.live_scanner.schedule import as_of_for, scan_date_for
from core.market_state.thresholds import publish_target_model_version
from core.model_validation_evaluation.validation.replay import ModuleConfigs
from data.canonical_model.exchanges import CanonicalExchange
from data.normalization.identity import SecurityIdentityResolver
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state_transitions
from infra.security.config import SecurityConfig
from packages.config.secrets import SecretNotFoundError, SecretsProvider
from services.telegram.client import SendOutcome, SendResult
from tests.integration.db.conftest import (  # noqa: F401
    admin_url,
    engine,
    migrated_database,
)

#: A session each test gets to itself. See the `session` fixture: the
#: transitions table is append-only guarded, so a test cannot clean up
#: after itself and isolation has to come from the date instead.
_SESSION_POOL = [
    day.date()
    for day in pd.bdate_range("2026-01-05", periods=400, tz="UTC")
    if is_trading_day(day.date())
]
_ALLOCATED = itertools.count()

WEBHOOK_SECRET_VALUE = "test-webhook-secret"  # noqa: S105 - a fixture


class StubSecrets(SecretsProvider):
    """In-memory secrets, so no `.env` and no real credential is needed."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        from services.telegram.app import WEBHOOK_SECRET

        self._values = {WEBHOOK_SECRET: WEBHOOK_SECRET_VALUE} if values is None else values

    def get_secret(self, key: str) -> str:
        try:
            return self._values[key]
        except KeyError:
            raise SecretNotFoundError(key) from None


class RecordingSender:
    """A `MessageSender` that records instead of sending.

    `outcomes` maps a chat id to the result it should produce, so a test
    can make one subscriber blocked or one send fail without touching the
    client's own classification, which is tested separately.
    """

    def __init__(self, outcomes: dict[int, SendResult] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._outcomes = outcomes or {}

    def send_message(self, chat_id: int, text: str) -> SendResult:
        self.sent.append((chat_id, text))
        return self._outcomes.get(chat_id, SendResult(SendOutcome.DELIVERED))

    @property
    def chats(self) -> list[int]:
        return [chat_id for chat_id, _ in self.sent]


@pytest.fixture
def session() -> tuple:
    """A trading session no other test in this run touches, and a `now` for it.

    `market_state_transitions` is append-only guarded — Module 03 rejects
    DELETE on it — so a test that writes a transition cannot remove it,
    and the session-scoped database is shared. Isolating by *date* is
    therefore the only isolation available, and it is the honest one:
    dispatch's date bracket is a real part of what is under test, so
    every test also proves it does not leak into a neighbour's session.

    `now` is one minute past the session's own cutoff, which is what
    makes `scan_date_for(now)` name it.
    """
    scan_date = _SESSION_POOL[next(_ALLOCATED)]
    now = as_of_for(scan_date) + timedelta(minutes=1)
    assert scan_date_for(now) == scan_date
    return scan_date, now


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:  # noqa: F811
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def committing_engine(engine: Engine) -> Engine:  # noqa: F811
    """The engine itself, for tests driving the real dispatch path.

    Dispatch opens its own transaction per delivery on purpose — that is
    what makes a crash mid-run leave the already-sent rows behind — so it
    cannot be driven inside one rolled-back outer transaction. These
    tests work only on rows they created.
    """
    return engine


@pytest.fixture
def secrets() -> StubSecrets:
    return StubSecrets()


@pytest.fixture
def security() -> SecurityConfig:
    return SecurityConfig()


@pytest.fixture
def transition_factory(engine: Engine) -> Callable[..., UUID]:  # noqa: F811
    """Register a security and record one state transition for it.

    Writes the transition row directly rather than running a scan: what
    is under test is what dispatch does with Module 10's log, and driving
    a full classification to produce one row would make the test about
    Module 10.
    """

    def _make(
        ticker: str,
        *,
        at: datetime,
        to_state: MarketState = MarketState.BREAKOUT_READY,
        from_state: MarketState | None = MarketState.BREAKOUT_WATCH,
        name: str | None = None,
    ) -> tuple[UUID, UUID]:
        stamp = datetime.now(UTC).timestamp()
        moment = at

        with engine.begin() as conn:
            model_version = publish_target_model_version(
                conn, ModuleConfigs().market_state, description="module27 test"
            )
            resolver = SecurityIdentityResolver(conn)
            security_id = resolver.register(
                f"{ticker}{int(stamp * 1000) % 100000}",
                exchange=CanonicalExchange.NASDAQ,
                valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                name=name if name is not None else f"{ticker} Test Corp.",
            )
            transition_id = conn.execute(
                market_state_transitions.insert()
                .values(
                    security_id=security_id,
                    from_state=from_state.value if from_state else None,
                    to_state=to_state.value,
                    transition_time=moment,
                    duration_in_prior_state=None if from_state is None else "3 days",
                    confidence=None,
                    evidence={},
                    target_model_version_id=model_version,
                )
                .returning(market_state_transitions.c.id)
            ).scalar_one()

        return security_id, transition_id

    return _make
