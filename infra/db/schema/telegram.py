"""Telegram subscribers and the alerts already sent to them (Module 27).

Two tables, and the interesting decisions are both about what they
deliberately do *not* hold.

## A subscriber is a chat id and nothing else

No `user_id`, no foreign key to `users`, no role. The bot is open: anyone
can `/start` it without an ARGUS account, because there is no track
record to sell access to yet. A paid tier gating this later is a real
discussed direction, and the shape here is chosen so that gate is an
added check rather than a migration — the table answers "who is
subscribed", which stays the right question whether or not subscribing
becomes conditional.

`chat_id` is `BIGINT`, not `INTEGER`. Telegram's ids exceed 32 bits, and
a group chat's id is negative. Both are things a schema learns about the
hard way.

## `/stop` keeps the row

`unsubscribed_at` is set; the row stays. Deleting it would make a
resubscribe indistinguishable from a first-time subscriber and would
throw away the count of everyone who ever subscribed — the only
engagement number this bot produces. Dispatch targets
`unsubscribed_at IS NULL` and nothing else.

## The sent log is what makes a rerun safe

`telegram_alerts_sent` is keyed `(chat_id, transition_id)`, unique. A
transition is Module 10's own row — one immutable record per state
change — so "this subscriber has already been told about this change" is
a fact with an exact primary key, and a dispatch rerun finds it rather
than sending again.

Keyed on the transition rather than on `(security_id, scan_date)`
because the transition *is* the event being announced: a security that
enters BREAKOUT_READY, falls back, and enters again is two transitions
and should be two messages, which a security/date key would collapse.

Neither table is append-only guarded, for the reason migration 0009 gave
`live_scan_runs`: Module 03's guards protect rows whose editing would
rewrite what ARGUS believed at a point in time, and neither of these is
that. `telegram_subscribers` in particular *must* accept an UPDATE —
that is what `/stop` is.

One consequence to keep in view: `telegram_alerts_sent` grows as
subscribers × transitions, and being unguarded it can be pruned. A future
retention policy must only prune rows older than the dispatch window, or
pruning becomes re-sending.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from infra.db.metadata import metadata

telegram_subscribers = Table(
    "telegram_subscribers",
    metadata,
    # Telegram's own chat id, which is the whole identity of a
    # subscriber. Primary key rather than a surrogate: there is exactly
    # one row per chat and the natural key is stable and supplied.
    Column("chat_id", BigInteger, primary_key=True, autoincrement=False),
    Column("subscribed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # NULL means active. See the module docstring on why `/stop` does not
    # delete.
    Column("unsubscribed_at", DateTime(timezone=True), nullable=True),
    Index("ix_telegram_subscribers_active", "unsubscribed_at"),
    comment=(
        "Telegram chats subscribed to BREAKOUT_READY alerts (Module 27). "
        "No ARGUS account, no role — a subscriber is a chat id."
    ),
)

telegram_alerts_sent = Table(
    "telegram_alerts_sent",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "chat_id",
        BigInteger,
        ForeignKey("telegram_subscribers.chat_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "transition_id",
        UUID(as_uuid=True),
        ForeignKey("market_state_transitions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The dispatch run's target session. Not derivable from the
    # transition alone, and it is the question an operator asks first —
    # "what went out for Monday".
    Column("scan_date", Date, nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("chat_id", "transition_id", name="uq_telegram_alert_once"),
    Index("ix_telegram_alerts_scan_date", "scan_date"),
    comment=(
        "One row per alert actually delivered (Module 27). The unique "
        "constraint is what makes a dispatch rerun send nothing."
    ),
)
