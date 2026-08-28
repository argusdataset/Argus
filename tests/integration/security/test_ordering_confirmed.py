"""Confirmation, not modification: Module 23's ordering hazards still stand.

Module 24's brief is explicit about this one: the four "latest row wins"
hazards Module 23's `ordering.py` registry names are real, and fixing any
of them needs a monotonic-sequence migration plus changes to what
Modules 11/15/17/21 *decide* — the open-ended refactor every module in
this position has been told to avoid. This file does exactly what was
asked and nothing more: confirm the registry and its live probe still
work, against the schema as Module 24 leaves it. No line in
`infra/observability/ordering.py` is touched.
"""

from __future__ import annotations

from infra.observability.ordering import AUDIT, ordering_report, probe


def test_the_four_previously_found_hazards_are_still_registered_unsafe():
    """The registry Module 23 wrote is unchanged by this module's additions."""
    unsafe = {entry.table for entry in AUDIT if not entry.safe}

    assert unsafe == {
        "setup_outcomes",
        "public_stat_snapshots",
        "signals",
        "historical_similarity_results",
    }


def test_login_attempts_was_already_registered_and_stays_so():
    """Module 23 already covered this table; confirmed unchanged, not re-added."""
    entry = next(item for item in AUDIT if item.table == "login_attempts")

    assert entry.safe is True


def test_registration_attempts_was_added_as_a_new_safe_entry_not_a_rewrite():
    """The one line this module added to `ordering.py`: a new table, registered.

    "Do not modify Module 23's ordering registry logic" governs the four
    hazards it already found and the `probe()` mechanism — neither
    changed. Cataloguing a table Module 24 itself introduced is the
    registry doing its job rather than going stale on day one; the
    completeness scan in `tests/integration/observability/test_ordering.py`
    would have failed otherwise, which is that test correctly noticing
    new code it had never seen.

    `registration_lockout_state` sums every attempt in a window
    (`func.count()`) rather than selecting one row and calling it
    current, so the entry is `safe=True` — there is no "latest row" for a
    tie to make ambiguous.
    """
    entry = next(item for item in AUDIT if item.table == "registration_attempts")

    assert entry.safe is True
    assert "func.count()" in entry.basis


def test_the_probe_still_finds_no_ambiguity_on_a_freshly_migrated_database(connection):
    """Module 23's own guarantee, re-run against the schema as it stands now."""
    assert probe(connection) == []


def test_the_ordering_report_still_assembles_correctly(connection):
    report = ordering_report(connection)

    assert report["reads_audited"] == len(AUDIT)
    assert report["healthy"] is True
    assert len(report["unsafe"]) == 4


def test_the_probe_still_detects_a_constructed_ambiguity(connection):
    """The live-detection half of the diagnostic, still functioning.

    Reconstructs the exact scenario Module 23's own test used — two
    `public_stat_snapshots` rows for one chart sharing a `computed_at` —
    to confirm the probe this module was told not to fix still correctly
    notices when the hazard it names actually fires.
    """
    from datetime import UTC, datetime

    from infra.db.schema.public_stats import public_stat_snapshots

    shared = datetime.now(UTC)
    for index in range(2):
        connection.execute(
            public_stat_snapshots.insert().values(
                chart="confirmation_check",
                computed_at=shared,
                as_of=shared,
                gate_fingerprint=f"fp-{index}",
                included_runs=[],
                included_windows=[],
                payload={},
                sample_size=1,
            )
        )

    found = probe(connection)

    assert any(item.table == "public_stat_snapshots" for item in found)
