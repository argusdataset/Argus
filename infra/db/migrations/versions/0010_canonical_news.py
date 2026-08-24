"""give news articles somewhere to live

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24

Module 05 defined `CanonicalNewsArticle` and `translate_news`, and both
carry the same note: "Module 03 defines no news table, so nothing persists
these." Module 05 flagged the gap rather than inventing a table outside
its own scope, and named Module 19 as the consumer that would need one.
This clears that flag.

The columns mirror the record type exactly. Nothing is added or renamed.

Not append-only-guarded, matching the other canonical tables: Module 03's
`canonical_*` guard is applied by migration 0003 to the tables it knew
about, and this one joins them there rather than inventing its own rule —
see `upgrade()`.

An article is not restated, so there is no `restates_id` here and no
"latest revision of this fact" query. A correction is a new article and
the original stays as published. What remains is the ordinary PIT filter
every canonical table carries.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "canonical_news"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("source_site", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "lineage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security_identity.id"],
            name=op.f("fk_canonical_news_security_id"),
            ondelete="RESTRICT",
        ),
        comment="Canonical news articles for the Terminal (Module 19). Never scored.",
    )

    # Two partial unique indexes rather than one constraint over a
    # nullable column: a provider that omits URLs would otherwise have
    # every one of its articles collide on a single NULL.
    op.create_index(
        "uq_news_url",
        TABLE,
        ["security_id", "url"],
        unique=True,
        postgresql_where=sa.text("url IS NOT NULL"),
    )
    op.create_index(
        "uq_news_headline",
        TABLE,
        ["security_id", "headline", "event_time"],
        unique=True,
        postgresql_where=sa.text("url IS NULL"),
    )
    op.create_index("ix_news_security_time", TABLE, ["security_id", "event_time"])
    op.create_index("ix_news_availability", TABLE, ["availability_time", "security_id"])

    # Joins the other canonical tables under migration 0003's guard. A
    # published article is a historical fact like a bar or a filing: a
    # correction is a new row, and silently editing one would rewrite what
    # ARGUS could have known at a past instant.
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_append_only
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION argus_reject_mutation();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {TABLE}_no_truncate
        BEFORE TRUNCATE ON {TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION argus_reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE}")
    op.drop_index("ix_news_availability", table_name=TABLE)
    op.drop_index("ix_news_security_time", table_name=TABLE)
    op.drop_index("uq_news_headline", table_name=TABLE)
    op.drop_index("uq_news_url", table_name=TABLE)
    op.drop_table(TABLE)
