"""Security identity and universe versioning.

The central rule here: **nothing in ARGUS references a security by
ticker**. Tickers are reassigned, recycled and changed; identity is not.
Everything downstream references `security_identity.id`, and the ticker a
security traded under at a given moment is looked up from
`security_ticker_history` by validity range.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.enums import ListingStatus
from infra.db.metadata import metadata, pg_enum

security_identity = Table(
    "security_identity",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    # Stable external anchors, when known. These survive ticker changes
    # and are what lets a re-listed or renamed company be recognised as
    # the same entity.
    Column("figi", Text, nullable=True, unique=True),
    Column("cik", Text, nullable=True),
    # Most recently known display name. Deliberately mutable — the
    # point-in-time record of what a company was called lives in the
    # canonical corporate-action history, not here.
    Column("name", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="Stable internal identity for a security. Referenced by everything; never the ticker.",
)

security_ticker_history = Table(
    "security_ticker_history",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("ticker", Text, nullable=False),
    Column("exchange", Text, nullable=False),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    # NULL valid_to means "still current".
    Column("valid_to", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("security_id", "ticker", "valid_from", name="uq_ticker_validity"),
    CheckConstraint(
        "valid_to IS NULL OR valid_to > valid_from",
        name="valid_range_ordered",
    ),
    # Resolving "which security was trading as AAPL on 2013-06-01" is the
    # hot path here, hence ticker leading the index.
    Index("ix_ticker_history_lookup", "ticker", "valid_from", "valid_to"),
    Index("ix_ticker_history_security", "security_id", "valid_from"),
    comment="Ticker <-> identity mapping over time. Tickers change; identity does not.",
)

universe_version = Table(
    "universe_version",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("version_label", Text, nullable=False, unique=True),
    # The rule that produced this universe (exchanges included, filters
    # applied), stored so the membership set can be explained later.
    # There is deliberately no "expected size" anywhere in the schema —
    # the universe is however many securities NYSE + NASDAQ listed.
    Column("definition", JSONB, nullable=False),
    Column("description", Text, nullable=True),
    Column("as_of_date", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="Immutable versioned definition of the analyzable universe.",
)

universe_membership = Table(
    "universe_membership",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "universe_version_id",
        UUID(as_uuid=True),
        ForeignKey("universe_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "listing_status",
        pg_enum(ListingStatus, "listing_status"),
        nullable=False,
    ),
    # Delisted and bankrupt securities stay in the historical universe on
    # purpose: dropping them is exactly how survivorship bias gets into a
    # backtest. Module 09's live eligibility gate excludes them from the
    # candidate pool, which is a separate decision.
    Column("has_sufficient_history", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("universe_version_id", "security_id", name="uq_membership"),
    Index("ix_universe_membership_security", "security_id"),
    comment="Which securities were in which universe version, including delisted/bankrupt ones.",
)
