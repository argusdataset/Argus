"""The immutability guarantees, proven against a real database.

These are the tests that matter most in Module 03. ARGUS's promise that a
failed setup can never be quietly erased, and that a published
configuration can never be edited out from under the signals that cite
it, is only worth as much as the database's willingness to refuse the
statement. So each test issues the forbidden statement and asserts the
database rejects it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from infra.db.append_only import APPEND_ONLY_TABLES, NO_DELETE_TABLES

# The guard raises SQLSTATE 23001 (restrict_violation). Class 23 is
# "integrity constraint violation", which psycopg surfaces as
# psycopg.errors.RestrictViolation and SQLAlchemy wraps as IntegrityError.
REJECTED = IntegrityError


def _insert_audit_row(engine: Engine) -> uuid.UUID:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO audit_log (action, payload) "
                "VALUES ('test.action', '{}'::jsonb) RETURNING id"
            )
        ).scalar_one()


def _seed_setup(engine: Engine) -> uuid.UUID:
    """Insert the minimal FK graph needed to create one setup."""
    with engine.begin() as conn:
        suffix = uuid.uuid4().hex[:8]
        security_id = conn.execute(
            text("INSERT INTO security_identity (name) VALUES ('Test Co') RETURNING id")
        ).scalar_one()
        model_id = conn.execute(
            text(
                "INSERT INTO target_model_version (version_label, definition, content_checksum) "
                "VALUES (:label, '{}'::jsonb, 'sha') RETURNING id"
            ),
            {"label": f"target-model-test-{suffix}"},
        ).scalar_one()
        detection_id = conn.execute(
            text(
                "INSERT INTO detection_configuration (version_label, definition, content_checksum) "
                "VALUES (:label, '{}'::jsonb, 'sha') RETURNING id"
            ),
            {"label": f"detection-test-{suffix}"},
        ).scalar_one()
        universe_id = conn.execute(
            text(
                "INSERT INTO universe_version (version_label, definition, as_of_date) "
                "VALUES (:label, '{}'::jsonb, now()) RETURNING id"
            ),
            {"label": f"universe-test-{suffix}"},
        ).scalar_one()
        # Migration 0007 made feature_schema_version_id NOT NULL on setups.
        schema_id = conn.execute(
            text(
                "INSERT INTO feature_schema_version (version_label, definition, "
                "content_checksum) VALUES (:label, '{}'::jsonb, 'sha') RETURNING id"
            ),
            {"label": f"features-test-{suffix}"},
        ).scalar_one()
        return conn.execute(
            text(
                "INSERT INTO setups (security_id, detected_at, target_model_version_id, "
                "detection_configuration_id, universe_version_id, feature_schema_version_id) "
                "VALUES (:sec, now(), :model, :det, :uni, :fsv) RETURNING id"
            ),
            {
                "sec": security_id,
                "model": model_id,
                "det": detection_id,
                "uni": universe_id,
                "fsv": schema_id,
            },
        ).scalar_one()


def _seed_snapshot(engine: Engine) -> uuid.UUID:
    """A data_snapshot row, which setup_outcomes now requires.

    Migration 0006 made `data_snapshot_id` NOT NULL: an outcome nobody can
    re-derive is the one result in ARGUS that must not exist.
    """
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO data_snapshot (version_label, as_of_time, definition, "
                "content_checksum) VALUES (:label, now(), '{}'::jsonb, :sum) RETURNING id"
            ),
            {"label": f"snapshot-test-{uuid.uuid4().hex[:8]}", "sum": uuid.uuid4().hex},
        ).scalar_one()


# --------------------------------------------------------------------------
# Append-only tables: INSERT allowed, everything else refused
# --------------------------------------------------------------------------


def test_append_only_table_accepts_insert(engine: Engine):
    assert _insert_audit_row(engine) is not None


def test_append_only_table_rejects_update(engine: Engine):
    row_id = _insert_audit_row(engine)
    with pytest.raises(REJECTED) as exc_info, engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
            {"id": row_id},
        )
    assert "append-only" in str(exc_info.value)


def test_append_only_table_rejects_delete(engine: Engine):
    row_id = _insert_audit_row(engine)
    with pytest.raises(REJECTED) as exc_info, engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})
    assert "append-only" in str(exc_info.value)


def test_append_only_table_rejects_truncate(engine: Engine):
    """TRUNCATE does not fire row-level triggers, so it needs its own guard."""
    _insert_audit_row(engine)
    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log"))


def test_row_survives_a_rejected_delete(engine: Engine):
    """The point of the guard: the data is still there afterwards."""
    row_id = _insert_audit_row(engine)
    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})

    with engine.connect() as conn:
        still_there = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    assert still_there == 1


def test_published_configuration_cannot_be_edited(engine: Engine):
    """A scoring configuration is what makes a signal reproducible."""
    with engine.begin() as conn:
        config_id = conn.execute(
            text(
                "INSERT INTO scoring_configuration (version_label, definition, content_checksum) "
                "VALUES (:label, '{\"pattern_quality\": 0.25}'::jsonb, 'sha') RETURNING id"
            ),
            {"label": f"scoring-test-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()

    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(
            text("UPDATE scoring_configuration SET definition = '{}'::jsonb WHERE id = :id"),
            {"id": config_id},
        )


# --------------------------------------------------------------------------
# No-delete tables: UPDATE allowed (review classification), DELETE refused
# --------------------------------------------------------------------------


def test_setup_cannot_be_deleted(engine: Engine):
    """A failed setup is permanent record — this is the core promise."""
    setup_id = _seed_setup(engine)
    with pytest.raises(REJECTED) as exc_info, engine.begin() as conn:
        conn.execute(text("DELETE FROM setups WHERE id = :id"), {"id": setup_id})
    assert "append-only" in str(exc_info.value)


def test_setup_outcome_cannot_be_deleted(engine: Engine):
    setup_id = _seed_setup(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO setup_outcomes (setup_id, outcome_status, data_snapshot_id) "
                "VALUES (:id, 'FAILED', :snapshot)"
            ),
            {"id": setup_id, "snapshot": _seed_snapshot(engine)},
        )

    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(text("DELETE FROM setup_outcomes WHERE setup_id = :id"), {"id": setup_id})


def test_setup_outcome_allows_review_classification_update(engine: Engine):
    """The deliberate asymmetry: erasing is blocked, reviewing is not."""
    setup_id = _seed_setup(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO setup_outcomes (setup_id, outcome_status, data_snapshot_id) "
                "VALUES (:id, 'FAILED', :snapshot)"
            ),
            {"id": setup_id, "snapshot": _seed_snapshot(engine)},
        )
        conn.execute(
            text(
                "UPDATE setup_outcomes SET review_confidence = 'HIGH', false_positive_type = 'C' "
                "WHERE setup_id = :id"
            ),
            {"id": setup_id},
        )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT review_confidence, false_positive_type FROM setup_outcomes "
                "WHERE setup_id = :id"
            ),
            {"id": setup_id},
        ).one()
    assert row.review_confidence == "HIGH"
    assert row.false_positive_type == "C"


# --------------------------------------------------------------------------
# Drift: the installed guards must still match the declared intent
# --------------------------------------------------------------------------


def test_installed_guards_match_declared_tables(engine: Engine):
    """Catch a table added to append_only.py without a matching migration.

    The migration freezes its own copy of these lists (migrations are
    historical records and must not change when the module is edited), so
    this test is what stops the two drifting apart silently.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname AS table_name, t.tgname AS trigger_name "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal"
            )
        ).all()

    guarded_append_only = {r.table_name for r in rows if r.trigger_name.endswith("_append_only")}
    guarded_no_delete = {r.table_name for r in rows if r.trigger_name.endswith("_no_delete")}
    guarded_truncate = {r.table_name for r in rows if r.trigger_name.endswith("_no_truncate")}

    assert guarded_append_only == set(APPEND_ONLY_TABLES)
    assert guarded_no_delete == set(NO_DELETE_TABLES)
    # Every guarded table blocks TRUNCATE, whichever strength it uses.
    assert guarded_truncate == set(APPEND_ONLY_TABLES) | set(NO_DELETE_TABLES)


