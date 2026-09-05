"""The Ultimate-plan data the Terminal serves: analyst, governance, holdings.

Four tables for the Ultimate-plan endpoints, and the split is by
**natural key** rather than by endpoint — two records with the same key
shape and the same meaning of "newer" belong in one table, however
different their payloads look.

| Table | Key | Holds |
|---|---|---|
| `canonical_disclosures` | security + type + fiscal period | analyst estimates, executive compensation, earnings-call transcripts |
| `canonical_snapshots` | security + type + observation time | price-target consensus, peer group, fund/ETF holdings |
| `analyst_grades` | security + firm + action time | rating changes, one row per action |

`analyst_grades` is separate from the snapshots because it is an event
stream, not a state: several firms can act on the same day, each action
is its own fact, and none of them supersedes another. Keying it like a
snapshot would make two firms' same-day calls collide.

## Why these are not `canonical_fundamentals` rows

An analyst estimate is period-keyed and restatable and would fit that
table mechanically. It is still the wrong home: every reader of
`canonical_fundamentals` is entitled to assume a row there is something
the *issuer reported*, and `core/candidate_detection/eligibility/bankruptcy.py`
is one such reader. A forecast sitting alongside filed figures is the
kind of category error that stays harmless until someone writes a query
that sums across statement types. See `CanonicalDisclosureType`.

## Append-only, joining migration 0003's guard

All four carry the same triggers `canonical_ohlcv`, `canonical_fundamentals`
and `canonical_news` carry: `UPDATE`, `DELETE` and `TRUNCATE` are refused
by the database rather than by discipline. A revised estimate, a new
consensus or a re-stated compensation figure arrives as a **new row** with
a later `observation_time`; the earlier row stays, because it is what
ARGUS could have known at the earlier instant and overwriting it would
rewrite that history.

That is also why re-ingestion is `ON CONFLICT DO NOTHING` everywhere in
`core/ingestion/terminal_data.py` — `DO UPDATE` would fire the guard and
fail, so the constraint and the trigger agree.

## Field-naming caveat

FMP's exact JSON field names for these endpoints are documented but not
verified against a live key (Ultimate plan). Every table keeps the
untouched provider payload in `data`; the few fields ARGUS reads to build
a key are resolved through `FIELD_ALIASES` in
`data/normalization/terminal_records.py`, which records the spelling that
worked in `lineage.resolved_fields` — the pattern
`core/candidate_detection/eligibility/bankruptcy.py` established.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
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

canonical_disclosures = Table(
    "canonical_disclosures",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # A `CanonicalDisclosureType`. Free text for the same reason
    # `canonical_fundamentals.statement_type` is: Module 05 owns the
    # taxonomy, and a native enum would need a migration to extend.
    Column("disclosure_type", Text, nullable=False),
    # The provider's own period label — "2026", "2026-Q2", "FY2025".
    # Kept verbatim rather than parsed into a number, because a label
    # ARGUS reshaped is one a reader cannot check against the source.
    Column("fiscal_period", Text, nullable=False),
    # When the period ended, where it can be determined. Nullable because
    # a transcript's quarter is a label, not always a datable boundary.
    Column("fiscal_period_end", DateTime(timezone=True), nullable=True),
    *pit_columns(),
    Column("data", JSONB, nullable=False),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    # Mirrors `uq_fundamentals_observation`: the same period observed
    # again at a later instant is a revision and gets its own row; the
    # same period observed at the same instant is the same fact.
    UniqueConstraint(
        "security_id",
        "disclosure_type",
        "fiscal_period",
        "observation_time",
        name="uq_disclosure_observation",
    ),
    Index("ix_disclosures_lookup", "security_id", "disclosure_type", "availability_time"),
    comment=(
        "Period-keyed provider records that are not financial statements "
        "(analyst estimates, executive compensation, earnings transcripts)."
    ),
)

canonical_snapshots = Table(
    "canonical_snapshots",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    #: A `CanonicalSnapshotType`.
    Column("snapshot_type", Text, nullable=False),
    *pit_columns(),
    Column("data", JSONB, nullable=False),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    # One observation per instant. Re-fetching an unchanged consensus
    # inserts nothing; a changed one becomes the newest row and the old
    # one stays readable as what the consensus was then.
    UniqueConstraint(
        "security_id", "snapshot_type", "observation_time", name="uq_snapshot_observation"
    ),
    Index("ix_snapshots_lookup", "security_id", "snapshot_type", "availability_time"),
    comment=(
        "Rolling per-security states with no fiscal period "
        "(price-target consensus, peer group, fund holdings)."
    ),
)

analyst_grades = Table(
    "analyst_grades",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    *pit_columns(),
    Column("grading_company", Text, nullable=False),
    # "upgrade", "downgrade", "initialise", "maintain" — the provider's
    # own word, not a normalised one. ARGUS does not decide what a firm
    # meant by its own action.
    Column("action", Text, nullable=True),
    Column("previous_grade", Text, nullable=True),
    Column("new_grade", Text, nullable=True),
    Column("data", JSONB, nullable=False),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    # One firm, one action, one instant. Two firms acting on the same day
    # are two rows, which a snapshot-shaped key could not represent.
    UniqueConstraint(
        "security_id", "grading_company", "event_time", "new_grade", name="uq_analyst_grade_action"
    ),
    Index("ix_analyst_grades_lookup", "security_id", "availability_time"),
    comment="Analyst rating changes, one row per firm per action. An event stream.",
)


technical_indicators = Table(
    "technical_indicators",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # One of `endpoints.TECHNICAL_INDICATORS`, and the parameters it was
    # computed under. All three are part of the key: a 14-period RSI and
    # a 50-period RSI on the same bar are different numbers, and storing
    # them under one key would make whichever arrived second invisible.
    Column("indicator", Text, nullable=False),
    Column("period_length", Integer, nullable=False),
    Column("timeframe", Text, nullable=False),
    *pit_columns(),
    # The indicator's own value. Nullable because which JSON key holds it
    # differs per indicator and the resolution is best-effort; the whole
    # provider row is kept in `data` either way.
    Column("value", Numeric, nullable=True),
    Column("data", JSONB, nullable=False),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    # No `observation_time` in the key, unlike the tables above. An
    # indicator over a *closed* bar is settled: yesterday's RSI does not
    # get revised, so a re-fetch offers the same number and inserting a
    # second row for it would only grow the table. First write wins.
    UniqueConstraint(
        "security_id",
        "indicator",
        "period_length",
        "timeframe",
        "event_time",
        name="uq_technical_indicator_point",
    ),
    Index(
        "ix_technical_indicators_series",
        "security_id",
        "indicator",
        "period_length",
        "timeframe",
        "event_time",
    ),
    Index("ix_technical_indicators_availability", "availability_time", "security_id"),
    comment="Provider-computed technical indicator series. Never recomputed by ARGUS.",
)
