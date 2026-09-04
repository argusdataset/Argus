"""Raw SEC filings ingested from FMP (currently 8-K only), and the daily
same-day-filing signal computed from them (Module 28's third signal —
see `core/news_signals/filings.py`).

## Why this table exists separately from `canonical_news`

An SEC filing is a different kind of fact from a news article: it is a
mandatory regulatory disclosure with a filing date, an accepted timestamp
and (when resolvable) which numbered "Item" it disclosed, none of which
`canonical_news` has a column for. Folding it into that table would mean
either leaving those columns NULL for every ordinary article or growing
`canonical_news` a vocabulary it was never meant to carry.

## Field-naming caveat

FMP's exact JSON field names for `/stable/8k-latest` and
`/stable/search-by-symbol` are documented but not verified against a live
key (Ultimate plan, not yet purchased) — the same caveat
`core/candidate_detection/eligibility/bankruptcy.py` carries for
fundamentals fields. `item_numbers` is resolved best-effort at ingest time
by `core/news_signals/filings.py`'s `FIELD_ALIASES` and may be an empty
array when nothing recognisable was found; the untouched provider payload
stays in `data` regardless, so a later correction can re-derive it.

## Two tables, one relationship

`sec_filings` is the raw, ingested fact — insert-only, like
`canonical_news`. `sec_filing_signals` is the daily projection: one row
per security per day, `raised` says whether *any* 8-K was filed on that
day. Upsertable, the same `(security_id, signal_date)` shape as
`news_volume_signals` — a same-day rerun converges on one row.

## `raised` is `NOT NULL`, unlike the volume-anomaly signal

That is deliberate, and different from `news_volume_signals.raised`. A
filing either happened on a given day or it did not; there is no baseline
to be insufficient and no history requirement that could make the answer
"undetermined" — the same distinction the module docstring draws between
a reactive fact and a relative anomaly.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.metadata import metadata, pit_columns

sec_filings = Table(
    "sec_filings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    *pit_columns(),
    Column("form_type", Text, nullable=False),
    # Best-effort, resolved via FIELD_ALIASES; [] when nothing resolved.
    Column("item_numbers", JSONB, nullable=False, server_default="[]"),
    Column("link", Text, nullable=True),
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    Column("data", JSONB, nullable=False, server_default="{}"),
    # Two partial indexes, mirroring canonical_news: a filing with a link
    # is deduplicated on it, one without falls back to a composite key —
    # so a provider that omits links on some rows cannot collapse them
    # onto a single NULL.
    Index(
        "uq_sec_filings_link",
        "security_id",
        "link",
        unique=True,
        postgresql_where=Column("link").is_not(None),
    ),
    Index(
        "uq_sec_filings_no_link",
        "security_id",
        "form_type",
        "event_time",
        unique=True,
        postgresql_where=Column("link").is_(None),
    ),
    Index("ix_sec_filings_security_time", "security_id", "event_time"),
    Index("ix_sec_filings_availability", "availability_time", "security_id"),
    comment="Raw SEC filings ingested from FMP (8-K only for now). Never scored.",
)

sec_filing_signals = Table(
    "sec_filing_signals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("signal_date", Date, nullable=False),
    # NOT NULL, unlike news_volume_signals.raised — see the module
    # docstring on why this signal has no undetermined state.
    Column("raised", Boolean, nullable=False),
    Column("item_numbers", JSONB, nullable=False, server_default="[]"),
    Column("config_version_label", Text, nullable=False),
    Column(
        "computed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("detail", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("security_id", "signal_date", name="uq_sec_filing_signal_security_date"),
    Index("ix_sec_filing_signals_security", "security_id", "signal_date"),
    comment=(
        "One same-day-8-K reading per security per day (Module 28). "
        "raised is NOT NULL — a binary fact, not a tri-state anomaly."
    ),
)
