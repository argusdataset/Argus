"""let outcomes be recomputed, and give setups their own lineage

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-23

Three corrections Module 15's report flagged as "decide before the first
large scan, because it is a schema change afterwards". Module 17 is that
scan's engine, so the deadline has arrived.

## `setup_outcomes` could only ever be computed once

Module 15 wrote `record_outcome` insert-only against `setup_id` alone.
That is the right default for a table nobody may quietly rewrite, but it
also means a second run writes **nothing** — and Module 15's success
criterion (+10% / -5% / 60 days) is explicitly an invented, revisable
placeholder. Module 17 exists to produce the evidence that revises it, and
then has to relabel the dataset.

Two shapes were available. This migration takes the second:

**A supersession chain**, matching migration 0005's `supersedes_signal_id`
on `signals`. Rejected here, for two reasons specific to this table.
First, `signals` needs a chain because a correction there is a *fix* — the
original was wrong and something must record which row replaced it. An
outcome recomputed under a different criterion is not a correction: both
rows are right, each about its own definition of success. Second,
`setup_outcomes` is the one result table that permits UPDATE (Module 03's
deliberate asymmetry, so a reviewer can assign `review_confidence` and
`false_positive_type` and revise them on re-review). Under a chain, a
reviewer would have to work out which row is current before touching it,
and the schema offers no way to stop them updating a superseded one.

**`(setup_id, data_snapshot_id)` uniqueness.** Taken, because
`data_snapshot_id` — added to this table one migration ago — already pins
exactly what a recomputation varies: the PIT cutoff and the outcome
definition in force. One outcome per setup per snapshot is therefore the
literal statement "this setup, evaluated under these rules, resolved this
way", and re-running under a revised criterion means publishing a new
snapshot, which the reproducibility guarantee requires anyway. It also
keeps the reviewer's job addressable: a row is identified by the pair, not
by its position in a chain.

The cost is real and worth naming: "the outcome of setup X" is no longer a
single row, so every reader must say which snapshot it means, or order
deterministically. `core/historical_similarity/cases.py` is the one
existing reader affected and is fixed in the same commit — its
`DISTINCT ON (s.id)` previously relied on this uniqueness for
determinism.

## `setups` recorded no feature schema version

Every other lineage ID a setup needs is a column; `feature_schema_version`
was left to the caller to supply per-call. For a replay spanning years and
several schema versions — precisely Module 17's job — "the caller
remembered correctly" is not a record. NOT NULL, like the other five,
because `Lineage` has always carried it.

## `setups` had no join key back to the qualifying signal

Module 15 read Module 14's QUALIFICATION event payload instead, and
recommended this column. Correlating `argus_score` against realized
outcomes at scale needs a join key, not a JSON copy that can drift from
the `signals` row it was copied from.

Nullable, and that is not laziness: a setup at DETECTION has not
qualified, and today essentially every setup is at DETECTION (Module 14's
bootstrap analysis). NULL here means "not qualified yet", which is a fact,
not a missing value.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOME_IDENTITY = "uq_setup_outcomes_setup_snapshot"
OLD_OUTCOME_UNIQUE = "uq_setup_outcomes_setup_id"


def upgrade() -> None:
    # -- Correction 1: one outcome per setup *per snapshot*
    op.drop_constraint(OLD_OUTCOME_UNIQUE, "setup_outcomes", type_="unique")
    op.create_unique_constraint(
        OUTCOME_IDENTITY,
        "setup_outcomes",
        ["setup_id", "data_snapshot_id"],
    )
    # The dropped constraint's implicit index was the only one on
    # setup_id; the new constraint's index leads with the same column, so
    # lookups by setup alone are still served.

    # -- Correction 2: a setup records its own feature schema version
    op.add_column(
        "setups",
        sa.Column("feature_schema_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_setups_feature_schema_version_id"),
        "setups",
        "feature_schema_version",
        ["feature_schema_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Two steps, as in 0006: a non-empty table fails loudly here rather
    # than acquiring a guessed-at version. There is nothing honest to
    # backfill with — which schema version a historical setup was detected
    # under is exactly the fact that was not recorded.
    op.alter_column("setups", "feature_schema_version_id", nullable=False)

    # -- Correction 2 (second half): the signal that qualified the setup
    op.add_column(
        "setups",
        sa.Column("qualifying_signal_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_setups_qualifying_signal_id"),
        "setups",
        "signals",
        ["qualifying_signal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_setups_qualifying_signal", "setups", ["qualifying_signal_id"])


def downgrade() -> None:
    op.drop_index("ix_setups_qualifying_signal", table_name="setups")
    op.drop_constraint(op.f("fk_setups_qualifying_signal_id"), "setups", type_="foreignkey")
    op.drop_column("setups", "qualifying_signal_id")
    op.drop_constraint(op.f("fk_setups_feature_schema_version_id"), "setups", type_="foreignkey")
    op.drop_column("setups", "feature_schema_version_id")
    op.drop_constraint(OUTCOME_IDENTITY, "setup_outcomes", type_="unique")
    op.create_unique_constraint(OLD_OUTCOME_UNIQUE, "setup_outcomes", ["setup_id"])
