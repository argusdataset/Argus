"""Translating the Ultimate-plan records: field tolerance and PIT.

Two things are being checked, and they fail in opposite directions.

**Field tolerance.** FMP's exact JSON key names for these endpoints were
never verified against a live key, so `FIELD_ALIASES` tries several
spellings per concept. The tests below feed each translator a payload
using a *different* alias than the first one and assert both that it
resolved and that `lineage.resolved_fields` records which spelling
worked. A mismatch has to surface as a visibly unresolved field, never as
a silently wrong row — the rule
`core/candidate_detection/eligibility/bankruptcy.py` established.

**Leakage.** Every translator answers "when could ARGUS first have known
this?", and the wrong answer is invisible in production and fatal in a
backtest. Two cases here are non-obvious in opposite directions:

- Executive compensation could be read *early*: a fiscal year ends in
  December and the proxy lands in spring, so sourcing the observation
  from the year end would make pay figures readable months before they
  existed.
- Analyst estimates could be read *late*: on that endpoint `date` is the
  period being forecast, so resolving the observation from it would mark
  a 2027 forecast knowable only in 2027 and hide every forward estimate
  the endpoint exists to serve.

Both are asserted below, because the first is the mistake everyone
expects and the second is the one that was actually made and caught.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from data.normalization.terminal_records import (
    TerminalTranslationError,
    translate_analyst_estimate,
    translate_executive_compensation,
    translate_fund_holdings,
    translate_grade,
    translate_peers,
    translate_price_target,
    translate_technical_indicator,
    translate_transcript,
)
from data.provider_adapters.fmp.models import (
    AnalystEstimate,
    AnalystGrade,
    EarningsTranscript,
    ExecutiveCompensation,
    FetchProvenance,
    FundHolding,
    PriceTarget,
    SecurityPeerGroup,
    TechnicalIndicatorPoint,
)

FETCHED_AT = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)
SECURITY_ID = uuid4()


def _provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=FETCHED_AT)


def _estimate(**raw: Any) -> AnalystEstimate:
    return AnalystEstimate(symbol="TEST", provenance=_provenance("analyst_estimates"), raw=raw)


# --------------------------------------------------------------------------
# Analyst estimates
# --------------------------------------------------------------------------


def test_a_forward_estimate_is_knowable_when_it_was_published() -> None:
    """The bug this endpoint invites, asserted directly.

    `date` here is the *period being forecast*. A 2027 estimate published
    today must become available today — not in 2027 — or the Terminal's
    forward panel is empty for a year at a time and nothing says why.
    """
    stored = translate_analyst_estimate(_estimate(date="2027-12-31"), SECURITY_ID)

    assert stored.fiscal_period == "2027-12-31"
    # Knowable at the fetch instant, not at the end of the period it
    # forecasts — which is nearly two years later.
    assert stored.pit.observation_time == FETCHED_AT
    assert stored.pit.availability_time < datetime(2026, 3, 5, tzinfo=UTC)
    assert stored.lineage["observation_source"] == "fetch_time"


def test_an_estimates_publication_date_is_preferred_over_the_period() -> None:
    """When the provider does say when it published, that wins.

    The fallback to fetch time is a concession to a payload that carries
    no publication timestamp, not the intended behaviour.
    """
    published = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    stored = translate_analyst_estimate(
        _estimate(date="2027-12-31", publishedDate="2026-02-01T12:00:00"), SECURITY_ID
    )

    assert stored.pit.observation_time == published
    assert stored.lineage["resolved_fields"]["published_at"] == "publishedDate"
    assert stored.lineage["observation_source"] == "provider"


def test_an_estimate_with_no_period_is_refused() -> None:
    """Refused rather than defaulted.

    Every plausible default files the row under a period it does not
    describe, which is worse than a visible failure: the row would look
    correct forever.
    """
    with pytest.raises(TerminalTranslationError, match="fiscal period"):
        translate_analyst_estimate(_estimate(estimatedEpsAvg=1.0), SECURITY_ID)


# --------------------------------------------------------------------------
# Executive compensation
# --------------------------------------------------------------------------


def test_compensation_is_knowable_when_filed_not_when_the_year_ended() -> None:
    """The leak, asserted from the translation side.

    A 2025 fiscal year with an April 2026 filing must not be knowable in
    January 2026. `event_time` still anchors to the year end because that
    is what the figures describe; only `observation_time` moves.
    """
    record = ExecutiveCompensation(
        symbol="TEST",
        provenance=_provenance("executive_compensation"),
        raw={"year": 2025, "filingDate": "2026-04-15", "nameAndPosition": "Jane Roe, CEO"},
    )

    stored = translate_executive_compensation(record, SECURITY_ID)

    assert stored.pit.event_time.year == 2025
    assert stored.pit.observation_time.date().isoformat() == "2026-04-15"
    assert stored.pit.availability_time > stored.pit.observation_time
    assert stored.lineage["observation_source"] == "filing"


def test_two_officers_in_one_year_get_distinct_period_labels() -> None:
    """Otherwise the second collides with the first and disappears.

    The row key is (security, type, fiscal_period, observation_time), and
    both officers share a year and a filing. Only the label keeps them
    apart, and `ON CONFLICT DO NOTHING` would drop the loser silently.
    """
    provenance = _provenance("executive_compensation")
    officers = [
        ExecutiveCompensation(
            symbol="TEST",
            provenance=provenance,
            raw={"year": 2025, "filingDate": "2026-04-15", "nameAndPosition": name},
        )
        for name in ("Jane Roe, CEO", "John Doe, CFO")
    ]

    labels = {
        translate_executive_compensation(record, SECURITY_ID).fiscal_period for record in officers
    }

    assert len(labels) == 2


def test_compensation_falls_back_to_fetch_time_without_a_filing_date() -> None:
    """Never to the year end, which is always present and always wrong."""
    record = ExecutiveCompensation(
        symbol="TEST",
        provenance=_provenance("executive_compensation"),
        raw={"fiscalYear": "2025", "nameAndPosition": "Jane Roe, CEO"},
    )

    stored = translate_executive_compensation(record, SECURITY_ID)

    assert stored.pit.observation_time == FETCHED_AT
    assert stored.lineage["observation_source"] == "fetch_time"
    assert stored.lineage["resolved_fields"]["compensation_year"] == "fiscalYear"


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------


def test_a_transcript_is_labelled_by_year_and_quarter() -> None:
    record = EarningsTranscript(
        symbol="TEST",
        provenance=_provenance("earnings_transcript"),
        year=2026,
        quarter=1,
        raw={"date": "2026-02-04 17:00:00", "content": "Operator: good afternoon."},
    )

    stored = translate_transcript(record, SECURITY_ID)

    assert stored.disclosure_type is CanonicalDisclosureType.EARNINGS_TRANSCRIPT
    assert stored.fiscal_period == "2026-Q1"
    assert stored.pit.event_time == datetime(2026, 2, 4, 17, 0, tzinfo=UTC)
    assert stored.data["content"] == "Operator: good afternoon."


# --------------------------------------------------------------------------
# Price targets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("consensus", CanonicalSnapshotType.PRICE_TARGET_CONSENSUS),
        ("summary", CanonicalSnapshotType.PRICE_TARGET_SUMMARY),
    ],
)
def test_the_two_price_target_endpoints_become_two_snapshot_types(
    source: str, expected: CanonicalSnapshotType
) -> None:
    """One type would let one half overwrite the other.

    Both halves are fetched in the same run, and the snapshot key is
    (security, type, observation_time). Under a shared type, two payloads
    resolving to the same instant collide and the second is dropped —
    leaving a response whose contents depend on request ordering.
    """
    target = PriceTarget(
        symbol="TEST", source=source, provenance=_provenance("price_target"), raw={"x": 1}
    )

    stored = translate_price_target(target, SECURITY_ID)

    assert stored.snapshot_type is expected
    # `source` stays in the payload too, so a row read on its own says
    # what it is without consulting the column it was filed under.
    assert stored.data["source"] == source


# --------------------------------------------------------------------------
# Peers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"peers": ["AAA", "BBB"]}, ["AAA", "BBB"]),
        ({"peersList": "AAA,BBB"}, ["AAA", "BBB"]),
        ({"symbols": ["AAA"]}, ["AAA"]),
        ({}, []),
    ],
)
def test_peer_shapes_all_reduce_to_a_symbol_list(raw: dict[str, Any], expected: list[str]) -> None:
    """Three documented shapes, one stored shape.

    Which one FMP actually returns is unverified, so all three resolve.
    The empty case is a snapshot that names nobody — a real answer,
    distinct from no snapshot at all, which `related.py` keeps apart.
    """
    group = SecurityPeerGroup(symbol="TEST", provenance=_provenance("stock_peers"), raw=raw)

    stored = translate_peers(group, SECURITY_ID)

    assert stored.data["peers"] == expected


# --------------------------------------------------------------------------
# Fund holdings
# --------------------------------------------------------------------------


def test_a_fund_disclosure_is_one_row_not_one_per_position() -> None:
    """A disclosure is a statement about the portfolio as it stood.

    A row per holding would multiply an index ETF into five hundred rows
    per fetch and make "what did this fund hold in March" a
    reconstruction rather than a lookup.
    """
    provenance = _provenance("etf_holdings")
    holdings = [
        FundHolding(symbol="ETF", source="etf_holdings", provenance=provenance, raw={"asset": a})
        for a in ("AAA", "BBB", "CCC")
    ]

    stored = translate_fund_holdings(holdings, SECURITY_ID)

    assert stored.snapshot_type is CanonicalSnapshotType.FUND_HOLDINGS
    assert stored.data["position_count"] == 3
    assert [item["asset"] for item in stored.data["holdings"]] == ["AAA", "BBB", "CCC"]


def test_an_empty_holdings_list_is_refused() -> None:
    """Nothing to snapshot. A zero-position row would assert a fact the
    fetch never established."""
    with pytest.raises(TerminalTranslationError):
        translate_fund_holdings([], SECURITY_ID)


# --------------------------------------------------------------------------
# Grades
# --------------------------------------------------------------------------


def test_a_grade_keeps_the_firms_own_wording() -> None:
    """ARGUS does not decide what a house meant by "Market Perform".

    Normalising these into a scale would be interpretation, which is
    precisely what the Terminal does not do.
    """
    grade = AnalystGrade(
        symbol="TEST",
        provenance=_provenance("analyst_grades"),
        raw={
            "gradingCompany": "Alpha Bank",
            "date": "2026-03-02",
            "action": "upgrade",
            "previousGrade": "Market Perform",
            "newGrade": "Outperform",
        },
    )

    stored = translate_grade(grade, SECURITY_ID)

    assert stored.grading_company == "Alpha Bank"
    assert stored.previous_grade == "Market Perform"
    assert stored.new_grade == "Outperform"
    assert stored.lineage["resolved_fields"]["grading_company"] == "gradingCompany"


def test_a_grade_resolves_an_alternative_spelling_and_records_it() -> None:
    """The tolerance pattern, and the audit trail that makes it safe.

    Trying several spellings is only defensible if the row says which one
    worked — otherwise a provider rename becomes an invisible change in
    what the column means.
    """
    grade = AnalystGrade(
        symbol="TEST",
        provenance=_provenance("analyst_grades"),
        raw={"analystCompany": "Beta Securities", "date": "2026-03-02", "gradeTo": "Buy"},
    )

    stored = translate_grade(grade, SECURITY_ID)

    assert stored.grading_company == "Beta Securities"
    assert stored.new_grade == "Buy"
    assert stored.lineage["resolved_fields"]["grading_company"] == "analystCompany"
    assert stored.lineage["resolved_fields"]["new_grade"] == "gradeTo"


def test_a_grade_with_no_firm_is_refused() -> None:
    """The firm is half the row's key. Without it two actions merge."""
    grade = AnalystGrade(
        symbol="TEST", provenance=_provenance("analyst_grades"), raw={"date": "2026-03-02"}
    )

    with pytest.raises(TerminalTranslationError, match="grading company"):
        translate_grade(grade, SECURITY_ID)


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------


