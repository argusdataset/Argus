"""One dispatch run: who entered BREAKOUT_READY, and who has not been told.

## Which session, and why it is not this module's decision

`scan_date_for(now)` — Module 18's own schedule function, the same one
Module 26's ingestion reuses. Three processes now agree on "which
trading session is this evening about" because all three ask one
function. If this module decided independently, a disagreement would look
exactly like a quiet day: the dispatch would find no transitions and send
nothing, and nothing anywhere would say why.

## Finding the transitions

`market_state_transitions` is already exactly the feed this needs —
Module 10 records one immutable row per change and *only* changes, so
"entered BREAKOUT_READY today" needs no derivation, no diffing against
yesterday, and no second projection.

`history()` is the closest existing reader and does not fit: it answers
"every transition for **one** security", which is the question Module 11
asks. This asks "every security that made **one** transition, on one
session" — the other axis. So a narrow query lives here rather than in
Module 10, whose boundary this module was told not to cross.

**The date bracket.** A live scan stamps every transition with the scan's
`as_of`, which is `session_close + scan_offset_hours`. Matching that
instant exactly would be more precise and would break the day somebody
changes the offset — which is a change `docs/architecture/KNOWN_ISSUES.md`
G1 says has to happen. So the query brackets
`[session_close(scan_date), session_close(scan_date) + 24h)`, which holds
for any offset a daily scanner could sanely use, and states the
assumption instead of hiding it.

## Idempotency

`telegram_alerts_sent` is unique on `(chat_id, transition_id)`, and the
run reads it before sending rather than after. A rerun — a cron that
fired twice, an operator retrying — finds every pair already present and
sends nothing.

Note what is *not* recorded: a failed send. The row is written only after
Telegram accepted the message, so a failure is not marked done. It is
also not retried, because the next run is tomorrow and asks about
tomorrow's session — an alert about yesterday delivered a day late is
worse than silence, and this is a notification, not a record. The record
is `market_state_transitions`, which is untouched by any of this.

## One failure does not end the run

Every send is wrapped: a blocked subscriber deactivates and the loop
continues, a transient failure is counted and the loop continues. The
alternative — one unreachable chat aborting the run — would mean the
first person to block the bot silences it for everyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.data_validation.identity import SecurityLabel, labels_as_of
from core.live_scanner.schedule import scan_date_for
from data.canonical_model.pit import session_close
from infra.db.enums import MarketState
from infra.db.schema.intelligence import market_state_transitions
from infra.db.schema.telegram import telegram_alerts_sent
from infra.observability.logging import get_logger
from services.telegram import subscribers
from services.telegram.client import SendOutcome, SendResult
from services.telegram.config import TelegramConfig
from services.telegram.messages import alert_text, truncate

__all__ = [
    "ALERT_STATE",
    "DispatchReport",
    "MessageSender",
    "Transition",
    "breakout_transitions",
    "run_dispatch",
]

_log = get_logger("argus.telegram.dispatch")

#: The one state this bot alerts on. A constant rather than a parameter:
#: alerting on any other state is explicitly out of scope, and a
#: parameter would invite it in without the copy or the reasoning that
#: would need.
ALERT_STATE = MarketState.BREAKOUT_READY


class MessageSender(Protocol):
    """The slice of `TelegramClient` a dispatch run uses."""

    def send_message(self, chat_id: int, text: str) -> SendResult: ...


@dataclass(frozen=True, slots=True)
class Transition:
    """One security entering the alert state, with the row's own id."""

    transition_id: UUID
    security_id: UUID
    transition_time: datetime


@dataclass(slots=True)
class DispatchReport:
    """What one run found, sent, and could not send."""

    scan_date: date | None
    transitions: int = 0
    subscribers: int = 0
    already_sent: int = 0
    delivered: int = 0
    deactivated: list[int] = field(default_factory=list)
    failed: int = 0
    skipped_reason: str | None = None

    @property
    def healthy(self) -> bool:
        """A run with nothing to do is healthy; unsent transient failures are not.

        A blocked subscriber is not a failure — it is a subscriber who
        left, handled correctly. A transient failure is the run not
        managing to do its job, and the exit code should say so.
        """
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_date": self.scan_date.isoformat() if self.scan_date else None,
            "transitions": self.transitions,
            "subscribers": self.subscribers,
            "already_sent": self.already_sent,
            "delivered": self.delivered,
            "deactivated": len(self.deactivated),
            "failed": self.failed,
            "skipped_reason": self.skipped_reason,
            "healthy": self.healthy,
        }