# --------------------------------------------------------------------------
# Migration 0018: the Terminal's Ultimate-plan tables joined the guard
# --------------------------------------------------------------------------


#: One insertable row per table migration 0018 added. Written out rather
#: than generated, because a row-level trigger only fires on a row: an
#: UPDATE matching nothing succeeds trivially and would prove nothing.
_MIGRATION_0018_ROWS: dict[str, str] = {
    "canonical_disclosures": (
        "INSERT INTO canonical_disclosures "
        "(security_id, disclosure_type, fiscal_period, event_time, observation_time, "
        "availability_time, ingestion_time, data) "
        "VALUES (:sid, 'ANALYST_ESTIMATES', :label, now(), now(), now(), now(), '{}'::jsonb)"
    ),
    "canonical_snapshots": (
        "INSERT INTO canonical_snapshots "
        "(security_id, snapshot_type, event_time, observation_time, availability_time, "
        "ingestion_time, data) "
        "VALUES (:sid, :label, now(), now(), now(), now(), '{}'::jsonb)"
    ),
    "analyst_grades": (
        "INSERT INTO analyst_grades "
        "(security_id, event_time, observation_time, availability_time, ingestion_time, "
        "grading_company, data) "
        "VALUES (:sid, now(), now(), now(), now(), :label, '{}'::jsonb)"
    ),
    "technical_indicators": (
        "INSERT INTO technical_indicators "
        "(security_id, indicator, period_length, timeframe, event_time, observation_time, "
        "availability_time, ingestion_time, data) "
        "VALUES (:sid, :label, 14, '1day', now(), now(), now(), now(), '{}'::jsonb)"
    ),
}


