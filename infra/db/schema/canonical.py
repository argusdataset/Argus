"""Canonical market data — the only shape downstream modules ever read.

Every table here carries all four point-in-time timestamps, all
non-nullable (see `infra.db.metadata.pit_columns`). Module 05 translates
provider payloads into these tables; Module 07 enforces that an "as of X"
query only sees rows with `availability_time <= X`. This module supplies
the fields that make that enforceable — it implements neither.

**Restatements are new rows, never edits.** A fundamental that gets
revised, or a bar that a provider corrects, arrives as an additional row
with a later `observation_time` / `availability_time`. That is why the
uniqueness constraints include `observation_time`: the same
`event_time` legitimately has several observations, and a PIT-correct
query picks the latest one that was available at the time.

**Raw and adjusted prices are both retained.** Raw is what a trader would
actually have seen on the day (point-in-time realism); adjusted is what
continuity analysis across splits and dividends needs. Collapsing them
loses one or the other irrecoverably.

Sizing note: `canonical_ohlcv` and `feature_vectors` are the two
high-volume tables — roughly a full US equity universe times ~15 years of
daily bars. Their access pattern is always "one or many securities, over
a time range", which is what the composite indexes below serve. If volume
later demands TimescaleDB or ClickHouse, these tables are the ones that
would move; nothing here uses a Postgres-only construct that would block
that, and no such dependency is added now.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.enums import CorporateActionType, Timeframe
from infra.db.metadata import metadata, pg_enum, pit_columns

# Prices carry more precision than US equities strictly need so that
# split-adjusted historical series do not lose resolution.
_PRICE = Numeric(20, 6)

canonical_ohlcv = Table(
    "canonical_ohlcv",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # Only DAILY is sourced initially (Module 04). The other timeframes
    # exist so Module 08's multi-timeframe work needs no schema change.
    Column("timeframe", pg_enum(Timeframe, "timeframe"), nullable=False),
    *pit_columns(),
    # Raw: what actually printed on the day.
    Column("open_raw", _PRICE, nullable=False),
    Column("high_raw", _PRICE, nullable=False),
    Column("low_raw", _PRICE, nullable=False),
    Column("close_raw", _PRICE, nullable=False),
    Column("volume_raw", BigInteger, nullable=False),
    # Adjusted: back-adjusted for splits/dividends, for continuity
    # analysis. Nullable because adjustment depends on corporate actions
    # that may not have been known at ingestion time.
    Column("open_adjusted", _PRICE, nullable=True),
    Column("high_adjusted", _PRICE, nullable=True),
    Column("low_adjusted", _PRICE, nullable=True),
    Column("close_adjusted", _PRICE, nullable=True),
    Column("volume_adjusted", BigInteger, nullable=True),
    UniqueConstraint(
        "security_id",
        "timeframe",
        "event_time",
        "observation_time",
        name="uq_ohlcv_observation",
    ),
    CheckConstraint("high_raw >= low_raw", name="high_ge_low"),
    CheckConstraint("volume_raw >= 0", name="volume_non_negative"),
    # Primary access pattern: a security's bars over a time range.
    Index("ix_ohlcv_security_time", "security_id", "timeframe", "event_time"),
    # PIT access pattern: what was available as of a past date. Module 07
    # filters on availability_time; without this index that filter is a
    # full scan across ~15 years of the whole universe.
    Index("ix_ohlcv_availability", "availability_time", "security_id", "timeframe"),
    comment="Canonical OHLCV bars, raw and adjusted, with full PIT timestamps.",
)

canonical_fundamentals = Table(
    "canonical_fundamentals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # e.g. INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW / RATIOS. Free
    # text rather than an enum because Module 05 owns the canonical
    # statement taxonomy and has not defined it yet.
    Column("statement_type", Text, nullable=False),
    Column("fiscal_period", Text, nullable=False),
    Column("fiscal_period_end", DateTime(timezone=True), nullable=False),
    *pit_columns(),
    # The metric payload itself. Module 05 defines the canonical field
    # names; pinning them as columns here would pre-empt that decision
    # and force a migration per metric. A GIN index makes metric lookups
    # inside the payload practical.
    Column("data", JSONB, nullable=False),
    Column(
        "restates_id", UUID(as_uuid=True), ForeignKey("canonical_fundamentals.id"), nullable=True
    ),
    UniqueConstraint(
        "security_id",
        "statement_type",
        "fiscal_period",
        "observation_time",
        name="uq_fundamentals_observation",
    ),
    Index("ix_fundamentals_security_period", "security_id", "fiscal_period_end"),
    Index("ix_fundamentals_availability", "availability_time", "security_id"),
    Index("ix_fundamentals_data", "data", postgresql_using="gin"),
    comment="Canonical fundamentals. Restatements arrive as new rows, never edits.",
)

canonical_corporate_actions = Table(
    "canonical_corporate_actions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "action_type",
        pg_enum(CorporateActionType, "corporate_action_type"),
        nullable=False,
    ),
    *pit_columns(),
    Column("effective_date", DateTime(timezone=True), nullable=False),
    # Type-specific detail: split ratio, dividend amount and currency,
    # acquirer identity, new ticker, and so on.
    Column("details", JSONB, nullable=False),
    UniqueConstraint(
        "security_id",
        "action_type",
        "effective_date",
        "observation_time",
        name="uq_corporate_action_observation",
    ),
    Index("ix_corporate_actions_security", "security_id", "effective_date"),
    Index("ix_corporate_actions_availability", "availability_time", "security_id"),
    comment="Splits, dividends, mergers, delistings, bankruptcies, ticker changes.",
)
