"""Module 08's adversarial leakage test.

Module 07 proved that *querying* one record is PIT-correct. This proves
the harder thing: that a **derived** number is. Features are the layer
where leakage stops being obvious — nobody looks at `drawdown_pct = -0.02`
and asks which corporate actions went into it.

## The concrete scenario

A 4-for-1 split takes effect on 1 July 2020. The provider backfills the
action row on 1 September — two months late, which is entirely routine.
The raw price series therefore shows a 75% overnight collapse on 1 July
that is not a collapse at all.

- **`as_of` 1 August 2020**: the split row exists in the database but was
  not knowable. ARGUS must compute features from the raw series and report
  a ~75% drawdown, because that is what ARGUS knew that day.
- **`as_of` 1 October 2020**: the split is knowable. The pre-split bars
  are back-adjusted, the series is continuous, and the drawdown vanishes.

Same function, same security, one argument different.

## Why this shape of test, and the cost it makes visible

The August reading is *deliberately* "wrong" about the world — the stock
did not fall 75%. It is right about ARGUS's knowledge, which is the only
thing a backtest may condition on. Getting this backwards is not a
theoretical concern: `apply_adjustments` (Module 05) applies every action
ever ingested, so a feature engine built on it would silently produce the
October answer for every historical date in the database. That is a
plausible-looking wrong number in fifteen years of backtests, and nothing
downstream would ever flag it.

`test_the_naive_adjustment_leaks_the_future_split` constructs exactly that
mistake against this same fixture and proves it leaks.

## Proving these tests are load-bearing

Same discipline as Module 07: break the enforcement, confirm the tests
catch it, restore. Four attempts, and the third is the one worth reading.

**Attempt 1** — in `core/data_validation/bulk.py`, changed
`load_corporate_actions_as_of`'s filter from `availability_time <= :as_of`
to `effective_date <= :as_of`. Plausible-looking: it still filters, and it
still names a date. Four tests failed at once;
`test_features_before_the_split_is_knowable_use_raw_prices` reported a
drawdown of **0.0** where it requires < -0.7 — the August query saw a
split that had taken effect but had not yet been reported. Restored.

**Attempt 2** — in `core/feature_engine/panel.py`, changed `load_panel` to
call `load_corporate_actions_as_of(connection, security_ids,
datetime.now(UTC))` instead of passing its own `as_of`. This is exactly
`apply_adjustments`'s semantics reintroduced at the call site, and it is
the more realistic regression: the loader is still correct, the caller
simply asks it the wrong question. Three tests failed, again with a
drawdown of 0.0. Note that `test_the_boundary_is_inclusive_to_the_microsecond`
**passed** — correctly, because it probes the loader, which was not the
broken component. Restored.

**Attempt 3** — in `build_adjustment_factors`, changed `factors.index <
effective` to `<=`, an off-by-one on the adjustment boundary rather than
on the PIT filter. **All eight tests passed.** That is the useful result,
and it turned out to have two separate causes:

1. Every test in this file read `drawdown_pct`, which is computed from the
   *last* bar of the panel. The off-by-one damages the bar in the middle
   of the series, on the split date itself, where nothing was looking.
2. More importantly, the operator was never deciding anything. Bars are
   timestamped at the session close (20:00 UTC) and `effective_date` is
   written at midnight, so no bar ever compares equal and `<` and `<=`
   are the same predicate on this data.

Cause 2 is a latent bug, not a test gap: the module was correct only
because two layers happened to agree on a time-of-day convention that
nothing enforces. `canonical_corporate_actions.effective_date` is a
`DateTime(timezone=True)` column holding what Module 05 models as a plain
`date`; an effective date stored at 23:59 makes the session-close bar on
the effective day compare as "earlier" and be adjusted a *second* time —
a one-bar 75% hole in the middle of an otherwise flat series. Verified
directly before fixing: the same split at 00:00 produced
`[100.0, 100.0, 100.0]` and at 23:59 produced `[100.0, 25.0, 100.0]`.

Fixed in `panel._effective_day` by comparing calendar days, which is what
Module 05's `apply_adjustments` already does (`factor.effective_date >
bar_date`, date against date). Both gaps are now covered:
`test_the_adjusted_series_is_continuous_across_the_split` checks the whole
series rather than its last bar, and
`test_adjustment_ignores_the_effective_dates_time_of_day` parametrizes
over midnight, mid-session and 23:59.

**Attempt 4**, against the fix — removed `.normalize()` from
`_effective_day`. Exactly one parametrization failed
(`...time_of_day[late-in-the-day]`) and the other twelve tests passed,
which is the localization you want: the failure names the condition that
broke rather than lighting up the whole file. Restored.

`git diff` shows `bulk.py` unmodified and `panel.py` carrying only the
calendar-day fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from core.data_validation.bulk import (
    load_corporate_actions_as_of,
    load_latest_fundamentals_as_of,
)
from core.feature_engine.engine import compute_features
from core.feature_engine.panel import build_adjustment_factors, load_panel
from core.feature_engine.spec import FeatureSpec
from data.canonical_model.records import CanonicalStatementType
from infra.db.schema.canonical import canonical_fundamentals
from tests.integration.feature_engine.conftest import insert_bars, insert_split

#: 1 July 2020: the split takes effect and the raw price quarters.
SPLIT_EFFECTIVE = datetime(2020, 7, 1, tzinfo=UTC)
#: 1 September 2020: the provider finally reports it. Two months late.
SPLIT_AVAILABLE = datetime(2020, 9, 1, 12, 0, tzinfo=UTC)

BEFORE_THE_SPLIT_IS_KNOWABLE = datetime(2020, 8, 1, tzinfo=UTC)
AFTER_THE_SPLIT_IS_KNOWABLE = datetime(2020, 10, 1, tzinfo=UTC)

PRE_SPLIT_PRICE = 400.0
POST_SPLIT_PRICE = 100.0


@pytest.fixture
def split_security(connection: Connection, register) -> UUID:
    """A security whose raw series shows a 75% cliff that never happened.

    Flat at $400 from January 2019 to June 2020, flat at $100 after the
    4-for-1 split on 1 July. Flat on purpose: any drawdown the features
    report is attributable to the split alone, with no price trend to
    argue about.
    """
    security_id = register("SPLTR")

    # 390 business days from 2 Jan 2019 run through 30 June 2020, the last
    # session before the split; the second run starts on the split date.
    # Long enough that the 252-bar structural window is fully populated at
    # both `as_of` dates — a short fixture would produce NaN and the test
    # would prove nothing.
    insert_bars(connection, security_id, start=datetime(2019, 1, 2), closes=[PRE_SPLIT_PRICE] * 390)
    insert_bars(connection, security_id, start=SPLIT_EFFECTIVE, closes=[POST_SPLIT_PRICE] * 130)

    insert_split(
        connection,
        security_id,
        effective_date=SPLIT_EFFECTIVE,
        availability_time=SPLIT_AVAILABLE,
        numerator=4,
        denominator=1,
    )
    return security_id


def drawdown_at(connection: Connection, security_id: UUID, as_of: datetime) -> float:
    """`drawdown_pct` — price against its trailing structural peak.

    Chosen as the probe because it is scale-free: a uniform rescaling of
    the whole series leaves it unchanged, so any movement in it is the
    *discontinuity* the split creates, never the adjustment's overall
    level. That makes the leak unmistakable rather than arguable.
    """
    vector = compute_features(connection, security_id, as_of, spec=FeatureSpec())
    value = vector.features["drawdown_pct"]
    assert value is not None, "the fixture should have enough history to compute a drawdown"
    return value


# --------------------------------------------------------------------------
# THE test
# --------------------------------------------------------------------------


def test_features_before_the_split_is_knowable_use_raw_prices(
    connection: Connection, split_security: UUID
):
    """On 1 August, ARGUS had not been told about the split. It must say so.

    The unadjusted series really does show a 75% fall, and a feature
    computed that day must reflect it. Reporting the smooth, adjusted
    number instead would mean the backtest saw a September data delivery
    while standing in August.
    """
    assert drawdown_at(connection, split_security, BEFORE_THE_SPLIT_IS_KNOWABLE) < -0.7


def test_features_after_the_split_is_knowable_use_adjusted_prices(
    connection: Connection, split_security: UUID
):
    """The mirror image: once knowable, the split IS applied.

    Blocking the leak must not mean blocking the data. A series that
    stayed permanently unadjusted would be just as wrong, and would make
    every multi-year structural feature meaningless.
    """
    assert drawdown_at(connection, split_security, AFTER_THE_SPLIT_IS_KNOWABLE) > -0.05


def test_the_two_answers_differ_only_in_the_as_of_argument(
    connection: Connection, split_security: UUID
):
    """One plain parameter separates a live scan from a 2015 replay.

    No mode flag, no separate code path — the property
    `docs/architecture/CROSS_CUTTING_REQUIREMENTS.md` asks Modules 08-16
    to guarantee, asserted rather than assumed.
    """
    before = drawdown_at(connection, split_security, BEFORE_THE_SPLIT_IS_KNOWABLE)
    after = drawdown_at(connection, split_security, AFTER_THE_SPLIT_IS_KNOWABLE)
    assert before < -0.7 < -0.05 < after


def test_the_boundary_is_inclusive_to_the_microsecond(connection: Connection, split_security: UUID):
    """`availability_time <= as_of`, matching Module 07 exactly.

    A boundary that differed between the single-record and bulk loaders
    would be a second enforcement rule, which is the thing keeping these
    loaders in `core/data_validation/` was meant to prevent.
    """
    from datetime import timedelta

    actions_just_before = load_corporate_actions_as_of(
        connection, [split_security], SPLIT_AVAILABLE - timedelta(microseconds=1)
    )
    actions_exactly_on = load_corporate_actions_as_of(connection, [split_security], SPLIT_AVAILABLE)
    assert len(actions_just_before) == 0
    assert len(actions_exactly_on) == 1


def test_the_adjusted_series_is_continuous_across_the_split(
    connection: Connection, split_security: UUID
):
    """Adjusting must remove the cliff without carving a new one.

    The bar *on* the effective date already reflects the split, so scaling
    it again puts a one-bar 75% dip into the middle of an otherwise flat
    series. Every adjusted close in this fixture should be $100: the
    pre-split $400 bars divided by four, and the post-split bars untouched.

    This test exists because it was missing. The break-attempt pass
    recorded in the module docstring flipped `build_adjustment_factors`
    from `index < effective` to `index <= effective` and **all eight tests
    still passed** — `drawdown_pct` reads the last bar of the panel and
    never looks at 1 July. A leakage suite that only probes the final bar
    cannot see a defect in the middle of the series, so this one probes
    the whole series.
    """
    panel = load_panel(
        connection, [split_security], AFTER_THE_SPLIT_IS_KNOWABLE, max_lookback_bars=252
    )
    adjusted = panel.close_adj[split_security].dropna()

    assert len(adjusted) > 252
    assert adjusted.min() == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)
    assert adjusted.max() == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)


def test_the_bar_on_the_effective_date_is_not_adjusted_again(
    connection: Connection, split_security: UUID
):
    """The boundary itself, named explicitly.

    Module 05's convention: bars strictly *before* an action's effective
    date are scaled. Asserting the convention directly means a future
    change to it fails here, where the reason is written down, rather than
    somewhere downstream where it looks like a data problem.
    """
    panel = load_panel(
        connection, [split_security], AFTER_THE_SPLIT_IS_KNOWABLE, max_lookback_bars=252
    )
    closes = panel.close_adj[split_security]
    on_the_day = closes[closes.index >= SPLIT_EFFECTIVE].iloc[0]
    day_before = closes[closes.index < SPLIT_EFFECTIVE].iloc[-1]

    assert on_the_day == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)
    assert day_before == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)


@pytest.mark.parametrize(
    "effective_time",
    [
        pytest.param(datetime(2020, 7, 1, 0, 0, tzinfo=UTC), id="midnight"),
        pytest.param(datetime(2020, 7, 1, 13, 30, tzinfo=UTC), id="mid-session"),
        pytest.param(datetime(2020, 7, 1, 23, 59, tzinfo=UTC), id="late-in-the-day"),
    ],
)
def test_adjustment_ignores_the_effective_dates_time_of_day(
    connection: Connection, register, effective_time: datetime
):
    """The split lands on a calendar day, whatever hour the column holds.

    Found while proving the tests above load-bearing. Bars are timestamped
    at the session close (20:00 UTC) and `effective_date` is a `DateTime`
    column holding what Module 05 models as a plain `date`; comparing the
    two as timestamps worked only because that column is written at
    midnight today. Stored at 23:59 instead, the session-close bar on the
    effective day compared as "earlier" and was adjusted a second time —
    a one-bar 75% hole in the middle of an otherwise flat series, which no
    other test in this file could see because they all read the last bar.

    Parametrized over three times of day so the guarantee is the calendar
    day, not the coincidence.
    """
    security_id = register(f"TOD{effective_time.hour}")
    insert_bars(connection, security_id, start=datetime(2020, 6, 1), closes=[PRE_SPLIT_PRICE] * 22)
    insert_bars(connection, security_id, start=SPLIT_EFFECTIVE, closes=[POST_SPLIT_PRICE] * 22)
    insert_split(
        connection,
        security_id,
        effective_date=effective_time,
        availability_time=SPLIT_AVAILABLE,
        numerator=4,
        denominator=1,
    )

    panel = load_panel(
        connection, [security_id], AFTER_THE_SPLIT_IS_KNOWABLE, max_lookback_bars=252
    )
    adjusted = panel.close_adj[security_id].dropna()

    # The panel must actually straddle the split, or a flat series would
    # prove nothing — it could just be the post-split bars on their own.
    assert (adjusted.index < SPLIT_EFFECTIVE).any()
    assert (adjusted.index >= SPLIT_EFFECTIVE).any()

    assert adjusted.min() == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)
    assert adjusted.max() == pytest.approx(POST_SPLIT_PRICE, rel=1e-6)


# --------------------------------------------------------------------------
# The mistake, made explicit
# --------------------------------------------------------------------------


def test_the_naive_adjustment_leaks_the_future_split(connection: Connection, split_security: UUID):
    """The wrong version, run against the same fixture, to make the danger real.

    This is what `apply_adjustments` does: take every corporate action in
    the database, ignoring when it became knowable, and apply it. Here the
    unfiltered action set is fed to `build_adjustment_factors` for the
    August panel — and the 75% cliff disappears, two months before ARGUS
    had any way to know it should.

    The real `load_panel` sources its actions from
    `load_corporate_actions_as_of` and therefore cannot do this.
    """
    panel = load_panel(
        connection,
        [split_security],
        BEFORE_THE_SPLIT_IS_KNOWABLE,
        max_lookback_bars=252,
        apply_corporate_actions=False,
    )

    # Every action ever ingested — no availability filter at all. This is
    # the single line that separates a correct engine from a leaking one.
    every_action_ever = load_corporate_actions_as_of(
        connection, [split_security], datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert len(every_action_ever) == 1, "fixture sanity: the split exists in the database"

    leaked_factors = build_adjustment_factors(every_action_ever, panel.close_raw)
    leaked_close = panel.close_raw * leaked_factors

    peak = leaked_close[split_security].max()
    latest = leaked_close[split_security].iloc[-1]
    leaked_drawdown = (latest - peak) / peak

    assert leaked_drawdown > -0.05, (
        "fixture sanity check: the naive adjustment smooths away a split ARGUS could not yet see"
    )

    # And the honest computation, on the same bars, does not.
    assert drawdown_at(connection, split_security, BEFORE_THE_SPLIT_IS_KNOWABLE) < -0.7


def test_the_panel_loader_never_reaches_for_apply_adjustments(
    connection: Connection, split_security: UUID
):
    """A structural check, not a behavioural one.

    The behavioural tests above prove the current code is correct. This
    proves the *forbidden call* is absent, which is what stops a future
    edit from reintroducing the leak while every other test still passes —
    `apply_adjustments` would produce the right answer for a `as_of` of
    today, and only the historical dates would be quietly wrong.
    """
    from pathlib import Path

    source = Path("core/feature_engine/panel.py").read_text()
    assert "apply_adjustments" in source, "the docstring should explain why it is not used"
    assert "from data.normalization.adjustments import" not in source
    assert "apply_adjustments(" not in source


# --------------------------------------------------------------------------
# Fundamentals: the same rule, through this module's bulk primitive
# --------------------------------------------------------------------------


def test_the_bulk_fundamentals_loader_blocks_a_restatement_leak(connection: Connection, register):
    """Module 07's third requirement, exercised through Module 08's loader.

    `load_latest_fundamentals_as_of` is new code added by this module, and
    a bulk `DISTINCT ON` is an easy place to get the ordering subtly wrong
    — `ORDER BY fiscal_period_end DESC, availability_time DESC` after the
    availability filter, not before it. Groups A-E do not consume
    fundamentals today, but Module 09's eligibility gates will, and an
    untested primitive is where that leak would start.
    """
    security_id = register("RSTMT")
    period_end = datetime(2019, 3, 31, tzinfo=UTC)

    for available, revenue in (
        (datetime(2019, 5, 15, tzinfo=UTC), 90_753_000_000),
        (datetime(2019, 8, 1, tzinfo=UTC), 91_200_000_000),
    ):
        connection.execute(
            canonical_fundamentals.insert().values(
                security_id=security_id,
                statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
                fiscal_period="Q1",
                fiscal_period_end=period_end,
                event_time=period_end,
                observation_time=available,
                availability_time=available,
                ingestion_time=available,
                data={"revenue": revenue},
            )
        )

    before = load_latest_fundamentals_as_of(
        connection,
        [security_id],
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 6, 1, tzinfo=UTC),
    )
    after = load_latest_fundamentals_as_of(
        connection,
        [security_id],
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 9, 1, tzinfo=UTC),
    )

    assert before.iloc[0]["data"]["revenue"] == 90_753_000_000
    assert after.iloc[0]["data"]["revenue"] == 91_200_000_000


def test_the_bulk_loader_returns_one_row_per_security_not_per_restatement(
    connection: Connection, register
):
    """`DISTINCT ON (security_id)` — the property that makes it a bulk load.

    Without it the caller would receive every restatement of every period
    and have to reduce client-side, which for a ten-thousand-security
    universe is the difference between one query and an unusable one.
    """
    ids = [register(ticker) for ticker in ("AAA", "BBB")]
    for security_id in ids:
        for available, revenue in (
            (datetime(2019, 5, 15, tzinfo=UTC), 100),
            (datetime(2019, 8, 1, tzinfo=UTC), 200),
        ):
            connection.execute(
                canonical_fundamentals.insert().values(
                    security_id=security_id,
                    statement_type=CanonicalStatementType.INCOME_STATEMENT.value,
                    fiscal_period="Q1",
                    fiscal_period_end=datetime(2019, 3, 31, tzinfo=UTC),
                    event_time=datetime(2019, 3, 31, tzinfo=UTC),
                    observation_time=available,
                    availability_time=available,
                    ingestion_time=available,
                    data={"revenue": revenue},
                )
            )

    result = load_latest_fundamentals_as_of(
        connection,
        ids,
        CanonicalStatementType.INCOME_STATEMENT,
        datetime(2019, 9, 1, tzinfo=UTC),
    )
    assert len(result) == 2
    assert set(result["security_id"]) == set(ids)