def _point(indicator: str, **raw: Any) -> TechnicalIndicatorPoint:
    return TechnicalIndicatorPoint(
        symbol="TEST",
        indicator=indicator,
        period_length=14,
        timeframe="1day",
        provenance=_provenance("technical_indicator"),
        raw=raw,
    )


@pytest.mark.parametrize(
    ("indicator", "raw", "expected"),
    [
        ("rsi", {"date": "2026-03-02", "rsi": "62.5"}, Decimal("62.5")),
        ("adx", {"date": "2026-03-02", "adx": 21.0}, Decimal("21.0")),
        (
            "standarddeviation",
            {"date": "2026-03-02", "standardDeviation": "3.25"},
            Decimal("3.25"),
        ),
        ("williams", {"date": "2026-03-02", "williamsR": "-80"}, Decimal("-80")),
        ("sma", {"date": "2026-03-02", "value": "101.25"}, Decimal("101.25")),
    ],
)
def test_each_indicator_value_is_found_under_its_own_key(
    indicator: str, raw: dict[str, Any], expected: Decimal
) -> None:
    """Which key holds the number differs per indicator.

    `standarddeviation` and `williams` are the awkward ones: the path
    segment and the JSON key are spelled differently, which is exactly
    the kind of thing that would otherwise store a null beside a perfectly
    good number.
    """
    stored = translate_technical_indicator(_point(indicator, **raw), SECURITY_ID)

    assert stored.value == expected


