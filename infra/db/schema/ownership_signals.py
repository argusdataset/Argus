"""Module 29 — insider-trading clusters and 13F institutional-ownership trend.

Two independent data families, two independent signals, kept in one
schema file because they share nothing but the FMP Ultimate plan they
came from — see `core/ownership_signals/README.md`.

## Field-naming caveat

FMP's exact JSON field names for `/stable/insider-trading/search` and
`/stable/institutional-ownership/symbol-positions-summary` are documented
but not verified against a live key (Ultimate plan, not yet purchased).
Both raw tables keep the untouched provider payload in `data`;
`core/ownership_signals/translate.py`'s `FIELD_ALIASES` resolves what it
can at ingest time and records which alias worked.

## Insider trades: raw, append-like but not append-only guarded

`insider_trades` has no reliable provider-supplied unique id (unconfirmed
field names — see above), so its uniqueness key is a best-effort
composite over resolved fields. Not append-only guarded for the same
reason `deep_refresh_log` (migration 0014) and `news_volume_signals`
(migration 0016) are not: this is operational, recomputable ingestion,
not a fact ARGUS is claiming to have believed at a point in time in the
sense Module 03's guards protect.

## `insider_cluster_signals.raised` is tri-state

Unlike the SEC-filing signal, "was there a cluster of independent
open-market buys in the trailing window" genuinely has an undetermined
state: a security this module has never fetched insider-trading data for
has no basis to say `False` — see `core/ownership_signals/insider.py`.

## `institutional_ownership_signals` carries no `raised` at all

Per the brief: ownership trend is a trend, not an event. The stored
row is the current period's figures, the prior period's, and the
percentage change between them — a trader's own call, not ARGUS's.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.metadata import metadata, pit_columns

insider_trades = Table(
    "insider_trades",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # event_time/observation_time: the transaction date reported to the
    # SEC. availability_time: when ARGUS could plausibly have fetched it.
    *pit_columns(),
    Column("reporting_person", Text, nullable=True),
    Column("reporting_position", Text, nullable=True),
    # The raw SEC transaction code (e.g. "P" open-market purchase, "S"
    # sale, "A" grant/award) where resolvable — see FIELD_ALIASES.
    Column("transaction_code", Text, nullable=True),
    Column("quantity", Numeric, nullable=True),
    Column("price", Numeric, nullable=True),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    Column("data", JSONB, nullable=False, server_default="{}"),
    # Best-effort natural key: no confirmed provider id exists to key on.
    UniqueConstraint(
        "security_id",
        "reporting_person",
        "event_time",
        "transaction_code",
        "quantity",
        name="uq_insider_trade_natural_key",
    ),
    Index("ix_insider_trades_security_time", "security_id", "event_time"),
    Index("ix_insider_trades_availability", "availability_time", "security_id"),
    comment="Raw insider transactions ingested from FMP. Never scored.",
)

institutional_ownership = Table(
    "institutional_ownership",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("year", Integer, nullable=False),
    Column("quarter", Integer, nullable=False),
    *pit_columns(),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    Column("data", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint(
        "security_id", "year", "quarter", name="uq_institutional_ownership_security_period"
    ),
    Index("ix_institutional_ownership_security_period", "security_id", "year", "quarter"),
    comment="Raw 13F institutional-ownership summaries ingested from FMP. Never scored.",
)

insider_cluster_signals = Table(
    "insider_cluster_signals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("signal_date", Date, nullable=False),
    # Tri-state, like news_volume_signals.raised — see the module docstring.
    Column("raised", Boolean, nullable=True),
    Column("distinct_purchasers", Integer, nullable=False),
    Column("window_days", Integer, nullable=False),
    Column("min_insiders", Integer, nullable=False),
    Column("unavailable_reason", Text, nullable=True),
    Column("config_version_label", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("detail", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("security_id", "signal_date", name="uq_insider_cluster_signal_security_date"),
    Index("ix_insider_cluster_signals_security", "security_id", "signal_date"),
    comment="One insider-buy-cluster reading per security per day. raised is tri-state.",
)

institutional_ownership_signals = Table(
    "institutional_ownership_signals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("year", Integer, nullable=False),
    Column("quarter", Integer, nullable=False),
    # No `raised` column at all — see the module docstring.
    Column("investors_holding", Integer, nullable=True),
    Column("investors_holding_change", Integer, nullable=True),
    Column("total_shares", Numeric, nullable=True),
    Column("total_shares_change_percent", Numeric, nullable=True),
    Column("ownership_percent", Numeric, nullable=True),
    Column("prior_year", Integer, nullable=True),
    Column("prior_quarter", Integer, nullable=True),
    Column("unavailable_reason", Text, nullable=True),
    Column("config_version_label", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("detail", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint(
        "security_id", "year", "quarter", name="uq_institutional_ownership_signal_period"
    ),
    Index("ix_institutional_ownership_signals_security", "security_id", "year", "quarter"),
    comment=(
        "One institutional-ownership trend reading per security per quarter. "
        "No raised column — a trend, not an event; the trader decides."
    ),
)
