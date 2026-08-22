"""Stamping a schema version, and writing vectors that cite it.

The reproducibility guarantee ARGUS makes is narrow and precise: a signal
that recorded a `feature_schema_version_id` can be re-run years later and
produce the identical number. Two things have to hold for that, and both
are tested here.

**A version is identified by its content, not by when it was published.**
Publishing the same spec twice must return the same ID, and publishing a
changed one must return a new row rather than editing the old — otherwise
a version ID stops naming a fixed definition and the guarantee is empty.

**A vector cannot be written without one.** An unstamped vector is a
number with no recoverable definition. Refusing to persist it is the only
way the table stays trustworthy, so the refusal is asserted rather than
left to convention.

The round trip through Module 07's `get_feature_vector_as_of` is included
deliberately: this module writes the table that module reads, and a
serialization mismatch between the two would be invisible to either
alone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.data_validation.feature_vectors import get_feature_vector_as_of
from core.data_validation.result import MissReason
from core.feature_engine.engine import compute_features_batch
from core.feature_engine.persistence import write_feature_vectors
from core.feature_engine.spec import (
    FeatureSpec,
    FeatureWindows,
    publish_feature_schema_version,
)
from infra.db.schema.versioning import feature_schema_version
from tests.integration.feature_engine.conftest import insert_bars

AS_OF = datetime(2024, 6, 3, tzinfo=UTC)


@pytest.fixture
def seasoned(connection: Connection, register) -> UUID:
    security_id = register("PERSIST")
    insert_bars(
        connection,
        security_id,
        start=datetime(2020, 6, 1),
        closes=[10.0 + (index % 40) * 0.1 for index in range(1000)],
    )
    return security_id


# --------------------------------------------------------------------------
# Publishing a schema version
# --------------------------------------------------------------------------


def test_publishing_the_same_spec_twice_returns_one_version(connection: Connection):
    """Identity is the checksum, not the act of publishing.

    Without this, every scan would mint a new version ID for identical
    arithmetic, and "which definition produced this number" would become
    unanswerable within a week of running in production.
    """
    spec = FeatureSpec()
    first = publish_feature_schema_version(connection, spec)
    second = publish_feature_schema_version(connection, spec)

    assert first == second
    rows = connection.execute(
        select(feature_schema_version.c.id).where(
            feature_schema_version.c.content_checksum == spec.content_checksum()
        )
    ).all()
    assert len(rows) == 1


def test_a_changed_spec_becomes_a_new_version_not_an_edit(connection: Connection):
    """Append-only, so the old definition survives to explain old signals.

    A 2019 signal cites the 2019 version. If publishing a changed spec
    edited that row, the signal would silently start claiming to have been
    computed by arithmetic that did not exist when it fired.
    """
    original = FeatureSpec()
    changed = FeatureSpec(windows=FeatureWindows(medium=21))

    original_id = publish_feature_schema_version(connection, original)
    changed_id = publish_feature_schema_version(connection, changed)

    assert original_id != changed_id
    stored = dict(
        connection.execute(
            select(feature_schema_version.c.id, feature_schema_version.c.content_checksum).where(
                feature_schema_version.c.id.in_([original_id, changed_id])
            )
        ).all()
    )
    assert stored[original_id] == original.content_checksum()
    assert stored[changed_id] == changed.content_checksum()


def test_the_stored_definition_is_enough_to_rebuild_the_spec(connection: Connection):
    """A checksum proves two runs agree; the definition says what they agreed on.

    Storing only the hash would make a mismatch detectable but not
    diagnosable — you would know the arithmetic changed and have no way to
    see how.
    """
    spec = FeatureSpec()
    version_id = publish_feature_schema_version(connection, spec, description="test")

    definition = connection.execute(
        select(feature_schema_version.c.definition).where(feature_schema_version.c.id == version_id)
    ).scalar_one()

    assert definition["windows"]["structural"] == spec.windows.structural
    assert definition["tolerances"]["level_test"] == spec.tolerances.level_test
    assert definition["trading_periods_per_year"] == spec.trading_periods_per_year
    assert len(definition["feature_names"]) == 50


# --------------------------------------------------------------------------
# Writing vectors
# --------------------------------------------------------------------------


def test_an_unstamped_batch_is_refused(connection: Connection, seasoned: UUID):
    """No schema version, no write. The one hard precondition.

    Persisting an unstamped vector would put a number in the table with no
    recoverable definition — indistinguishable from a stamped one to every
    downstream reader, and impossible to reproduce.
    """
    result = compute_features_batch(connection, [seasoned], AS_OF)
    assert result.feature_schema_version_id is None

    with pytest.raises(ValueError, match="feature_schema_version_id"):
        write_feature_vectors(connection, result)


def test_vectors_are_written_and_read_back_identically(connection: Connection, seasoned: UUID):
    """The round trip through Module 07's reader, closing the loop."""
    version_id = publish_feature_schema_version(connection, FeatureSpec())
    result = compute_features_batch(
        connection, [seasoned], AS_OF, feature_schema_version_id=version_id
    )

    assert write_feature_vectors(connection, result) == 1

    computed_vector = result.vectors[seasoned]
    stored = get_feature_vector_as_of(
        connection, seasoned, version_id, computed_vector.event_time, AS_OF
    )
    assert stored.found

    computed = computed_vector.available_features()
    for name, value in computed.items():
        assert stored.unwrap().features[name] == pytest.approx(value)


