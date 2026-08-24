"""Canonical news articles (Module 19's storage for a Module 05 record type).

## Why this table did not exist until now

Module 05 defined `CanonicalNewsArticle` and `translate_news`, and both
carry the same note: *"Module 03 defines no news table, so nothing
persists these."* Module 05 flagged it rather than inventing a table
outside its own scope, and named Module 19 as the consumer that would
need one. This is that module, so this is where the flag gets cleared.

The columns mirror `CanonicalNewsArticle` exactly. Nothing is added,
renamed or reinterpreted — if the two ever disagree, the record type is
right and this is wrong.

## News is never scored, and the schema says so

There is no `feature_schema_version_id`, no `signal` reference, and no
lineage to a scoring configuration. That is not an omission: the project
has been explicit since early planning that fundamentals and news are a
**context layer**, never a `target-model-v1` or `argus_score` input.
A news row that carried a scoring lineage would be an invitation to wire
one up.

## Point-in-time, with a simpler shape than fundamentals

An article is not restated. A correction is a new article, and the
original stays as published — so unlike `canonical_fundamentals` there is
no `restates_id` and no "latest revision of this fact" query. What
remains is the ordinary PIT filter every canonical table carries:
`availability_time <= as_of`, which is what stops a Terminal replay of
last March from showing an article published in June.

Identity is `(security_id, url)` when a URL exists and
`(security_id, headline, published_at)` when it does not — two partial
unique indexes rather than one constraint over a nullable column, so a
provider that omits URLs cannot collapse every one of its articles into a
single row.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from infra.db.metadata import metadata, pit_columns

canonical_news = Table(
    "canonical_news",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The four PIT timestamps every canonical row carries. For an article
    # `event_time` and `observation_time` are the same instant —
    # publication is both when it happened and when it became knowable —
    # which is exactly what Module 05's `translate_news` sets.
    *pit_columns(),
    Column("headline", Text, nullable=False),
    Column("source_site", Text, nullable=True),
    Column("url", Text, nullable=True),
    Column("summary", Text, nullable=True),
    # Provider and endpoint, matching every other canonical table's
    # `lineage` column: for auditing a row back to its origin, never for
    # dispatch.
    Column("lineage", JSONB, nullable=False, server_default="{}"),
    # Two partial indexes rather than one constraint over a nullable
    # column. A provider that omits URLs would otherwise have every one
    # of its articles collide on a single NULL.
    Index(
        "uq_news_url",
        "security_id",
        "url",
        unique=True,
        postgresql_where=Column("url").is_not(None),
    ),
    Index(
        "uq_news_headline",
        "security_id",
        "headline",
        "event_time",
        unique=True,
        postgresql_where=Column("url").is_(None),
    ),
    # The Terminal's only query: this security's articles, newest first,
    # bounded by availability.
    Index("ix_news_security_time", "security_id", "event_time"),
    Index("ix_news_availability", "availability_time", "security_id"),
    comment="Canonical news articles for the Terminal (Module 19). Never scored.",
)
