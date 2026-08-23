"""add a uniqueness guarantee and a detail column to signals

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-23

Corrects two gaps in the Module 03 baseline, both found while building
Module 13.

## No uniqueness

`signals` was the only result table in ARGUS without a unique constraint.
`feature_vectors`, `eligibility_check_results`,
`historical_similarity_results` and `pending_material_events` all have one
and all use it with `ON CONFLICT DO NOTHING`, so a re-run inserts what is
missing and rewrites nothing. Here a re-run would silently insert a second
row — and because the table is append-only, both rows would then be
permanent with no way to tell which one a downstream decision cited.

Module 13 compensated with a read-then-insert guard in `write_signal`.
That closes the ordinary case but is not race-proof: two concurrent
writers can both pass the check before either inserts.

The uniqueness key is the tuple that identifies **one scoring of one
candidate**: security, the instant scored, the data cutoff, and the
configuration that produced it. Two rows agreeing on all four are the same
computation run twice.

The index is **partial** — `WHERE supersedes_signal_id IS NULL`. A
correction is deliberately a second row with the same identity tuple
pointing at the row it replaces, which is the schema's existing design;
an unconditional constraint would make corrections impossible. Only the
uncorrected originals are constrained to be unique.

## No detail column

Every other result table carries a JSONB column for the layer beneath its
typed values. `signals` stored the seven component numbers but had
nowhere for what produced them: which ramp mapped which reading, why a
component was unmeasured, how much of the weight was measurable, the
calibration status of the configuration. Module 03's own comment on the
table says "a user can always see why" — this is where the rest of the
why lives. It also gives `probability`'s not-yet-calibrated status a
proper home instead of riding along inside `probability_definition`.

Defaulted to `{}` rather than left nullable, matching
`eligibility_check_results.detail`: an empty object and a missing object
are the same fact here, and having one representation avoids every reader
handling both.

The table carries append-only triggers, but ALTER TABLE is DDL rather than
a row mutation, so they do not block this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tuple identifying one scoring of one candidate.
IDENTITY_COLUMNS = (
    "security_id",
    "event_time",
    "data_snapshot_id",
    "scoring_configuration_id",
)

INDEX_NAME = "uq_signals_identity"


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Any pre-existing duplicates would make the index creation fail, and
    # failing loudly is right: they are two permanent rows for one
    # computation and a human has to decide which is authoritative.
    # Nothing has written signals yet, so this is a guard rather than a
    # migration step.
    op.create_index(
        INDEX_NAME,
        "signals",
        list(IDENTITY_COLUMNS),
        unique=True,
        postgresql_where=sa.text("supersedes_signal_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="signals")
    op.drop_column("signals", "detail")
