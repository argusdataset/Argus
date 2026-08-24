"""The one endpoint that touches scan output, and the two rules it inherits.

Module 18's report made both binding for any API layer built on top of it:

1. A date is not a cutoff. Rows carry `as_of` (session close plus an
   offset); a caller sends a calendar date. Resolve through
   `live_scan_runs`, never by reconstructing the offset.
2. `available=False` and "available but empty" are different real
   answers, and must not be flattened.

Both are asserted here rather than trusted, because both are the kind of
thing a later refactor would break silently.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from core.live_scanner.runs import finish_run, start_run
from core.live_scanner.schedule import as_of_for
from infra.db.enums import LiveScanStatus

SCAN_DATE = date(2026, 3, 3)


@pytest.fixture
def lineage(connection, register):
    """A published six-part lineage, so a scan run can be recorded."""
    from uuid import uuid4

    from core.candidate_detection.config import DetectionConfig, publish_detection_configuration
    from core.feature_engine.spec import FeatureSpec, publish_feature_schema_version
    from core.market_state.thresholds import MarketStateConfig, publish_target_model_version
    from core.outcome_tracking.config import OutcomeConfig, publish_outcome_snapshot
    from core.scoring.config import ScoringConfig, publish_scoring_configuration
    from core.scoring.engine import Lineage
    from infra.db.schema.identity import universe_version

    universe_id = connection.execute(
        universe_version.insert()
        .values(
            version_label=f"module19-{uuid4()}",
            as_of_date=datetime(2026, 3, 3, tzinfo=UTC),
            definition={"source": "module19 test"},
        )
        .returning(universe_version.c.id)
    ).scalar_one()

    return Lineage(
        target_model_version_id=publish_target_model_version(connection, MarketStateConfig()),
        feature_schema_version_id=publish_feature_schema_version(connection, FeatureSpec()),
        scoring_configuration_id=publish_scoring_configuration(connection, ScoringConfig()),
        detection_configuration_id=publish_detection_configuration(connection, DetectionConfig()),
        universe_version_id=universe_id,
        data_snapshot_id=publish_outcome_snapshot(
            connection, OutcomeConfig(), as_of=datetime(2026, 3, 3, tzinfo=UTC)
        ),
    )


def _record_scan(connection, lineage, *, status: LiveScanStatus, scan_date: date = SCAN_DATE):
    run = start_run(
        connection,
        scan_date=scan_date,
        as_of=as_of_for(scan_date),
        lineage=lineage,
    )
    return finish_run(connection, run.id, status=status, detail={}, note="module19 test")


def test_a_date_nobody_scanned_reports_unavailable(client):
    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert payload["available"] is False
    assert payload["scored_signals"] == 0
    assert "nobody has looked" in payload["explanation"]


def test_a_scanned_date_that_found_nothing_reports_available(client, connection, lineage):
    """The distinction that matters most right now: this is the *normal*
    outcome today, because scoring needs a historical case dataset that
    Module 17's full scan has not produced. Reporting it as unavailable
    would show a working system as a broken one every single day."""
    _record_scan(connection, lineage, status=LiveScanStatus.COMPLETED)

    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert payload["available"] is True
    assert payload["status"] == "COMPLETED"
    assert payload["scored_signals"] == 0
    assert "expected outcome" in payload["explanation"]


def test_the_two_empties_are_distinguishable_in_the_response(client, connection, lineage):
    never = client.get("/terminal/scan-status/2026-03-02").json()
    _record_scan(connection, lineage, status=LiveScanStatus.COMPLETED)
    scanned = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert never["available"] != scanned["available"]
    assert never["explanation"] != scanned["explanation"]
    # Both have zero counts — so a consumer keying on counts alone would
    # see these as the same thing, which is exactly why `available` is a
    # separate field rather than an inference.
    assert never["scored_signals"] == scanned["scored_signals"] == 0


def test_a_failed_scan_is_not_reported_as_available(client, connection, lineage):
    """Only COMPLETED and COMPLETED_WITH_EXCLUSIONS count as a scan that
    happened — Module 18's `TERMINAL_SUCCESS`."""
    _record_scan(connection, lineage, status=LiveScanStatus.FAILED)

    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert payload["available"] is False
    assert payload["status"] == "FAILED"


def test_a_scan_with_exclusions_is_available_and_says_how_many(client, connection, lineage):
    run = start_run(connection, scan_date=SCAN_DATE, as_of=as_of_for(SCAN_DATE), lineage=lineage)
    finish_run(
        connection,
        run.id,
        status=LiveScanStatus.COMPLETED_WITH_EXCLUSIONS,
        detail={},
        excluded=[{"security_id": "x", "reason": "bad bars", "error_type": "ValueError"}],
    )

    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert payload["available"] is True
    assert payload["excluded_count"] == 1


def test_the_date_is_resolved_through_live_scan_runs_not_by_reconstructing_a_cutoff(
    client, connection, lineage
):
    """Module 18's binding rule, asserted by construction.

    The scan is recorded with the offset `as_of_for` produces. Then the
    row's `as_of` is moved to something the Terminal could not possibly
    derive from the date. If any part of this module reconstructed the
    cutoff instead of reading it, the lookup would now miss — and it
    does not, because it goes through `scan_results`.
    """
    _record_scan(connection, lineage, status=LiveScanStatus.COMPLETED)
    invented = as_of_for(SCAN_DATE) + timedelta(hours=7, minutes=13)
    connection.execute(
        text("UPDATE live_scan_runs SET as_of = :as_of WHERE scan_date = :d"),
        {"as_of": invented, "d": SCAN_DATE},
    )

    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert payload["available"] is True
    assert payload["status"] == "COMPLETED"


def test_an_unparseable_date_is_rejected_by_the_framework(client):
    response = client.get("/terminal/scan-status/not-a-date")

    assert response.status_code == 422


def test_the_endpoint_exposes_no_per_security_intelligence(client, connection, lineage):
    """Freshness metadata, not Intelligence. The three auto-generated
    watchlists and every per-security signal belong to Module 21."""
    _record_scan(connection, lineage, status=LiveScanStatus.COMPLETED)

    payload = client.get(f"/terminal/scan-status/{SCAN_DATE.isoformat()}").json()

    assert set(payload) == {
        "scan_date",
        "available",
        "status",
        "scored_signals",
        "setups_opened",
        "excluded_count",
        "explanation",
    }
