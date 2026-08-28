"""The Live Scanner as a deployed process. Mostly about what it refuses to do.

Module 18 built a scanner that takes a `Lineage`. A deployed process has
to produce one, and five of its six parts come from this build's own
configuration. The sixth — `universe_version_id` — is a fact about the
market rather than about the code, and choosing it automatically would
mean writing `ORDER BY created_at DESC LIMIT 1` over a column defaulting
to `now()` with a random-UUID tiebreak. That is the ordering hazard
Module 23 catalogued four instances of and Module 25 was told not to add
a fifth of.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from infra.deploy.migrate import upgrade_to_head
from infra.deploy.scanner import (
    UNIVERSE_VERSION_ENV_VAR,
    ScannerNotReady,
    ScannerSettings,
    build_lineage,
    resolve_universe_version,
)


@pytest.fixture
def migrated(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def _universe(engine: Engine, label: str) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text(
                    "INSERT INTO universe_version "
                    "(version_label, definition, as_of_date) "
                    "VALUES (:label, '{}'::jsonb, now()) RETURNING id"
                ),
                {"label": label},
            ).scalar_one()
        )


def test_it_refuses_to_run_when_no_universe_version_is_named(migrated: Engine):
    """Deliberate operational cost: somebody sets a variable after a universe build.

    It buys the property that a production scan's lineage was chosen by a
    person rather than by whichever row a non-deterministic sort happened
    to return.
    """
    with (
        migrated.connect() as connection,
        pytest.raises(ScannerNotReady, match=UNIVERSE_VERSION_ENV_VAR),
    ):
        resolve_universe_version(connection, None)


def test_the_refusal_says_no_universe_exists_yet_when_none_does(migrated: Engine):
    with migrated.connect() as connection, pytest.raises(ScannerNotReady, match="Module 06"):
        resolve_universe_version(connection, None)


def test_it_still_refuses_when_versions_exist_to_choose_from(migrated: Engine):
    """The case that actually matters, and the one an empty database hides.

    With no universe at all, any implementation refuses — including a
    wrong one that would have fallen back to "the most recent". This is
    the test that fails if somebody adds that fallback: two versions
    exist, one of them would be returned, and the whole reason not to is
    that which one is a coin flip.
    """
    _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")
    _universe(migrated, "universe-2026-08-20-bbbbbbbbbbbb")

    with (
        migrated.connect() as connection,
        pytest.raises(ScannerNotReady, match=UNIVERSE_VERSION_ENV_VAR),
    ):
        resolve_universe_version(connection, None)


def test_the_refusal_lists_what_could_be_chosen(migrated: Engine):
    """An error an operator can act on without opening a database client."""
    _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")
    _universe(migrated, "universe-2026-08-20-bbbbbbbbbbbb")

    with migrated.connect() as connection, pytest.raises(ScannerNotReady) as refused:
        resolve_universe_version(connection, None)

    assert "universe-2026-08-20-bbbbbbbbbbbb" in str(refused.value)


def test_a_version_can_be_named_by_its_label(migrated: Engine):
    """Which is what an operator actually has in front of them."""
    expected = _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")

    with migrated.connect() as connection:
        found = resolve_universe_version(connection, "universe-2026-08-01-aaaaaaaaaaaa")

    assert str(found) == expected


def test_a_version_can_be_named_by_its_id(migrated: Engine):
    expected = _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")

    with migrated.connect() as connection:
        assert str(resolve_universe_version(connection, expected)) == expected


def test_a_name_that_matches_nothing_is_refused_rather_than_ignored(migrated: Engine):
    _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")

    with (
        migrated.connect() as connection,
        pytest.raises(ScannerNotReady, match="matches no universe"),
    ):
        resolve_universe_version(connection, "universe-that-does-not-exist")


def test_an_unknown_uuid_is_refused_too(migrated: Engine):
    """A well-formed id is not evidence that the row exists."""
    with migrated.connect() as connection, pytest.raises(ScannerNotReady):
        resolve_universe_version(connection, str(uuid.uuid4()))


def test_the_lineage_is_stable_across_two_starts_under_unchanged_configuration(
    migrated: Engine,
):
    """Every `publish_*` is idempotent by content checksum.

    That is what makes a re-run comparable to the run it repeats — and
    what makes a cron schedule that fires twice harmless.
    """
    universe_id = _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")

    with migrated.begin() as connection:
        first = build_lineage(connection, universe_version_id=uuid.UUID(universe_id))
    with migrated.begin() as connection:
        second = build_lineage(connection, universe_version_id=uuid.UUID(universe_id))

    assert first.target_model_version_id == second.target_model_version_id
    assert first.feature_schema_version_id == second.feature_schema_version_id
    assert first.scoring_configuration_id == second.scoring_configuration_id
    assert first.detection_configuration_id == second.detection_configuration_id
    assert first.universe_version_id == second.universe_version_id


def test_settings_are_read_from_the_environment_without_touching_the_process():
    settings = ScannerSettings.from_environment(
        {UNIVERSE_VERSION_ENV_VAR: "universe-2026-08-01-aaaaaaaaaaaa"}
    )
    assert settings.universe_version == "universe-2026-08-01-aaaaaaaaaaaa"


def test_an_empty_variable_is_the_same_as_an_unset_one():
    """An unset Railway variable arrives as an empty string, not as absent."""
    assert ScannerSettings.from_environment({UNIVERSE_VERSION_ENV_VAR: ""}).universe_version is None


def test_the_snapshot_is_published_at_a_day_boundary(migrated: Engine):
    """Which is what makes a restart on the same date idempotent.

    See `_snapshot_instant`: Module 16's checksum is per-instant while its
    unique label is per-day, so only a day-stable `as_of` satisfies both.
    """
    universe_id = _universe(migrated, "universe-2026-08-01-aaaaaaaaaaaa")

    with migrated.begin() as connection:
        lineage = build_lineage(
            connection,
            universe_version_id=uuid.UUID(universe_id),
            as_of=datetime(2026, 8, 28, 22, 30, 17, 512, tzinfo=UTC),
        )
        published = connection.execute(
            text("SELECT as_of_time FROM data_snapshot WHERE id = :id"),
            {"id": lineage.data_snapshot_id},
        ).scalar_one()

    assert published == datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def test_module_16s_snapshot_publisher_collides_on_two_instants_in_one_day(
    migrated: Engine,
):
    """A defect in Module 16, recorded here rather than fixed.

    `publish_outcome_snapshot` keys its idempotence checksum on
    `as_of.isoformat()` but its uniqueness on `version_label`, which is
    `…@<date>`. Two calls on the same date at different instants miss the
    checksum lookup and then violate the unique constraint.

    Module 25's boundary is not to change Modules 03-24, so this is a
    regression test for the behaviour as it stands: if somebody fixes
    Module 16, this test fails and `_snapshot_instant` can be revisited.
    """
    from sqlalchemy.exc import IntegrityError

    from core.outcome_tracking.config import publish_outcome_snapshot

    day = datetime(2026, 8, 28, tzinfo=UTC)
    with migrated.begin() as connection:
        publish_outcome_snapshot(connection, as_of=day.replace(hour=1))

    with (
        pytest.raises(IntegrityError, match="uq_data_snapshot_version_label"),
        migrated.begin() as connection,
    ):
        publish_outcome_snapshot(connection, as_of=day.replace(hour=2))