def breakout_transitions(
    connection: Connection,
    *,
    scan_date: date,
    state: MarketState = ALERT_STATE,
) -> list[Transition]:
    """Every security that entered `state` during `scan_date`'s session.

    See the module docstring on the date bracket and why it is a range
    rather than the scan's exact `as_of`.
    """
    opened = session_close(scan_date)
    closed = opened + timedelta(days=1)

    rows = connection.execute(
        select(
            market_state_transitions.c.id,
            market_state_transitions.c.security_id,
            market_state_transitions.c.transition_time,
        )
        .where(
            market_state_transitions.c.to_state == state.value,
            market_state_transitions.c.transition_time >= opened,
            market_state_transitions.c.transition_time < closed,
        )
        .order_by(
            market_state_transitions.c.transition_time,
            market_state_transitions.c.security_id,
        )
    ).all()

    return [
        Transition(
            transition_id=row.id,
            security_id=row.security_id,
            transition_time=row.transition_time,
        )
        for row in rows
    ]


def run_dispatch(
    engine: Engine,
    sender: MessageSender,
    *,
    config: TelegramConfig | None = None,
    now: datetime | None = None,
) -> DispatchReport:
    """One scheduled wake-up: alert every active subscriber, once each."""
    settings = (config or TelegramConfig()).settings
    moment = now or datetime.now(UTC)

    scan_date = scan_date_for(moment)
    if scan_date is None:
        report = DispatchReport(
            scan_date=None,
            skipped_reason=(
                "No trading session is due yet. Module 18's schedule answers which "
                "session should have been handled by now, and on a weekend the "
                "answer is none."
            ),
        )
        _log.info("nothing to dispatch", extra={"event": "dispatch_skipped", **report.as_dict()})
        return report

    with engine.connect() as connection:
        transitions = breakout_transitions(connection, scan_date=scan_date)
        chat_ids = subscribers.active_chat_ids(connection)
        labels = labels_as_of(
            connection,
            [transition.security_id for transition in transitions],
            as_of=moment,
        )
        outstanding = _outstanding(connection, transitions, chat_ids)

    report = DispatchReport(
        scan_date=scan_date,
        transitions=len(transitions),
        subscribers=len(chat_ids),
        already_sent=len(transitions) * len(chat_ids) - len(outstanding),
    )

    _log.info(
        "dispatch starting",
        extra={"event": "dispatch_starting", **report.as_dict()},
    )

    for chat_id, transition in outstanding:
        label = labels.get(
            transition.security_id,
            SecurityLabel(security_id=transition.security_id, ticker=None, name=None),
        )
        text = truncate(alert_text(label, scan_date=scan_date), limit=settings.message_limit)
        _deliver(engine, sender, chat_id, transition, text, scan_date, report, moment)

    _log.info("dispatch finished", extra={"event": "dispatch_finished", **report.as_dict()})
    return report


def _deliver(
    engine: Engine,
    sender: MessageSender,
    chat_id: int,
    transition: Transition,
    text: str,
    scan_date: date,
    report: DispatchReport,
    moment: datetime,
) -> None:
    """One message, and the row that stops it being sent twice.

    The row is written after Telegram accepts, in its own transaction. In
    that order because the two failure modes are not equal: writing first
    and crashing means a subscriber never hears about a transition and
    nothing will ever retry, while sending first and crashing means a
    duplicate on the next rerun. A duplicate message is an annoyance; a
    silently dropped one is the bot not working.
    """
    result = sender.send_message(chat_id, text)

    if result.outcome is SendOutcome.UNREACHABLE:
        with engine.begin() as connection:
            subscribers.deactivate(connection, chat_id, now=moment)
        report.deactivated.append(chat_id)
        _log.info(
            "subscriber unreachable, deactivated",
            extra={
                "event": "telegram_subscriber_deactivated",
                "chat_id": chat_id,
                "detail": result.detail,
            },
        )
        return

    if not result.delivered:
        report.failed += 1
        return

    with engine.begin() as connection:
        connection.execute(
            insert(telegram_alerts_sent)
            .values(
                chat_id=chat_id,
                transition_id=transition.transition_id,
                scan_date=scan_date,
                sent_at=moment,
            )
            # Belt and braces against two runs overlapping: the read-side
            # check above is what normally prevents a resend, and this is
            # what stops the write from raising if it did not.
            .on_conflict_do_nothing(constraint="uq_telegram_alert_once")
        )
    report.delivered += 1


def _outstanding(
    connection: Connection,
    transitions: list[Transition],
    chat_ids: list[int],
) -> list[tuple[int, Transition]]:
    """Every (subscriber, transition) pair not already in the sent log.

    One query for the whole cross product rather than one per pair. The
    pairs are produced in subscriber order so an interrupted run has
    messaged a deterministic prefix.
    """
    if not transitions or not chat_ids:
        return []

    by_id = {transition.transition_id: transition for transition in transitions}
    sent = {
        (row.chat_id, row.transition_id)
        for row in connection.execute(
            select(
                telegram_alerts_sent.c.chat_id,
                telegram_alerts_sent.c.transition_id,
            ).where(
                and_(
                    telegram_alerts_sent.c.chat_id.in_(chat_ids),
                    telegram_alerts_sent.c.transition_id.in_(list(by_id)),
                )
            )
        ).all()
    }

    return [
        (chat_id, transition)
        for chat_id in chat_ids
        for transition in transitions
        if (chat_id, transition.transition_id) not in sent
    ]