def test_an_unresolvable_indicator_value_is_null_beside_a_kept_payload() -> None:
    """`None` is never `0.0`, and the fetch is not lost.

    A value under a key nobody tried leaves a null column and a complete
    `data` payload — recoverable by a later reader, unlike a zero.
    """
    stored = translate_technical_indicator(
        _point("rsi", date="2026-03-02", someUnexpectedKey="62.5"), SECURITY_ID
    )

    assert stored.value is None
    assert stored.data["someUnexpectedKey"] == "62.5"
    assert stored.lineage["resolved_fields"]["value"] is None


def test_an_indicator_point_is_available_when_its_bar_is() -> None:
    """The bar lag, reused rather than reinvented.

    An indicator computed from a bar cannot be knowable before the bar
    is. Reusing `ProviderLagPolicy.daily_bar` means that if the bar lag
    is ever wrong, it is wrong in one place.
    """
    from data.canonical_model.pit import DEFAULT_LAG_POLICY

    stored = translate_technical_indicator(
        _point("rsi", date="2026-03-02", rsi="62.5"), SECURITY_ID
    )

    assert (
        stored.pit.availability_time - stored.pit.observation_time == DEFAULT_LAG_POLICY.daily_bar
    )


def test_an_indicator_point_with_no_date_is_refused() -> None:
    with pytest.raises(TerminalTranslationError, match="no date"):
        translate_technical_indicator(_point("rsi", rsi="62.5"), SECURITY_ID)
