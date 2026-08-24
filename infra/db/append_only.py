"""Database-level immutability guards.

ARGUS makes two promises that are enforced here as *structure*, not as
convention someone has to remember:

1. A failed setup can never be deleted. Failures are how the system
   learns what a false positive looks like, and keeping them is what
   stops ARGUS's own published statistics from inheriting survivorship
   bias.
2. A signal must stay reproducible. If a published configuration or a
   written signal can be edited in place, every historical result that
   referenced it becomes unverifiable.

Two guard strengths are applied, because the two promises differ:

- APPEND_ONLY_TABLES  — reject UPDATE, DELETE and TRUNCATE. For rows that
  are immutable facts once written: event streams, published
  configuration versions, written signals, audit records.
- NO_DELETE_TABLES    — reject DELETE and TRUNCATE, but permit UPDATE.
  For records that must never be erased but do legitimately get filled
  in later, specifically the human review classification on a case
  record (review_confidence / false_positive_type in Module 15).

Both guards also block TRUNCATE. This matters: TRUNCATE does not fire row
-level triggers, so a row-only guard would leave the whole table erasable
by a single statement.

These guards intentionally do not defend against a superuser dropping the
trigger — that is a database-permissions concern, not a schema one.
"""

from __future__ import annotations

# plpgsql function every guard trigger calls. SQLSTATE 23001
# (restrict_violation) surfaces in psycopg as errors.RestrictViolation,
# so tests can assert on the type rather than on message text.
MUTATION_GUARD_FUNCTION = "argus_reject_mutation"

CREATE_GUARD_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {MUTATION_GUARD_FUNCTION}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'Table % is protected: % is not permitted. '
        'ARGUS history is append-only by design.',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;
"""

DROP_GUARD_FUNCTION_SQL = f"DROP FUNCTION IF EXISTS {MUTATION_GUARD_FUNCTION}();"

#: Reject UPDATE, DELETE and TRUNCATE — immutable once written.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    # Reproducibility: a published configuration that can be silently
    # edited invalidates every signal that recorded its ID.
    "universe_version",
    "universe_membership",
    "feature_schema_version",
    "target_model_version",
    "scoring_configuration",
    "detection_configuration",
    "data_snapshot",
    # Canonical data. A canonical row records what ARGUS believed at a
    # point in time; editing one destroys the evidence of that belief and
    # makes every historical claim computed from it uncheckable.
    # Restatements arrive as new rows with a later observation_time —
    # see infra/db/schema/canonical.py. Guards added in migration 0003.
    "canonical_ohlcv",
    "canonical_fundamentals",
    "canonical_corporate_actions",
    # Added with the table itself in migration 0010 (Module 19). A
    # published article is a historical fact like a bar or a filing: a
    # correction is a new article, and the original stays as published.
    "canonical_news",
    # Event streams and written results.
    "market_state_transitions",
    "eligibility_check_results",
    "signals",
    "setup_events",
    "historical_similarity_results",
    "historical_scan_status",
    "audit_log",
)

#: Reject DELETE and TRUNCATE, but allow UPDATE.
NO_DELETE_TABLES: tuple[str, ...] = (
    # A setup and its outcome are permanent record. UPDATE stays open so
    # a reviewer can assign review_confidence / false_positive_type after
    # the outcome was computed (Module 15).
    "setups",
    "setup_outcomes",
)


def _trigger_name(table: str, suffix: str) -> str:
    return f"{table}_{suffix}"


def create_guard_sql(table: str, *, allow_update: bool) -> list[str]:
    """DDL creating the guard triggers for one table."""
    row_ops = "DELETE" if allow_update else "UPDATE OR DELETE"
    suffix = "no_delete" if allow_update else "append_only"
    return [
        f"CREATE TRIGGER {_trigger_name(table, suffix)} "
        f"BEFORE {row_ops} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {MUTATION_GUARD_FUNCTION}();",
        f"CREATE TRIGGER {_trigger_name(table, 'no_truncate')} "
        f"BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {MUTATION_GUARD_FUNCTION}();",
    ]


def drop_guard_sql(table: str, *, allow_update: bool) -> list[str]:
    """DDL removing the guard triggers for one table."""
    suffix = "no_delete" if allow_update else "append_only"
    return [
        f"DROP TRIGGER IF EXISTS {_trigger_name(table, suffix)} ON {table};",
        f"DROP TRIGGER IF EXISTS {_trigger_name(table, 'no_truncate')} ON {table};",
    ]


def all_create_guard_sql() -> list[str]:
    """Every statement needed to install the guards, function first."""
    statements = [CREATE_GUARD_FUNCTION_SQL]
    for table in APPEND_ONLY_TABLES:
        statements.extend(create_guard_sql(table, allow_update=False))
    for table in NO_DELETE_TABLES:
        statements.extend(create_guard_sql(table, allow_update=True))
    return statements


def all_drop_guard_sql() -> list[str]:
    """Every statement needed to remove the guards, function last."""
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        statements.extend(drop_guard_sql(table, allow_update=False))
    for table in NO_DELETE_TABLES:
        statements.extend(drop_guard_sql(table, allow_update=True))
    statements.append(DROP_GUARD_FUNCTION_SQL)
    return statements
