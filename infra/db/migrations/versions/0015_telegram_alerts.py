"""subscribers for the open Telegram bot, and the log that stops a rerun resending

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03

Module 27 sends a message when a security enters the BREAKOUT_READY
watchlist. Two tables, and both shapes are chosen against a specific
failure.

## `telegram_subscribers`

`chat_id` is `BIGINT` and the primary key. Telegram's chat ids exceed 32
bits and a group chat's is negative, so `INTEGER` is wrong in two
directions; and the id is the entire identity of a subscriber, so a
surrogate key would add a column that nothing would ever query by.

No `user_id`, no role, no foreign key to `users`. The bot is open by
design — ARGUS is pre-revenue and pre-track-record, and there is nothing
to sell access to yet. The table answers "who is subscribed", which stays
the right question if a future prompt gates it: that becomes a check
before dispatch, not a migration.

`/stop` sets `unsubscribed_at` and keeps the row. Deleting would make a
resubscribe look like a first-time subscriber and would discard the only
engagement number this bot produces. Dispatch reads
`unsubscribed_at IS NULL`.

## `telegram_alerts_sent`

Unique on `(chat_id, transition_id)`. Keyed on Module 10's transition row
— one immutable record per state change — rather than on
`(security_id, scan_date)`, because the transition *is* the announced
event: a security that enters BREAKOUT_READY, falls back and enters
again is two changes and should be two messages, which a security/date
key would silently collapse into one.

## Neither is append-only guarded

`telegram_subscribers` cannot be: `/stop` is an UPDATE.

`telegram_alerts_sent` follows the reasoning migration 0009 applied to
`live_scan_runs` — Module 03's guards exist for rows whose editing would
rewrite what ARGUS believed at a point in time, and a delivery record is
not one. It grows as subscribers × transitions, so leaving it prunable is
deliberate. A retention policy for it must only prune rows older than the
dispatch window; pruning inside that window turns into re-sending.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUBSCRIBERS = "telegram_subscribers"
ALERTS = "telegram_alerts_sent"


def upgrade() -> None:
    op.create_table(
        SUBSCRIBERS,
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        comment=(
            "Telegram chats subscribed to BREAKOUT_READY alerts (Module 27). "
            "No ARGUS account, no role — a subscriber is a chat id."
        ),
    )
    op.create_index("ix_telegram_subscribers_active", SUBSCRIBERS, ["unsubscribed_at"])

    op.create_table(
        ALERTS,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("transition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scan_date", sa.Date(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            [f"{SUBSCRIBERS}.chat_id"],
            name=op.f("fk_telegram_alerts_sent_chat_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"],
            ["market_state_transitions.id"],
            name=op.f("fk_telegram_alerts_sent_transition_id"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("chat_id", "transition_id", name="uq_telegram_alert_once"),
        comment=(
            "One row per alert actually delivered (Module 27). The unique "
            "constraint is what makes a dispatch rerun send nothing."
        ),
    )
    op.create_index("ix_telegram_alerts_scan_date", ALERTS, ["scan_date"])


def downgrade() -> None:
    op.drop_index("ix_telegram_alerts_scan_date", table_name=ALERTS)
    op.drop_table(ALERTS)
    op.drop_index("ix_telegram_subscribers_active", table_name=SUBSCRIBERS)
    op.drop_table(SUBSCRIBERS)
