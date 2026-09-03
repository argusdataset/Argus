"""One evening's alerts: who gets told, once each, and what happens when a send fails.

The idempotency test is the one that matters most here. Every other
failure in this module is an annoyance; a rerun that double-sends is the
one a subscriber notices immediately and the one that would make people
turn the bot off.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select

from core.live_scanner.schedule import as_of_for
from infra.db.enums import MarketState
from infra.db.schema.telegram import telegram_alerts_sent, telegram_subscribers
from services.telegram import subscribers
from services.telegram.client import SendOutcome, SendResult
from services.telegram.dispatch import run_dispatch
from tests.integration.telegram.conftest import RecordingSender

CHAT_A = 111_000_001
CHAT_B = 111_000_002


@pytest.fixture
def subscribed(committing_engine: Engine):
    """Two active subscribers, cleaned up after the test.

    Cleaned up rather than rolled back because dispatch commits per
    delivery on purpose — the sent log surviving a mid-run crash is the
    whole point of the design.
    """
    created: list[int] = []

    def _add(*chat_ids: int) -> list[int]:
        with committing_engine.begin() as connection:
            for chat_id in chat_ids:
                subscribers.subscribe(connection, chat_id)
                created.append(chat_id)
        return list(chat_ids)

    yield _add

    with committing_engine.begin() as connection:
        connection.execute(
            telegram_alerts_sent.delete().where(telegram_alerts_sent.c.chat_id.in_(created))
        )
        connection.execute(
            telegram_subscribers.delete().where(telegram_subscribers.c.chat_id.in_(created))
        )


def _sent_rows(engine: Engine, chat_ids: list[int]) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count())
            .select_from(telegram_alerts_sent)
            .where(telegram_alerts_sent.c.chat_id.in_(chat_ids))
        ).scalar_one()


def test_a_breakout_transition_reaches_every_active_subscriber_exactly_once(
    committing_engine, subscribed, transition_factory, session
):
    scan_date, now = session
    chats = subscribed(CHAT_A, CHAT_B)
    transition_factory("AAA", at=as_of_for(scan_date))
    sender = RecordingSender()

    report = run_dispatch(committing_engine, sender, now=now)

    assert report.scan_date == scan_date
    assert report.delivered == len(chats)
    assert sorted(sender.chats) == sorted(chats)
    assert _sent_rows(committing_engine, chats) == len(chats)


def test_the_message_names_the_security_and_the_session_and_promises_nothing(
    committing_engine, subscribed, transition_factory, session
):
    """The copy restraint, asserted rather than left to review.

    A push notification is the least supervised surface in the project.
    Anything that reads as advice here would need Module 20's gate on the
    public page, and would not have it.
    """
    scan_date, now = session
    subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date), name="Alpha Industries")
    sender = RecordingSender()

    run_dispatch(committing_engine, sender, now=now)

    _, text = sender.sent[0]
    assert "BREAKOUT_READY" in text
    assert "Alpha Industries" in text
    assert scan_date.isoformat() in text
    assert "not a recommendation" in text
    for forbidden in ("buy", "sell", "target", "profit", "opportunity", "guarantee"):
        assert forbidden not in text.lower(), forbidden


def test_rerunning_the_same_date_sends_nothing_new(
    committing_engine, subscribed, transition_factory, session
):
    """The idempotency claim, proven by counting sends across two runs.

    A cron that fires twice, or an operator retrying after a partial
    failure, must not message anybody again. The sent log is read before
    sending, so the second run finds every pair already present.
    """
    scan_date, now = session
    chats = subscribed(CHAT_A, CHAT_B)
    transition_factory("AAA", at=as_of_for(scan_date))

    first = RecordingSender()
    run_dispatch(committing_engine, first, now=now)

    second = RecordingSender()
    report = run_dispatch(committing_engine, second, now=now)

    assert len(first.sent) == len(chats)
    assert second.sent == []
    assert report.delivered == 0
    assert report.already_sent == len(chats)
    assert _sent_rows(committing_engine, chats) == len(chats)


def test_a_second_transition_for_the_same_security_is_a_second_alert(
    committing_engine, subscribed, transition_factory, session
):
    """Which is why the log is keyed on the transition, not on the security.

    A security that enters BREAKOUT_READY, falls back and enters again is
    two state changes and two pieces of news. A `(security_id, scan_date)`
    key would collapse them into one and silently drop the second.
    """
    scan_date, now = session
    chats = subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date))
    transition_factory("BBB", at=as_of_for(scan_date) + timedelta(minutes=1))

    sender = RecordingSender()
    run_dispatch(committing_engine, sender, now=now)

    assert len(sender.sent) == 2
    assert _sent_rows(committing_engine, chats) == 2


def test_an_unsubscribed_chat_is_not_messaged(
    committing_engine, subscribed, transition_factory, session
):
    scan_date, now = session
    chats = subscribed(CHAT_A, CHAT_B)
    with committing_engine.begin() as connection:
        subscribers.unsubscribe(connection, CHAT_B)
    transition_factory("AAA", at=as_of_for(scan_date))

    sender = RecordingSender()
    report = run_dispatch(committing_engine, sender, now=now)

    assert sender.chats == [CHAT_A]
    assert report.subscribers == 1
    assert _sent_rows(committing_engine, chats) == 1


@pytest.mark.parametrize(
    "state",
    [
        MarketState.CONSOLIDATION,
        MarketState.UPTREND,
        MarketState.BREAKOUT_WATCH,
        MarketState.DOWN_TREND,
        MarketState.DISTRIBUTION,
    ],
)
def test_a_transition_to_any_other_state_sends_nothing(
    committing_engine, subscribed, transition_factory, session, state
):
    """Alerting on one state is the scope, and this is what holds it there.

    Parameterized across the states that would be most tempting to add —
    UPTREND especially, since a confirmed move is the flattering one to
    announce.
    """
    scan_date, now = session
    subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date), to_state=state)

    sender = RecordingSender()
    report = run_dispatch(committing_engine, sender, now=now)

    assert sender.sent == []
    assert report.transitions == 0


def test_a_transition_from_another_session_is_not_todays_news(
    committing_engine, subscribed, transition_factory, session
):
    """The run reports the session `scan_date_for(now)` names, and only that.

    A transition recorded a week ago is not something to notify anybody
    about now, and a date bracket that leaked would turn the first run
    after an outage into a week of alerts arriving at once.
    """
    scan_date, now = session
    subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date) - timedelta(days=7))

    sender = RecordingSender()
    report = run_dispatch(committing_engine, sender, now=now)

    assert report.transitions == 0
    assert sender.sent == []


def test_a_blocked_subscriber_is_deactivated_and_the_rest_of_the_run_continues(
    committing_engine, subscribed, transition_factory, session
):
    """403 means they blocked the bot — a subscriber who left, not an incident.

    The run must finish: the first person to block the bot silencing it
    for everyone is the failure this test exists to prevent.
    """
    scan_date, now = session
    chats = subscribed(CHAT_A, CHAT_B)
    transition_factory("AAA", at=as_of_for(scan_date))
    sender = RecordingSender(
        {CHAT_A: SendResult(SendOutcome.UNREACHABLE, "bot was blocked by the user")}
    )

    report = run_dispatch(committing_engine, sender, now=now)

    assert sorted(sender.chats) == sorted(chats)  # both were attempted
    assert report.deactivated == [CHAT_A]
    assert report.delivered == 1
    assert report.healthy  # a subscriber leaving is not a failed run

    with committing_engine.connect() as connection:
        row = connection.execute(
            select(telegram_subscribers).where(telegram_subscribers.c.chat_id == CHAT_A)
        ).one()
    assert row.unsubscribed_at is not None
    # No sent row for the blocked chat: nothing was delivered to it.
    assert _sent_rows(committing_engine, [CHAT_A]) == 0


def test_a_transient_failure_does_not_stop_the_run_but_does_fail_it(
    committing_engine, subscribed, transition_factory, session
):
    """The distinction the exit code carries.

    A blocked subscriber is handled; a 502 is this job not doing its
    work, and a cron that exits 0 on that is a job that goes quietly
    dark.
    """
    scan_date, now = session
    chats = subscribed(CHAT_A, CHAT_B)
    transition_factory("AAA", at=as_of_for(scan_date))
    sender = RecordingSender({CHAT_A: SendResult(SendOutcome.TRANSIENT, "http:502")})

    report = run_dispatch(committing_engine, sender, now=now)

    assert len(sender.sent) == 2
    assert report.delivered == 1
    assert report.failed == 1
    assert not report.healthy
    assert _sent_rows(committing_engine, chats) == 1


def test_a_failed_send_is_retried_by_a_rerun_of_the_same_date(
    committing_engine, subscribed, transition_factory, session
):
    """Because the row is written only after Telegram accepted.

    Within the same session a retry is exactly right. Across sessions it
    is not, and the next evening's run asks about the next session — see
    the dispatch module on why a stale alert is worse than a missing one.
    """
    scan_date, now = session
    chats = subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date))

    failing = RecordingSender({CHAT_A: SendResult(SendOutcome.TRANSIENT, "http:502")})
    run_dispatch(committing_engine, failing, now=now)

    retry = RecordingSender()
    report = run_dispatch(committing_engine, retry, now=now)

    assert retry.chats == [CHAT_A]
    assert report.delivered == 1
    assert _sent_rows(committing_engine, chats) == 1


def test_a_run_with_no_subscribers_sends_nothing_and_is_healthy(
    committing_engine, transition_factory, session
):
    scan_date, now = session
    transition_factory("AAA", at=as_of_for(scan_date))
    sender = RecordingSender()

    report = run_dispatch(committing_engine, sender, now=now)

    assert sender.sent == []
    assert report.healthy


def test_a_run_with_no_due_session_does_nothing_and_says_so(
    committing_engine, subscribed, transition_factory, session, monkeypatch
):
    """The defensive branch, reached the only way it can be.

    `scan_date_for` walks back up to Module 18's catch-up window looking
    for a due session, so in practice it always finds one — the None it
    can return needs thirty consecutive non-trading days. The branch is
    still there because the function's contract says None is possible,
    and a run that quietly treated None as a date would query a bracket
    around nothing. Patched here rather than left unexercised, with the
    patch named so nobody mistakes this for a real calendar case.
    """
    scan_date, now = session
    subscribed(CHAT_A)
    transition_factory("AAA", at=as_of_for(scan_date))
    monkeypatch.setattr("services.telegram.dispatch.scan_date_for", lambda moment: None)
    sender = RecordingSender()

    report = run_dispatch(committing_engine, sender, now=now)

    assert report.scan_date is None
    assert sender.sent == []
    assert report.healthy
    assert "No trading session is due" in report.skipped_reason
