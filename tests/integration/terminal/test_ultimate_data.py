"""The eight Ultimate-plan data types, found and not found.

Two tests per type, and the second one matters more than the first. A
read that returns a row when a row exists is easy to get right by
accident. A read that returns the *right absence* — `NEVER_INGESTED`
when ARGUS holds nothing, `NOT_YET_AVAILABLE` when it holds something the
cutoff hides — is the property that stops the Terminal reporting a
working system as a broken one, and nothing else in the codebase checks
it for these tables.

Beyond the pairs, three tests cover the things that are only wrong under
a specific arrangement of rows and would otherwise pass by luck:

- `test_estimate_revision_...` — the restatement rule. One period, two
  observations, and the query has to pick by observation time among the
  available rows rather than by availability alone.
- `test_price_target_halves_...` — the two endpoints are two snapshot
  types, so a run that fetches both keeps both.
- `test_rsi_and_sma_do_not_bleed_...` — the series key. Two indicators at
  two periods, and neither must appear in the other's response.

`NOW` is the conftest's fixed instant, so a cutoff assertion means the
same thing on every run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from services.terminal import analyst, governance, indicators, related
from services.terminal.errors import TerminalError
from tests.integration.terminal.conftest import NOW

YESTERDAY = NOW - timedelta(days=1)
LAST_WEEK = NOW - timedelta(days=7)
#: Filed after the cutoff. Anything stamped with this must be invisible.
TOMORROW = NOW + timedelta(days=1)


# --------------------------------------------------------------------------
# 1. Analyst estimates
# --------------------------------------------------------------------------


def test_analyst_estimates_are_returned_newest_period_first(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    security_id = register("ESTA")
    for period in ("2026", "2027"):
        add_disclosure(
            security_id,
            disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
            fiscal_period=period,
            available_at=LAST_WEEK,
            data={"estimatedEpsAvg": 4.2, "period": period},
        )

    response = analyst.read_analyst_estimates(connection, "ESTA", as_of=NOW)

    assert [period.fiscal_period for period in response.periods] == ["2027", "2026"]
    assert response.periods[0].data["estimatedEpsAvg"] == 4.2
    assert response.unavailable is None


def test_analyst_estimates_absent_are_named_not_empty(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    """The whole point of the `Unavailable` block.

    A security ARGUS has never held estimates for gets a reason, not an
    empty list — a consumer rendering "no coverage" needs to know it is
    absence rather than a provider that returned nothing.
    """
    register("ESTB")

    response = analyst.read_analyst_estimates(connection, "ESTB", as_of=NOW)

    assert response.periods == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value
    assert response.unavailable.available is False


def test_estimate_published_after_the_cutoff_is_not_yet_available(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    """Held, but not knowable — the other kind of empty.

    The reason has to differ from the case above. Both look like an empty
    list to a consumer and mean opposite things: one says "ask a
    different provider", the other says "ask again tomorrow".
    """
    security_id = register("ESTC")
    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
        fiscal_period="2027",
        available_at=TOMORROW,
        data={"estimatedEpsAvg": 9.9},
    )

    response = analyst.read_analyst_estimates(connection, "ESTC", as_of=NOW)

    assert response.periods == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NOT_YET_AVAILABLE.value


def test_estimate_revision_shows_the_view_current_at_the_cutoff(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    """The restatement rule, which ordering on availability alone breaks.

    One fiscal period, two observations of it. A query before the
    revision must see the original — not because the revision is later,
    but because it was not knowable yet — and a query after must see the
    revision and *only* the revision. A reader that returned both would
    show one period twice with different numbers.
    """
    security_id = register("ESTD")
    original = NOW - timedelta(days=30)
    revised = NOW - timedelta(days=2)

    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
        fiscal_period="2027",
        observed_at=original,
        available_at=original,
        data={"estimatedEpsAvg": 3.0},
    )
    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.ANALYST_ESTIMATES,
        fiscal_period="2027",
        observed_at=revised,
        available_at=revised,
        data={"estimatedEpsAvg": 5.0},
    )

    before = analyst.read_analyst_estimates(connection, "ESTD", as_of=NOW - timedelta(days=10))
    after = analyst.read_analyst_estimates(connection, "ESTD", as_of=NOW)

    assert [period.data["estimatedEpsAvg"] for period in before.periods] == [3.0]
    assert [period.data["estimatedEpsAvg"] for period in after.periods] == [5.0]


# --------------------------------------------------------------------------
# 2. Price targets
# --------------------------------------------------------------------------


def test_price_target_consensus_is_returned(
    connection: Connection,
    register: Callable[..., UUID],
    add_snapshot: Callable[..., None],
) -> None:
    security_id = register("PTA")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.PRICE_TARGET_CONSENSUS,
        available_at=LAST_WEEK,
        data={"source": "consensus", "targetConsensus": 210.0},
    )

    response = analyst.read_price_target(connection, "PTA", as_of=NOW)

    assert response.consensus is not None
    assert response.consensus["targetConsensus"] == 210.0
    assert response.observed_at == LAST_WEEK
    # Only one half on file, so the response says it is partial rather
    # than showing a target with no idea who stands behind it.
    assert response.summary is None
    assert response.unavailable is not None


def test_price_target_absent_is_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("PTB")

    response = analyst.read_price_target(connection, "PTB", as_of=NOW)

    assert response.consensus is None
    assert response.summary is None
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


def test_price_target_halves_do_not_overwrite_each_other(
    connection: Connection,
    register: Callable[..., UUID],
    add_snapshot: Callable[..., None],
) -> None:
    """Both endpoints, one instant, both rows kept.

    This is why the two are separate snapshot types. Under a single
    `PRICE_TARGET` type these two writes share the snapshot key
    (security, type, observation_time) and `ON CONFLICT DO NOTHING` drops
    the second — leaving a response whose contents depend on which
    request the ingestion happened to make first.
    """
    security_id = register("PTC")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.PRICE_TARGET_CONSENSUS,
        available_at=LAST_WEEK,
        data={"source": "consensus", "targetConsensus": 210.0},
    )
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.PRICE_TARGET_SUMMARY,
        available_at=LAST_WEEK,
        data={"source": "summary", "lastMonthCount": 7},
    )

    response = analyst.read_price_target(connection, "PTC", as_of=NOW)

    assert response.consensus is not None
    assert response.consensus["targetConsensus"] == 210.0
    assert response.summary is not None
    assert response.summary["lastMonthCount"] == 7
    assert response.unavailable is None


# --------------------------------------------------------------------------
# 3. Analyst grades
# --------------------------------------------------------------------------


def test_grades_return_every_action_newest_first(
    connection: Connection,
    register: Callable[..., UUID],
    add_grade: Callable[..., None],
) -> None:
    """An event stream, not a current rating.

    Two firms acting on different days are two facts, and neither
    supersedes the other. A reader that collapsed these to "the current
    grade" would answer a question nobody asked.
    """
    security_id = register("GRDA")
    add_grade(security_id, grading_company="Alpha Bank", graded_at=LAST_WEEK)
    add_grade(
        security_id,
        grading_company="Beta Securities",
        graded_at=YESTERDAY,
        action="downgrade",
        previous_grade="Buy",
        new_grade="Hold",
    )

    response = analyst.read_analyst_grades(connection, "GRDA", as_of=NOW)

    assert [grade.grading_company for grade in response.grades] == [
        "Beta Securities",
        "Alpha Bank",
    ]
    assert response.grades[0].action == "downgrade"
    assert response.grades[0].new_grade == "Hold"
    assert response.unavailable is None


def test_grades_absent_are_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("GRDB")

    response = analyst.read_analyst_grades(connection, "GRDB", as_of=NOW)

    assert response.grades == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


# --------------------------------------------------------------------------
# 4. Executive compensation
# --------------------------------------------------------------------------


def test_executive_compensation_is_returned(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    """Two officers in one year are two rows.

    `translate_executive_compensation` writes the person into the period
    label for exactly this reason; a period key of the bare year would
    make the second officer collide with the first and vanish.
    """
    security_id = register("EXCA")
    for officer, pay in (("2025:Jane Roe, CEO", 9_000_000), ("2025:John Doe, CFO", 4_000_000)):
        add_disclosure(
            security_id,
            disclosure_type=CanonicalDisclosureType.EXECUTIVE_COMPENSATION,
            fiscal_period=officer,
            available_at=LAST_WEEK,
            data={"nameAndPosition": officer.split(":")[1], "total": pay},
        )

    response = governance.read_executive_compensation(connection, "EXCA", as_of=NOW)

    assert len(response.disclosures) == 2
    assert {record.data["total"] for record in response.disclosures} == {9_000_000, 4_000_000}
    assert response.unavailable is None


def test_compensation_filed_after_the_cutoff_is_invisible(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    """The leak this type is prone to, checked from the read side.

    A fiscal year ends in December and the proxy is filed in spring. A
    query between the two must see nothing — availability is the filing,
    never the year end.
    """
    security_id = register("EXCB")
    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.EXECUTIVE_COMPENSATION,
        fiscal_period="2025:Jane Roe, CEO",
        fiscal_period_end=NOW - timedelta(days=90),
        available_at=TOMORROW,
        data={"total": 9_000_000},
    )

    response = governance.read_executive_compensation(connection, "EXCB", as_of=NOW)

    assert response.disclosures == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NOT_YET_AVAILABLE.value


# --------------------------------------------------------------------------
# 5. Earnings transcripts
# --------------------------------------------------------------------------


def test_transcripts_are_returned_with_their_text(
    connection: Connection,
    register: Callable[..., UUID],
    add_disclosure: Callable[..., None],
) -> None:
    """Text passed through, never summarised.

    The assertion is on the raw content: if anything ever starts
    condensing this, the equality fails rather than the meaning quietly
    changing.
    """
    security_id = register("TRNA")
    add_disclosure(
        security_id,
        disclosure_type=CanonicalDisclosureType.EARNINGS_TRANSCRIPT,
        fiscal_period="2026-Q1",
        fiscal_period_end=LAST_WEEK,
        available_at=LAST_WEEK,
        data={"content": "Operator: good afternoon.", "quarter": 1, "year": 2026},
    )

    response = governance.read_transcripts(connection, "TRNA", as_of=NOW)

    assert len(response.transcripts) == 1
    assert response.transcripts[0].fiscal_period == "2026-Q1"
    assert response.transcripts[0].data["content"] == "Operator: good afternoon."
    assert response.transcripts[0].held_at == LAST_WEEK
    assert response.unavailable is None


def test_transcripts_absent_are_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("TRNB")

    response = governance.read_transcripts(connection, "TRNB", as_of=NOW)

    assert response.transcripts == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


# --------------------------------------------------------------------------
# 6. Peers
# --------------------------------------------------------------------------


def test_peers_are_returned(
    connection: Connection,
    register: Callable[..., UUID],
    add_snapshot: Callable[..., None],
) -> None:
    security_id = register("PRA")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.PEERS,
        available_at=LAST_WEEK,
        data={"peers": ["AAA", "BBB", "CCC"]},
    )

    response = related.read_peers(connection, "PRA", as_of=NOW)

    assert response.peers == ["AAA", "BBB", "CCC"]
    assert response.observed_at == LAST_WEEK
    assert response.unavailable is None


def test_peers_absent_are_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("PRB")

    response = related.read_peers(connection, "PRB", as_of=NOW)

    assert response.peers == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


def test_a_peer_list_naming_nobody_is_not_an_absence(
    connection: Connection,
    register: Callable[..., UUID],
    add_snapshot: Callable[..., None],
) -> None:
    """The provider answered and named nobody.

    Different from having no snapshot at all, and the response has to
    keep them apart: `unavailable` stays `None` because ARGUS did hear
    from the provider — it heard that this security has no peers.
    """
    security_id = register("PRC")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.PEERS,
        available_at=LAST_WEEK,
        data={"peers": []},
    )

    response = related.read_peers(connection, "PRC", as_of=NOW)

    assert response.peers == []
    assert response.unavailable is None
    assert response.observed_at == LAST_WEEK


# --------------------------------------------------------------------------
# 7. Fund holdings (ETF holdings and fund disclosures, one snapshot type)
# --------------------------------------------------------------------------


def test_fund_holdings_are_returned_with_the_disclosed_count(
    connection: Connection,
    register: Callable[..., UUID],
    add_snapshot: Callable[..., None],
) -> None:
    """`position_count` is the fund's number, not this response's.

    Stored at write time from the whole disclosure. A count derived from
    the returned list would silently become a fact about pagination.
    """
    security_id = register("ETFA")
    add_snapshot(
        security_id,
        snapshot_type=CanonicalSnapshotType.FUND_HOLDINGS,
        available_at=LAST_WEEK,
        data={
            "source": "etf_holdings",
            "position_count": 503,
            "holdings": [{"asset": "AAA", "weightPercentage": 7.1}],
        },
    )

    response = related.read_fund_holdings(connection, "ETFA", as_of=NOW)

    assert response.position_count == 503
    assert len(response.holdings) == 1
    assert response.holdings[0]["asset"] == "AAA"
    assert response.source == "etf_holdings"
    assert response.unavailable is None


def test_fund_holdings_absent_are_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    """An operating company and an uningested fund look the same here.

    Documented rather than fixed: ARGUS stores no security type, so this
    is `NEVER_INGESTED` for both. See `services/terminal/related.py`.
    """
    register("OPCO")

    response = related.read_fund_holdings(connection, "OPCO", as_of=NOW)

    assert response.holdings == []
    assert response.position_count == 0
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


# --------------------------------------------------------------------------
# 8. Technical indicators
# --------------------------------------------------------------------------


def test_indicator_series_is_returned_newest_bar_first(
    connection: Connection,
    register: Callable[..., UUID],
    add_indicator: Callable[..., None],
) -> None:
    security_id = register("INDA")
    for offset, value in ((7, "51.5"), (1, "62.5")):
        add_indicator(
            security_id,
            indicator="rsi",
            period_length=14,
            bar_time=NOW - timedelta(days=offset),
            value=Decimal(value),
        )

    response = indicators.read_indicator(connection, "INDA", "rsi", as_of=NOW)

    assert [point.value for point in response.points] == [62.5, 51.5]
    assert response.indicator == "rsi"
    # The period was not asked for, so it came from what the ingestion
    # writes — a default that disagreed would return an empty series.
    assert response.period_length == 14
    assert response.timeframe == "1day"
    assert response.unavailable is None


def test_indicator_absent_is_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("INDB")

    response = indicators.read_indicator(connection, "INDB", "rsi", as_of=NOW)

    assert response.points == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


def test_rsi_and_sma_do_not_bleed_into_each_other(
    connection: Connection,
    register: Callable[..., UUID],
    add_indicator: Callable[..., None],
) -> None:
    """The series key, checked with both halves populated.

    Two indicators at two period lengths on the same bar. Each response
    must contain its own number and nothing else — a key that dropped
    `indicator` or `period_length` would draw one series under the
    other's label, which no test of a single series can catch.
    """
    security_id = register("INDC")
    add_indicator(
        security_id, indicator="rsi", period_length=14, bar_time=YESTERDAY, value=Decimal("62.5")
    )
    add_indicator(
        security_id, indicator="sma", period_length=50, bar_time=YESTERDAY, value=Decimal("101.25")
    )

    rsi = indicators.read_indicator(connection, "INDC", "rsi", as_of=NOW)
    sma = indicators.read_indicator(connection, "INDC", "sma", as_of=NOW)

    assert [point.value for point in rsi.points] == [62.5]
    assert [point.value for point in sma.points] == [101.25]


def test_an_uningested_period_reports_absence_not_an_error(
    connection: Connection,
    register: Callable[..., UUID],
    add_indicator: Callable[..., None],
) -> None:
    """A period nobody ingested is a request a later run could satisfy.

    So it is an empty series with a reason, unlike an unknown indicator
    name below, which nothing can ever satisfy.
    """
    security_id = register("INDD")
    add_indicator(
        security_id, indicator="rsi", period_length=14, bar_time=YESTERDAY, value=Decimal("62.5")
    )

    response = indicators.read_indicator(connection, "INDD", "rsi", period_length=21, as_of=NOW)

    assert response.points == []
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


def test_an_unknown_indicator_is_rejected(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    """ "No such indicator" is not "not ingested yet".

    An empty series would state the second when the first is true, and a
    client would keep asking forever.
    """
    register("INDE")

    with pytest.raises(TerminalError) as raised:
        indicators.read_indicator(connection, "INDE", "ichimoku", as_of=NOW)

    assert raised.value.code == "UNKNOWN_INDICATOR"
    assert raised.value.status == 400
    assert "rsi" in raised.value.detail["allowed"]


def test_an_indicator_value_the_provider_did_not_give_stays_null(
    connection: Connection,
    register: Callable[..., UUID],
    add_indicator: Callable[..., None],
) -> None:
    """`None` is never `0.0`. The rule every module has held.

    A value the translation could not resolve under any key it tried is
    a null beside a complete payload — an indicator reading of zero and
    an indicator ARGUS could not parse are different facts, and a chart
    plotting the second at zero would be a visible lie.
    """
    security_id = register("INDF")
    add_indicator(security_id, indicator="adx", period_length=14, bar_time=YESTERDAY, value=None)

    response = indicators.read_indicator(connection, "INDF", "adx", as_of=NOW)

    assert len(response.points) == 1
    assert response.points[0].value is None


# --------------------------------------------------------------------------
# 9. Institutional ownership — the provider's figure, from Module 26's table
# --------------------------------------------------------------------------


def test_institutional_ownership_is_the_providers_own_percentage(
    connection: Connection,
    register: Callable[..., UUID],
    add_ownership_summary: Callable[..., None],
) -> None:
    """Read, not derived.

    ARGUS holds share counts and could divide them by a float it holds
    separately. It does not: the percentage shown is the one FMP's 13F
    summary reported, resolved through Module 26's own `FIELD_ALIASES`
    so the spelling that stored it is the spelling that reads it.
    """
    security_id = register("OWNA")
    add_ownership_summary(
        security_id,
        year=2025,
        quarter=4,
        available_at=LAST_WEEK,
        data={
            "ownershipPercent": 61.4,
            "investorsHolding": 6_312,
            "numberOf13Fshares": 9_400_000_000,
        },
    )

    response = related.read_institutional_ownership(connection, "OWNA", as_of=NOW)

    assert response.institutional_ownership_percent == 61.4
    assert response.investors_holding == 6_312
    assert response.fiscal_period == "2025-Q4"
    # The untouched summary sits beside the resolved fields.
    assert response.data["ownershipPercent"] == 61.4
    assert response.unavailable is None


def test_ownership_absent_is_named(
    connection: Connection,
    register: Callable[..., UUID],
) -> None:
    register("OWNB")

    response = related.read_institutional_ownership(connection, "OWNB", as_of=NOW)

    assert response.institutional_ownership_percent is None
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NEVER_INGESTED.value


def test_a_quarter_inside_the_filing_window_is_not_visible(
    connection: Connection,
    register: Callable[..., UUID],
    add_ownership_summary: Callable[..., None],
) -> None:
    """13F filers have 45 days, and the cutoff honours them.

    A quarter whose deadline has not passed is held but not knowable —
    `NOT_YET_AVAILABLE`, not `NEVER_INGESTED`. Confusing the two would
    tell a reader ARGUS has no coverage of a name it covers.
    """
    security_id = register("OWNC")
    add_ownership_summary(
        security_id,
        year=2026,
        quarter=1,
        available_at=TOMORROW,
        data={"ownershipPercent": 62.0},
    )

    response = related.read_institutional_ownership(connection, "OWNC", as_of=NOW)

    assert response.institutional_ownership_percent is None
    assert response.unavailable is not None
    assert response.unavailable.reason == MissReason.NOT_YET_AVAILABLE.value


def test_an_unreported_percentage_stays_none_not_zero(
    connection: Connection,
    register: Callable[..., UUID],
    add_ownership_summary: Callable[..., None],
) -> None:
    """A summary that names no percentage is not a company nobody owns.

    The rule every module holds: `None` is never `0.0`. Zero percent
    institutional ownership is a real and unusual fact about a security,
    and it must not be indistinguishable from a field FMP left out.
    """
    security_id = register("OWND")
    add_ownership_summary(
        security_id,
        year=2025,
        quarter=4,
        available_at=LAST_WEEK,
        data={"investorsHolding": 12},
    )

    response = related.read_institutional_ownership(connection, "OWND", as_of=NOW)

    assert response.institutional_ownership_percent is None
    assert response.investors_holding == 12
    assert response.unavailable is None
