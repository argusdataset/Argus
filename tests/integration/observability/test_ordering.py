"""The "latest row wins" audit, and the probe that says whether it has bitten."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from infra.db.schema.public_stats import public_stat_snapshots
from infra.observability.ordering import AUDIT, ordering_report, probe
from tests.integration.observability.conftest import NOW


def test_the_audit_covers_every_ordered_read_in_the_codebase():
    """The registry is only worth something if it is complete.

    Scans the parse tree of every module for a descending order over a
    timestamp-shaped column and checks the table it belongs to appears in
    the audit. A registry that silently misses a table is worse than no
    registry, because the next person trusts it.
    """
    audited = {part.strip() for entry in AUDIT for part in entry.table.split("/")}
    interesting = {
        "recorded_at",
        "computed_at",
        "created_at",
        "assigned_at",
        "attempted_at",
        "issued_at",
        "entered_at",
        "valid_from",
        "event_time",
        "observation_time",
        "sequence_number",
        "attempt",
    }

    seen: set[str] = set()
    for path in [
        *Path("core").rglob("*.py"),
        *Path("services").rglob("*.py"),
        *Path("data").rglob("*.py"),
    ]:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in interesting:
                continue
            # `table.c.column` — the table name is two attributes up.
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "c"
                and isinstance(value.value, ast.Name)
            ):
                seen.add(value.value.id)

    assert seen, "the scan found no ordered reads at all, which cannot be right"
    missing = {name for name in seen if name not in audited}
    assert missing == set(), f"tables ordered on but not in the audit: {sorted(missing)}"


def test_the_audit_records_both_the_safe_and_the_unsafe():
    """Reporting only the problems loses the reasoning that cleared the rest."""
    assert len(AUDIT) >= 10
    assert any(entry.safe for entry in AUDIT)
    assert any(not entry.safe for entry in AUDIT)
    assert all(entry.basis for entry in AUDIT), "every verdict states its reasoning"


def test_the_three_known_fixes_are_recorded_as_safe():
    """Migration 0008's two instances, plus the table that never had the bug."""
    by_table = {entry.table: entry for entry in AUDIT}

    for table in ("historical_scan_status", "live_scan_runs", "setup_events"):
        assert by_table[table].safe is True
        assert by_table[table].ordering in ("sequence_number", "attempt")


def test_the_hazards_found_are_recorded_as_unsafe():
    """What the hour bought: four instances, none previously known.

    Two were found by reading. The other two — `signals` and
    `historical_similarity_results` — were found by the completeness scan
    above failing on a first draft of this registry, which is the
    strongest argument for having written the scan rather than only the
    list.
    """
    unsafe = {entry.table for entry in AUDIT if not entry.safe}

    assert unsafe == {
        "setup_outcomes",
        "public_stat_snapshots",
        "signals",
        "historical_similarity_results",
    }


def test_the_setup_outcomes_finding_names_all_three_parts_of_the_trap():
    """A finding that does not show the mechanism is a rumour."""
    entry = next(item for item in AUDIT if item.table == "setup_outcomes")

    assert "now()" in entry.basis
    assert "random" in entry.basis or "gen_random_uuid" in entry.basis
    assert "one transaction" in entry.basis
    assert "reproducibility" in entry.basis


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------


def test_a_clean_database_reports_no_ambiguity(connection):
    """The expected answer, and a different statement from "the code looks fine"."""
    assert probe(connection) == []

    report = ordering_report(connection)
    assert report["healthy"] is True
    assert report["observed_ambiguities"] == []


def test_the_probe_finds_an_ambiguity_that_has_actually_happened(connection):
    """Two snapshots of one chart sharing a `computed_at`, which decides nothing.

    Constructed rather than argued: the registry says the hazard exists,
    and this shows the probe would see it if a writer ever produced it.
    """
    shared = NOW - timedelta(minutes=5)
    for index in range(2):
        connection.execute(
            public_stat_snapshots.insert().values(
                chart="win_rate",
                computed_at=shared,
                as_of=NOW,
                gate_fingerprint=f"fingerprint-{index}",
                included_runs=[],
                included_windows=[],
                payload={"points": index},
                sample_size=10,
            )
        )

    found = probe(connection)

    assert len(found) == 1
    assert found[0].table == "public_stat_snapshots"
    assert found[0].parent == "win_rate"
    assert found[0].rows == 2

    assert ordering_report(connection)["healthy"] is False


def test_distinct_timestamps_are_not_flagged(connection):
    """The probe looks for ties, not for multiple rows."""
    for index in range(3):
        connection.execute(
            public_stat_snapshots.insert().values(
                chart="win_rate",
                computed_at=NOW - timedelta(minutes=index),
                as_of=NOW,
                gate_fingerprint="fingerprint",
                included_runs=[],
                included_windows=[],
                payload={},
                sample_size=1,
            )
        )

    assert probe(connection) == []
    assert (
        connection.execute(
            select(public_stat_snapshots.c.id).where(public_stat_snapshots.c.chart == "win_rate")
        ).all()
        != []
    ), "the rows really are there; they are simply unambiguous"


def test_the_report_reads_without_a_connection(connection):
    """The audit is a static fact; the probe is the part that needs a database."""
    report = ordering_report()

    assert report["reads_audited"] == len(AUDIT)
    assert "observed_ambiguities" not in report
    assert "healthy" not in report