def _seed_0018_row(engine: Engine, table: str) -> None:
    """One row in `table`, so an UPDATE has something to fire the guard on."""
    with engine.begin() as conn:
        security_id = conn.execute(text("SELECT id FROM security_identity LIMIT 1")).scalar_one()
        conn.execute(
            text(_MIGRATION_0018_ROWS[table]),
            {"sid": security_id, "label": f"guard-{uuid.uuid4().hex[:8]}"},
        )


@pytest.mark.parametrize(
    "table",
    ["canonical_disclosures", "canonical_snapshots", "analyst_grades", "technical_indicators"],
)
def test_migration_0018_tables_refuse_an_update(engine: Engine, table: str):
    """The guard fires on the tables migration 0018 added, in the database.

    `test_installed_guards_match_declared_tables` proves a trigger with
    the right *name* exists. This proves it does something — the two are
    not the same claim, and a trigger created against the wrong function
    would satisfy the first and not the second.

    It matters more here than for most guarded tables:
    `services/terminal/stored.py` answers "the latest revision knowable
    at this instant" by keeping every earlier observation. An UPDATE that
    succeeded would not just lose history, it would make that query
    return an answer that was never true.
    """
    _seed_0018_row(engine, table)

    with pytest.raises(REJECTED) as exc_info, engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET ingestion_time = now()"))

    assert "append-only" in str(exc_info.value)


@pytest.mark.parametrize(
    "table",
    ["canonical_disclosures", "canonical_snapshots", "analyst_grades", "technical_indicators"],
)
def test_migration_0018_tables_refuse_a_truncate(engine: Engine, table: str):
    """TRUNCATE fires no row-level trigger, so it needs its own guard."""
    _seed_0018_row(engine, table)

    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table}"))


