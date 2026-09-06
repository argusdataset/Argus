"""The 13F trend: quarter arithmetic, the comparison, and the 45-day rule.

Three things are checked here that nothing else can check. That this
signal produces **no verdict** — the one output in Modules 28 and 29 with
no `raised` anywhere, by design. That a *first* observation reports its
figures with `None` changes rather than zeroes. And that a quarter's
holdings are not treated as knowable until the filing deadline has
passed, which is the largest leak this module could have had.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from core.data_validation.result import MissReason
from core.ownership_signals.config import OwnershipThresholds
from core.ownership_signals.institutional import (
    FIELD_ALIASES,
    InstitutionalPeriod,
    InstitutionalTrend,
    OwnershipQuarter,
    OwnershipTranslationError,
    evaluate_institutional_trend,
    quarter_end,
    translate_institutional_ownership,
)
from data.provider_adapters.fmp.models import FetchProvenance, InstitutionalOwnershipSummary

AS_OF = datetime(2026, 9, 4, tzinfo=UTC)
FETCHED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _summary(year: int | None = 2026, quarter: int | None = 2, **payload):
    return InstitutionalOwnershipSummary(
        provenance=FetchProvenance(
            endpoint="institutional_ownership_summary",
            url_path="/stable/institutional-ownership/symbol-positions-summary",
            fetched_at=FETCHED_AT,
        ),
        symbol="TEST",
        year=year,
        quarter=quarter,
        raw=payload,
    )


def _payload_only_summary(payload: dict) -> InstitutionalOwnershipSummary:
    """A summary the fetcher was given no year/quarter for, so the period
    has to come out of the payload. Built directly rather than through
    `_summary` because the payload's own keys collide with that helper's
    parameter names."""
    return InstitutionalOwnershipSummary(
        provenance=FetchProvenance(
            endpoint="institutional_ownership_summary",
            url_path="/stable/institutional-ownership/symbol-positions-summary",
            fetched_at=FETCHED_AT,
        ),
        symbol="TEST",
        year=None,
        quarter=None,
        raw=payload,
    )


def _period(year: int, quarter: int, *, investors=None, change=None, shares=None, percent=None):
    return InstitutionalPeriod(
        period=OwnershipQuarter(year, quarter),
        investors_holding=investors,
        investors_holding_change=change,
        total_shares=Decimal(str(shares)) if shares is not None else None,
        ownership_percent=Decimal(str(percent)) if percent is not None else None,
    )


def _evaluate(periods) -> InstitutionalTrend:
    return evaluate_institutional_trend(
        security_id=uuid4(),
        as_of=AS_OF,
        periods=periods,
        config_version_label="test-version",
    )


# --------------------------------------------------------------------------
# No verdict, ever
# --------------------------------------------------------------------------


def test_the_trend_carries_no_raised_field_at_all():
    """The design constraint stated as a test rather than a comment.

    Not "raised is None" — the attribute does not exist. A future edit
    adding a boolean has to delete this test to do it, which is the point:
    how much ownership movement matters is the reader's call.
    """
    trend = _evaluate([_period(2026, 2, investors=300)])

    assert not hasattr(trend, "raised")
    assert "raised" not in trend.as_dict()


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


def test_two_quarters_produce_a_change_in_both_figures():
    trend = _evaluate(
        [
            _period(2026, 2, investors=310, shares=1_200_000, percent=64.5),
            _period(2026, 1, investors=280, shares=1_000_000, percent=60.0),
        ]
    )

    assert trend.period.as_tuple() == (2026, 2)
    assert trend.prior_period.as_tuple() == (2026, 1)
    assert trend.investors_holding == 310
    assert trend.investors_holding_change == 30
    assert trend.total_shares_change_percent == Decimal("20.0")
    assert trend.ownership_percent == Decimal("64.5")


def test_a_first_observation_reports_figures_but_no_changes():
    """`None`, not zero. A single quarter has nothing to have changed
    from, and reporting no change would claim a stability nobody
    measured."""
    trend = _evaluate([_period(2026, 2, investors=310, shares=1_200_000)])

    assert trend.investors_holding == 310
    assert trend.investors_holding_change is None
    assert trend.total_shares_change_percent is None
    assert trend.prior_period is None
    assert trend.detail["prior_quarter_available"] is False


def test_a_provider_supplied_investor_change_is_preferred_over_a_computed_one():
    """The provider's own figure is the more authoritative statement of
    what it observed; the detail records which was used."""
    trend = _evaluate(
        [
            _period(2026, 2, investors=310, change=25),
            _period(2026, 1, investors=280),
        ]
    )

    assert trend.investors_holding_change == 25  # not 310 - 280
    assert trend.detail["investors_holding_change_source"] == "provider"


def test_the_investor_change_is_computed_when_the_provider_omits_it():
    trend = _evaluate([_period(2026, 2, investors=310), _period(2026, 1, investors=280)])

    assert trend.investors_holding_change == 30
    assert trend.detail["investors_holding_change_source"] == "computed"


def test_a_falling_position_reports_a_negative_change():
    """Ownership going down is as much a reading as ownership going up —
    there is no verdict, so nothing here treats one direction as the
    interesting one."""
    trend = _evaluate(
        [
            _period(2026, 2, investors=200, shares=800_000),
            _period(2026, 1, investors=280, shares=1_000_000),
        ]
    )

    assert trend.investors_holding_change == -80
    assert trend.total_shares_change_percent == Decimal("-20.0")


def test_a_zero_prior_share_count_yields_no_percentage_rather_than_a_division_error():
    trend = _evaluate([_period(2026, 2, shares=500_000), _period(2026, 1, shares=0)])

    assert trend.total_shares_change_percent is None


def test_no_stored_quarter_at_all_is_never_ingested():
    trend = _evaluate(None)

    assert trend.unavailable is MissReason.NEVER_INGESTED
    assert trend.period is None
    assert trend.investors_holding is None


# --------------------------------------------------------------------------
# Quarter arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,expected",
    [
        (OwnershipQuarter(2026, 1), date(2026, 3, 31)),
        (OwnershipQuarter(2026, 2), date(2026, 6, 30)),
        (OwnershipQuarter(2026, 3), date(2026, 9, 30)),
        (OwnershipQuarter(2026, 4), date(2026, 12, 31)),
    ],
)
def test_quarter_ends_land_on_the_right_day(period: OwnershipQuarter, expected: date):
    assert quarter_end(period) == expected


def test_the_quarter_before_q1_is_the_previous_years_q4():
    assert OwnershipQuarter(2026, 1).previous().as_tuple() == (2025, 4)


# --------------------------------------------------------------------------
# PIT: the 45-day filing deadline
# --------------------------------------------------------------------------


def test_a_quarters_holdings_are_not_knowable_until_the_filing_deadline():
    """The leak this module could most easily have had. Treating a
    quarter's holdings as knowable at the quarter end would hand a
    backtest six weeks of hindsight on exactly the accumulation this
    signal exists to surface."""
    stored = translate_institutional_ownership(_summary(2026, 2), uuid4())
    thresholds = OwnershipThresholds()

    assert stored.pit.event_time.date() == date(2026, 6, 30)
    # Knowable 45 days later at the earliest, not on the day the quarter
    # closed. `>=` rather than `==`: the deadline is a floor, and a fetch
    # that happens after it observes the quarter *then* — a revision seen
    # in September was not knowable in August. Equality here would be
    # asserting the implementation rather than the guarantee, and would
    # make the honest late-observation case look like a regression.
    assert stored.pit.availability_time.date() >= date(2026, 8, 14)
    assert stored.pit.availability_time > stored.pit.event_time
    assert (
        stored.pit.availability_time - stored.pit.event_time
    ).days >= thresholds.institutional_availability_lag.days


def test_a_quarter_observed_long_after_its_deadline_is_observed_then():
    """The other half of the 45-day rule, and the half that was missing.

    The deadline is a floor on when a quarter *can* be known, not a claim
    about when this particular reading was. Filings keep arriving across
    those 45 days and amendments arrive later still, so a fetch in
    September saw figures that did not exist in August — stamping them
    with the August deadline would say ARGUS could have known a number
    before it was filed, which is the same leak the floor exists to
    prevent, pointing the other way.
    """
    stored = translate_institutional_ownership(_summary(2026, 2), uuid4())

    # Q2 2026 closed on 30 June; the deadline was 14 August; the fetch
    # was on 4 September.
    assert stored.pit.observation_time == FETCHED_AT


def test_a_quarter_fetched_before_its_deadline_still_waits_for_it():
    """The floor holds when the fetch is early.

    A provider that returns a quarter's partial figures before the filing
    window closes must not make them readable early — the floor is what
    stops a backtest seeing accumulation six weeks before anyone could
    have.
    """
    early = InstitutionalOwnershipSummary(
        provenance=FetchProvenance(
            endpoint="institutional_ownership_summary",
            url_path="/stable/institutional-ownership/symbol-positions-summary",
            fetched_at=datetime(2026, 7, 5, tzinfo=UTC),
        ),
        symbol="TEST",
        year=2026,
        quarter=2,
        raw={"investorsHolding": 10},
    )

    stored = translate_institutional_ownership(early, uuid4())

    assert stored.pit.observation_time.date() >= date(2026, 8, 14)


def test_the_same_figures_fingerprint_the_same_and_changed_ones_do_not():
    """What makes the row key able to tell a re-fetch from a revision.

    Two fetches of a quarter carry different `fetched_at` values, so the
    observation instant cannot answer "is this the same fact". The
    figures can, and this is the property the uniqueness key rests on.
    """
    first = translate_institutional_ownership(_summary(2026, 2, investorsHolding=120), uuid4())
    again = translate_institutional_ownership(_summary(2026, 2, investorsHolding=120), uuid4())
    revised = translate_institutional_ownership(_summary(2026, 2, investorsHolding=340), uuid4())

    assert first.fingerprint == again.fingerprint
    assert first.fingerprint != revised.fingerprint


def test_a_field_outside_the_figures_does_not_look_like_a_revision():
    """A provider echoing a request id must not create a row a day.

    `FINGERPRINTED_FIELDS` covers the figures ARGUS reads and nothing
    else, so noise in the payload changes no key — which is the
    difference between a table that grows when the facts change and one
    that grows because the provider is chatty.
    """
    plain = translate_institutional_ownership(_summary(2026, 2, investorsHolding=120), uuid4())
    chatty = translate_institutional_ownership(
        _summary(2026, 2, investorsHolding=120, requestId="abc-123"), uuid4()
    )

    assert plain.fingerprint == chatty.fingerprint


def test_the_lag_is_not_applied_twice():
    """`observation_time` already carries the deadline, so availability
    must equal it rather than adding another 45 days."""
    stored = translate_institutional_ownership(_summary(2026, 2), uuid4())

    assert stored.pit.availability_time == stored.pit.observation_time


# --------------------------------------------------------------------------
# Field-name tolerance
# --------------------------------------------------------------------------


def test_the_quarter_comes_from_the_request_parameters_when_given():
    """The fetcher controls these, so they are facts rather than guesses."""
    stored = translate_institutional_ownership(_summary(2025, 4), uuid4())

    assert stored.period.as_tuple() == (2025, 4)


@pytest.mark.parametrize("year_alias", FIELD_ALIASES["year"])
@pytest.mark.parametrize("quarter_alias", FIELD_ALIASES["quarter"])
def test_the_quarter_is_read_from_the_payload_when_the_request_did_not_say(
    year_alias: str, quarter_alias: str
):
    """Whichever spelling FMP turns out to use, the period resolves and
    the row records which key it came from."""
    summary = _payload_only_summary({year_alias: 2026, quarter_alias: 3})
    stored = translate_institutional_ownership(summary, uuid4())

    assert stored.period.as_tuple() == (2026, 3)
    assert stored.lineage["resolved_fields"]["year"] == year_alias
    assert stored.lineage["resolved_fields"]["quarter"] == quarter_alias


def test_a_summary_naming_no_period_is_rejected_rather_than_dated():
    """A summary filed under the wrong quarter would be compared against
    the wrong prior quarter, and the change figure would look perfectly
    reasonable while being nonsense."""
    with pytest.raises(OwnershipTranslationError, match="no usable"):
        translate_institutional_ownership(_summary(None, None, investorsHolding=300), uuid4())


def test_an_impossible_quarter_is_rejected():
    with pytest.raises(OwnershipTranslationError):
        translate_institutional_ownership(_summary(2026, 7), uuid4())


def test_the_untouched_payload_is_kept_for_a_later_correction():
    """Field names are unconfirmed, so nothing is dropped: a key this
    adapter did not recognise today is what a correction reads tomorrow."""
    stored = translate_institutional_ownership(
        _summary(2026, 2, someUnknownField=42, investorsHolding=300), uuid4()
    )

    assert stored.data["someUnknownField"] == 42
    assert stored.data["investorsHolding"] == 300