def test_rewriting_the_same_batch_inserts_nothing(connection: Connection, seasoned: UUID):
    """Insert-only and idempotent: a re-run is safe, and never an overwrite.

    `DO UPDATE` would be the tempting choice, and it would mean a signal's
    cited vector could change after the signal fired.
    """
    version_id = publish_feature_schema_version(connection, FeatureSpec())
    result = compute_features_batch(
        connection, [seasoned], AS_OF, feature_schema_version_id=version_id
    )

    assert write_feature_vectors(connection, result) == 1
    assert write_feature_vectors(connection, result) == 0


def test_the_evidence_is_stored_alongside_the_features(connection: Connection, seasoned: UUID):
    """Module 09 must be able to trace INSUFFICIENT_EVIDENCE back to a cause.

    Storing the features without the evidence would leave a reader unable
    to tell a vector computed from 1,000 bars from one computed from 30.
    """
    version_id = publish_feature_schema_version(connection, FeatureSpec())
    result = compute_features_batch(
        connection, [seasoned], AS_OF, feature_schema_version_id=version_id
    )
    write_feature_vectors(connection, result)

    stored = get_feature_vector_as_of(
        connection, seasoned, version_id, result.vectors[seasoned].event_time, AS_OF
    ).unwrap()
    evidence = stored.features["_evidence"]

    assert evidence["bars_required"] == FeatureSpec().windows.max_lookback
    assert evidence["has_sufficient_history"] is True
    # Compared against the enum rather than a literal: the stored form is
    # the MissReason's value, and hardcoding it here would let a rename
    # silently change what Module 09 reads back.
    assert evidence["missing_inputs"]["sector_benchmark"] == MissReason.NEVER_INGESTED.value


def test_a_vector_with_nothing_computable_is_not_written(connection: Connection, register):
    """An all-None vector asserts nothing, so it must not become a row.

    A row in `feature_vectors` is a claim that a security had features on
    a date. Writing one built entirely of absences would make that claim
    falsely.
    """
    version_id = publish_feature_schema_version(connection, FeatureSpec())
    empty = register("NOBARS")
    result = compute_features_batch(
        connection, [empty], AS_OF, feature_schema_version_id=version_id
    )

    assert write_feature_vectors(connection, result) == 0


def test_availability_time_is_carried_into_storage(connection: Connection, seasoned: UUID):
    """Module 03 defines it as the max across inputs; the row must record it.

    Carrying it means a stored vector's PIT-correctness can be checked
    later without re-deriving its entire input set — which is what makes
    the audit trail usable rather than theoretical.
    """
    version_id = publish_feature_schema_version(connection, FeatureSpec())
    result = compute_features_batch(
        connection, [seasoned], AS_OF, feature_schema_version_id=version_id
    )
    write_feature_vectors(connection, result)

    stored = get_feature_vector_as_of(
        connection, seasoned, version_id, result.vectors[seasoned].event_time, AS_OF
    ).unwrap()
    assert stored.availability_time == result.vectors[seasoned].availability_time
    assert stored.availability_time <= AS_OF