# --------------------------------------------------------------------------
# Completeness: the drift test's blind spot, closed
#
# `test_installed_guards_match_declared_tables` above compares the
# triggers the database has against the tables `append_only.py` declares.
# Both sides are lists, so a table missing from *both* satisfies it —
# which is exactly what happened. Migration 0017 created `sec_filings`,
# `insider_trades` and `institutional_ownership` with no triggers and
# added them to no list, and every assertion still passed.
#
# The test below starts from the schema instead. Any table carrying the
# full PIT column set is, by that fact, a record of what ARGUS knew at an
# instant, and must be guarded — or be named in `PIT_GUARD_EXCEPTIONS`
# with a reason somebody wrote down. Omission stops being an option.
# --------------------------------------------------------------------------

#: The four columns that together make a table a point-in-time record.
#: `infra/db/metadata.py`'s `pit_columns()` emits exactly these, so a
#: table carrying all four was built as PIT evidence whatever else it is.
PIT_COLUMNS = frozenset({"event_time", "observation_time", "availability_time", "ingestion_time"})


def _pit_tables() -> set[str]:
    import infra.db.schema  # noqa: F401 - registers every table on the metadata
    from infra.db.metadata import metadata

    return {
        table.name for table in metadata.tables.values() if set(table.columns.keys()) >= PIT_COLUMNS
    }


def test_every_point_in_time_table_is_guarded():
    """The check that would have caught the 0017 gap on the day it landed.

    A PIT table is one ARGUS reads with `availability_time <= as_of` to
    answer "what was knowable then". An `UPDATE` on one does not lose a
    row, it rewrites that answer — and every historical claim computed
    from it afterwards becomes uncheckable.

    Failing here means one of two things and both need a decision: either
    add the table to `APPEND_ONLY_TABLES` and write the migration, or add
    it to `PIT_GUARD_EXCEPTIONS` with the reason it is genuinely
    different. What it must not be is neither.
    """
    from infra.db.append_only import (
        APPEND_ONLY_TABLES,
        NO_DELETE_TABLES,
        PIT_GUARD_EXCEPTIONS,
    )

    guarded = set(APPEND_ONLY_TABLES) | set(NO_DELETE_TABLES)
    unguarded = _pit_tables() - guarded - set(PIT_GUARD_EXCEPTIONS)

    assert unguarded == set(), (
        f"these tables carry PIT columns and no mutation guard: {sorted(unguarded)}. "
        "Add them to APPEND_ONLY_TABLES with a migration, or name them in "
        "PIT_GUARD_EXCEPTIONS with the reason they are different."
    )


def test_the_predicate_finds_the_tables_it_is_supposed_to_find():
    """A guard on a predicate that matched nothing would pass forever.

    The assertion above is a subtraction, so it is trivially satisfied by
    an empty left-hand side — a renamed PIT column, a metadata import
    that stopped registering, and the whole check silently becomes a
    tautology. This is the second half: the scan really does see the
    canonical tables everyone knows are PIT records.
    """
    found = _pit_tables()

    assert {"canonical_ohlcv", "canonical_fundamentals", "canonical_news"} <= found
    # Every table the audit named, now included by the same predicate
    # that missed nothing about them before — they were always PIT
    # tables; only the guard was absent.
    assert {
        "sec_filings",
        "insider_trades",
        "institutional_ownership",
        "pending_material_events",
    } <= found


def test_an_exception_must_carry_a_reason():
    """`PIT_GUARD_EXCEPTIONS` is a dict rather than a tuple on purpose.

    An exception list of bare names accumulates entries nobody can
    justify later. Requiring a value makes the justification part of
    adding one, and this asserts the values are real sentences rather
    than empty strings put there to satisfy the type.
    """
    from infra.db.append_only import PIT_GUARD_EXCEPTIONS

    for table, reason in PIT_GUARD_EXCEPTIONS.items():
        assert reason.strip(), f"{table} is excepted with no reason given"


@pytest.mark.parametrize("table", ["sec_filings", "insider_trades", "institutional_ownership"])
def test_migration_0019_tables_refuse_a_truncate(engine: Engine, table: str):
    """The guard 0019 installed, firing in the database.

    TRUNCATE rather than UPDATE because it is statement-level and needs
    no row to fire, which keeps this independent of what these tables
    happen to contain.
    """
    with pytest.raises(REJECTED), engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table}"))
