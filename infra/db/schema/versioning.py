"""Version and configuration entities — the backbone of reproducibility.

Every table here is immutable and append-only once written (enforced by
the guards in `infra.db.append_only`). The reason is narrow and
important: a signal records the IDs of the configuration that produced
it, and re-running that signal must yield an identical result. If a
published scoring configuration can be edited in place, every historical
signal that referenced it silently becomes unverifiable — and ARGUS's
whole claim rests on those historical results being checkable.

Correcting a configuration therefore means publishing a *new* version,
never editing an existing one.

Each table stores a `definition` payload plus a `content_checksum`. The
checksum is what makes "this configuration was not tampered with" a
verifiable statement rather than a trusted one; the modules that publish
these versions (08 for feature schemas, 10/13 for models and scoring
configs) compute it.
"""

from __future__ import annotations

from sqlalchemy import (
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

from infra.db.metadata import metadata


def _version_columns() -> list[Column]:
    """Columns shared by every immutable version/configuration entity."""
    return [
        Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
        Column("version_label", Text, nullable=False),
        Column("definition", JSONB, nullable=False),
        Column("content_checksum", Text, nullable=False),
        Column("description", Text, nullable=True),
        Column("published_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


feature_schema_version = Table(
    "feature_schema_version",
    metadata,
    *_version_columns(),
    UniqueConstraint("version_label", name="uq_feature_schema_version_label"),
    comment="Immutable feature-vector schema versions (Module 08 publishes these).",
)

target_model_version = Table(
    "target_model_version",
    metadata,
    *_version_columns(),
    UniqueConstraint("version_label", name="uq_target_model_version_label"),
    comment="Immutable target-model versions, e.g. target-model-v1 (Module 10).",
)

scoring_configuration = Table(
    "scoring_configuration",
    metadata,
    *_version_columns(),
    UniqueConstraint("version_label", name="uq_scoring_configuration_label"),
    comment="Immutable scoring configurations: component weights etc. (Module 13).",
)

detection_configuration = Table(
    "detection_configuration",
    metadata,
    *_version_columns(),
    UniqueConstraint("version_label", name="uq_detection_configuration_label"),
    comment="Immutable candidate-detection and eligibility-gate configurations (Module 09).",
)

data_snapshot = Table(
    "data_snapshot",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("version_label", Text, nullable=False, unique=True),
    # The PIT cutoff this snapshot represents. Re-running any computation
    # against this snapshot must see exactly the rows whose
    # availability_time <= as_of_time, which is what makes a historical
    # result reproducible rather than merely repeatable.
    Column("as_of_time", DateTime(timezone=True), nullable=False),
    Column("definition", JSONB, nullable=False),
    Column("content_checksum", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_data_snapshot_as_of", "as_of_time"),
    comment="Immutable point-in-time data cutoff a computation ran against.",
)

feature_vectors = Table(
    "feature_vectors",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "security_id",
        UUID(as_uuid=True),
        ForeignKey("security_identity.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "feature_schema_version_id",
        UUID(as_uuid=True),
        ForeignKey("feature_schema_version.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # The bar/date the feature vector describes.
    Column("event_time", DateTime(timezone=True), nullable=False),
    # The latest availability_time across every input that fed this
    # vector. Carrying it here is what lets Module 07 verify a vector was
    # computed only from data that existed by a given date, without
    # re-deriving the whole input set.
    Column("availability_time", DateTime(timezone=True), nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Feature values keyed by the names the feature_schema_version
    # defines. Module 08 owns those names.
    Column("features", JSONB, nullable=False),
    UniqueConstraint(
        "security_id",
        "feature_schema_version_id",
        "event_time",
        name="uq_feature_vector",
    ),
    Index("ix_feature_vectors_security_time", "security_id", "event_time"),
    Index("ix_feature_vectors_availability", "availability_time", "security_id"),
    Index("ix_feature_vectors_schema", "feature_schema_version_id", "event_time"),
    comment="Computed feature vectors, stamped with the schema version that defined them.",
)
