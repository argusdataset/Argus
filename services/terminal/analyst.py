"""What analysts say: estimates, price targets, rating actions.

Three reads over two tables, and they are grouped here rather than in
`company.py` because they answer a different kind of question. A
financial statement is what the company reported; everything in this file
is what somebody *thinks* about the company. Keeping them in separate
modules makes that distinction visible in the import list rather than
only in a docstring.

## Opinions are shown, never used

Nothing in ARGUS reads this data. It is not a scoring input, not an
eligibility gate, not a market-state signal — Module 19 serves it to a
human and that is the whole of it. The structural test in
`tests/unit/news_signals/test_isolation_from_scoring_and_market_state.py`
holds the reverse direction (no scoring module reads these tables), and
this module holds the forward one by importing nothing from `core/`
except the PIT result type.

That restraint is the point rather than an omission. A consensus target
is a number a dozen banks negotiated with their own incentives; ARGUS
displays it beside the price and lets the reader weigh it.

## The three shapes, and why they are not one endpoint

- **Estimates** are period-keyed and revised, so they come back as a
  series of periods with the newest revision of each.
- **Price targets** are a rolling state with no period at all, so the
  newest observation comes back — two of them, one per endpoint, kept
  apart rather than merged.
- **Grades** are an event stream, so every action comes back rather than
  a current rating. "Who moved this week" is the question; collapsing it
  to a single current grade would answer a different one.

Merging them into one `/analyst` response would flatten three different
notions of "now" into one, and a consumer could not tell which it had.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Connection

from core.data_validation.result import MissReason
from data.canonical_model.records import CanonicalDisclosureType, CanonicalSnapshotType
from services.terminal.company import resolve_company
from services.terminal.config import TerminalConfig
from services.terminal.schemas import (
    AnalystEstimatePeriod,
    AnalystEstimatesResponse,
    AnalystGradeAction,
    AnalystGradesResponse,
    PriceTargetResponse,
    Unavailable,
)
from services.terminal.stored import (
    grade_actions,
    latest_disclosures,
    latest_snapshot,
    unavailable,
)

__all__ = ["read_analyst_estimates", "read_analyst_grades", "read_price_target"]


def read_analyst_estimates(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> AnalystEstimatesResponse:
    """Forward consensus, newest fiscal period first.

    One row per period — the newest revision of each that was knowable at
    `as_of`, which is `stored.latest_disclosures`' restatement rule. A
    June estimate for FY2027 revised in September shows the June figure
    to a July query and the September one to an October query, and
    neither query has to know the other exists.
    """
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    rows, reason = latest_disclosures(
        connection,
        profile.security_id,
        CanonicalDisclosureType.ANALYST_ESTIMATES,
        as_of=moment,
        limit=config.limits.bounded_disclosure_limit(limit),
    )

    return AnalystEstimatesResponse(
        security=profile,
        as_of=moment,
        periods=[
            AnalystEstimatePeriod(
                fiscal_period=row.fiscal_period,
                fiscal_period_end=row.fiscal_period_end,
                availability_time=row.availability_time,
                data=dict(row.data),
            )
            for row in rows
        ],
        unavailable=unavailable(reason) if reason is not None else None,
    )


def read_price_target(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
) -> PriceTargetResponse:
    """The consensus target and the counts behind it, unmerged.

    Two reads, because they are two snapshot types — see
    `translate_price_target` on why storing them as one would let a
    fetch drop half the picture. Kept apart on the way out for a second
    reason: the consensus reports the figures and the summary reports how
    many analysts stand behind them, and a median target shown with no
    idea whether three or thirty houses produced it is a number a reader
    would trust more than they should.

    `observed_at` is the newer of the two observations — the instant the
    pair *as shown* was true as of. Reporting the older would overstate
    how stale the response is.

    `unavailable` is populated when either half is missing, including
    when the other half was found. A partial answer that does not say it
    is partial is the failure mode `schemas.py` opens by naming.
    """
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    consensus_row, consensus_miss = latest_snapshot(
        connection,
        profile.security_id,
        CanonicalSnapshotType.PRICE_TARGET_CONSENSUS,
        as_of=moment,
    )
    summary_row, summary_miss = latest_snapshot(
        connection,
        profile.security_id,
        CanonicalSnapshotType.PRICE_TARGET_SUMMARY,
        as_of=moment,
    )

    observed = [row.observation_time for row in (consensus_row, summary_row) if row is not None]

    return PriceTargetResponse(
        security=profile,
        as_of=moment,
        consensus=dict(consensus_row.data) if consensus_row is not None else None,
        summary=dict(summary_row.data) if summary_row is not None else None,
        observed_at=max(observed) if observed else None,
        # The stricter of the two reasons when both halves are missing:
        # NEVER_INGESTED only if neither half was ever held, since one
        # half on file makes "never ingested" false for this security.
        unavailable=_target_miss(consensus_miss, summary_miss),
    )


def read_analyst_grades(
    connection: Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> AnalystGradesResponse:
    """Rating actions, newest first. Every action, not a current rating."""
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    rows, reason = grade_actions(
        connection,
        profile.security_id,
        as_of=moment,
        limit=config.limits.bounded_grades_limit(limit),
    )

    return AnalystGradesResponse(
        security=profile,
        as_of=moment,
        grades=[
            AnalystGradeAction(
                graded_at=row.event_time,
                grading_company=row.grading_company,
                action=row.action,
                previous_grade=row.previous_grade,
                new_grade=row.new_grade,
            )
            for row in rows
        ],
        unavailable=unavailable(reason) if reason is not None else None,
    )


def _target_miss(consensus: MissReason | None, summary: MissReason | None) -> Unavailable | None:
    """Why the price-target picture is incomplete, if it is.

    Both halves present means no absence to report. One half present
    means the response *is* partial and says so, which is the whole point
    of not merging them: a consumer that got figures with no counts
    should be able to render "target, analyst count unknown" rather than
    silently show the target alone.

    When both are missing the reasons can differ — a security might have
    a stored consensus outside the cutoff and no summary at all. The
    reported reason is then `NOT_YET_AVAILABLE`, the weaker claim: saying
    "never ingested" of a security that holds one half would be false.
    """
    if consensus is None and summary is None:
        return None
    reasons = [reason for reason in (consensus, summary) if reason is not None]
    if MissReason.NOT_YET_AVAILABLE in reasons:
        return unavailable(MissReason.NOT_YET_AVAILABLE)
    return unavailable(reasons[0])
