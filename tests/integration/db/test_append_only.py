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
