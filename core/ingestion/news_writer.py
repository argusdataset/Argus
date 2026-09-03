"""Persisting canonical news — a gap this module had to cross, not close.

## The gap, stated plainly

Module 05 defines `CanonicalNewsArticle` and `translate_news`, and both
carry the same note: *"Module 03 defines no news table, so nothing
persists these."* Module 19 later added the table (`canonical_news`,
migration 0010) and a reader for it (`services/terminal/news.py`).
**Nobody added a writer.** `CanonicalWriter` handles bars, fundamentals
and corporate actions; `persist()` writes those three and nothing else.

So "fetch news via Module 04, normalize and persist via Module 05" — the
instruction this module was built to — is not currently possible for
news. Module 05 cannot persist it.

Two ways out, and the one not taken first:

- Add `write_news()` to `CanonicalWriter`. This is the **right** fix.
  It puts news persistence where the other three live, under the same
  batching and the same insert-only discipline, and deletes this file.
  It also modifies Module 05, which this module was explicitly told not
  to do.
- Translate with Module 05's `translate_news` (calling it, not changing
  it) and write the resulting records here, following exactly the
  convention `persistence.py` established. That is what this file does.

This is a **deviation, recorded as one**. It should not survive: the
first time anything else needs to write news, two writers exist and can
disagree. The fix is four lines in `data/normalization/persistence.py`
plus one in `pipeline.persist()`, and it belongs to whoever is next
allowed to touch Module 05.

## Insert-only, ON CONFLICT DO NOTHING, twice

Same reasoning as `persistence.py`: an existing row is a fact ARGUS
already recorded. DO NOTHING rather than DO UPDATE both because a
correction to an article arrives as a new article and because the
canonical tables carry append-only triggers that would reject an UPDATE
anyway.

Two statements rather than one because Module 19 gave the table **two
partial** unique indexes — `(security_id, url)` where a URL exists and
`(security_id, headline, event_time)` where it does not — precisely so
that a provider omitting URLs cannot collapse all its articles onto one
NULL. Postgres infers a partial index only when the statement repeats its
predicate, so articles with a URL and articles without are inserted
separately.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from data.canonical_model.records import CanonicalNewsArticle
from data.normalization.persistence import DEFAULT_BATCH_SIZE, WriteResult
from infra.db.schema.news import canonical_news

__all__ = ["write_news"]


def write_news(
    connection: Connection,
    articles: list[CanonicalNewsArticle],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Write canonical news articles. Insert-only; re-runs are idempotent."""
    result = WriteResult(offered=len(articles))
    if not articles:
        return result

    with_url = [_values(article) for article in articles if article.url]
    without_url = [_values(article) for article in articles if not article.url]

    result.inserted += _insert(
        connection,
        with_url,
        batch_size=batch_size,
        conflict_columns=("security_id", "url"),
        conflict_where=canonical_news.c.url.isnot(None),
    )
    result.inserted += _insert(
        connection,
        without_url,
        batch_size=batch_size,
        conflict_columns=("security_id", "headline", "event_time"),
        conflict_where=canonical_news.c.url.is_(None),
    )
    return result


def _insert(
    connection: Connection,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    conflict_columns: tuple[str, ...],
    conflict_where: Any,
) -> int:
    inserted = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        if not batch:
            continue
        statement = (
            insert(canonical_news)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=list(conflict_columns),
                index_where=conflict_where,
            )
            .returning(canonical_news.c.id)
        )
        inserted += len(connection.execute(statement).fetchall())
    return inserted


def _values(article: CanonicalNewsArticle) -> dict[str, Any]:
    """One row, mirroring `canonical_news` exactly.

    The table has no `published_at` column and does not need one:
    Module 19's schema notes that for an article `event_time` and
    `observation_time` are both the instant of publication, which is
    what `translate_news` sets.
    """
    return {
        "security_id": article.security_id,
        **article.pit.as_columns(),
        "headline": article.headline,
        "source_site": article.source_site,
        "url": article.url,
        "summary": article.summary,
        # Serialized rather than handed over as the model: `lineage` is a
        # JSONB column and `SourceLineage` is a Pydantic object, which
        # psycopg cannot encode. Written at all — unlike `CanonicalWriter`,
        # which omits lineage on the three record types it handles — because
        # Module 19's schema documents the column as carrying provider and
        # endpoint "for auditing a row back to its origin", and a column
        # that is always the empty default cannot do that.
        "lineage": article.lineage.model_dump(mode="json"),
    }
