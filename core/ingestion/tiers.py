"""Is this security due for a deep refresh? — the tiered-frequency decision.

Pure. No database, no clock, no provider: given a security's *current*
watchlist phase, the record of its last completed refresh, and the run's
target date, this says whether to refresh it and which rule fired.

## Two rules, and why the second one is needed

**Interval elapsed.** The tier for the security's current phase has a
number of days attached, and that many days have passed since the last
completed refresh.

**Tier escalation.** The security has moved into a *strictly more urgent*
tier than the one that drove its last refresh. This forces a refresh
immediately, regardless of elapsed time.

The second rule is the whole reason this module reads a last-known phase
at all. Without it, a security refreshed on a Monday while in DOWN_TREND
and promoted to CONSOLIDATION on the Wednesday would sit for another
twenty-eight days — the interval it is no longer on — before anyone
looked at it again. Interval arithmetic against `last_refresh_at` alone
cannot see that, because nothing about the elapsed time changed when the
phase did.

"Strictly more urgent" is derived from the intervals rather than from a
second hand-written ordering: a shorter interval *is* more urgent. That
makes BREAKOUT_READY -> UPTREND not an escalation, which is correct —
both are daily, so there is nothing to escalate to.

## One refresh per security per run date

Checked before either rule, so a re-run of the same day refreshes
nothing even if a security's phase changed in between. Without it the
escalation rule would fire on the second run, spend the requests, and
then fail to log anything — the log table's unique constraint holds the
same invariant from the other side.

## Days, not timestamps

Every comparison here is between dates. The intervals are declared in
whole days, the job runs once per trading day, and a wall-clock
comparison would let a cron firing that drifts ninety seconds early skip
a daily-tier security by arriving 23h 59m after the last refresh. The
exact instant is still recorded — see `refreshed_at` on the log table —
it is just not what the decision turns on.

## A security on no watchlist is not due

`UNCLASSIFIED` and `DISTRIBUTION` are on no list, and neither is a
security with no `market_state` row at all. None of them get a tier, so
none of them get refreshed, and that is the same reasoning Module 10
gives for keeping them off the lists: a security nobody has judged must
be absent rather than defaulted into a category.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.ingestion.config import IngestionSettings

#: Why a refresh was decided on. Recorded on the log row.
NEVER_REFRESHED = "never_refreshed"
INTERVAL_ELAPSED = "interval_elapsed"
TIER_ESCALATION = "tier_escalation"

#: Why it was not.
NOT_DUE = "not_due"
NO_TIER = "no_tier"
#: Already refreshed on this run's own target date. Checked before the
#: escalation rule so that a second run of the same day cannot re-fetch a
#: security whose phase moved between the two — which would spend
#: requests to write a log row the unique constraint then rejects.
ALREADY_REFRESHED = "already_refreshed_today"

#: The recorded tier no longer exists in configuration. Treated as "no
#: escalation information" rather than as an error: a watchlist removed
#: from Module 10 must not strand every security that was last refreshed
#: while on it.
UNKNOWN_PRIOR_TIER = "unknown_prior_tier"

__all__ = [
    "ALREADY_REFRESHED",
    "INTERVAL_ELAPSED",
    "NEVER_REFRESHED",
    "NOT_DUE",
    "NO_TIER",
    "TIER_ESCALATION",
    "UNKNOWN_PRIOR_TIER",
    "DueDecision",
    "RefreshRecord",
    "decide",
]


@dataclass(frozen=True, slots=True)
class RefreshRecord:
    """One security's last completed deep refresh, as recorded."""

    watchlist: str
    refreshed_on: date


@dataclass(frozen=True, slots=True)
class DueDecision:
    """Whether to refresh, which rule decided it, and in plain words why."""

    due: bool
    trigger: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"due": self.due, "trigger": self.trigger, "reason": self.reason}


def decide(
    *,
    watchlist: str | None,
    last: RefreshRecord | None,
    target_date: date,
    settings: IngestionSettings | None = None,
) -> DueDecision:
    """Whether `watchlist`'s tier makes this security due on `target_date`.

    `watchlist` is the security's phase read live from the `market_state`
    projection at decision time — never a stored value. `None` means it
    is on no watchlist.
    """
    resolved = settings or IngestionSettings()

    if watchlist is None:
        return DueDecision(
            due=False,
            trigger=NO_TIER,
            reason=(
                "On no watchlist, so on no refresh tier. UNCLASSIFIED and "
                "DISTRIBUTION are internal states Module 10 keeps off every list; "
                "a security with no state row has not been judged at all."
            ),
        )

    interval = resolved.interval_days(watchlist)

    if last is None:
        return DueDecision(
            due=True,
            trigger=NEVER_REFRESHED,
            reason=(
                f"No completed deep refresh on record, and the security is on "
                f"{watchlist} ({interval}-day tier). A first refresh is always due."
            ),
        )

    if last.refreshed_on >= target_date:
        return DueDecision(
            due=False,
            trigger=ALREADY_REFRESHED,
            reason=(
                f"Already refreshed on {last.refreshed_on.isoformat()}, which is this "
                "run's own target date. One refresh per security per run date, the "
                "same invariant the log table's unique constraint holds."
            ),
        )

    escalation = _escalation(watchlist, last.watchlist, interval, resolved)
    if escalation is not None:
        return escalation

    elapsed = (target_date - last.refreshed_on).days
    if elapsed >= interval:
        return DueDecision(
            due=True,
            trigger=INTERVAL_ELAPSED,
            reason=(
                f"{elapsed} days since the last refresh on "
                f"{last.refreshed_on.isoformat()}, and {watchlist}'s tier is "
                f"{interval} days."
            ),
        )

    return DueDecision(
        due=False,
        trigger=NOT_DUE,
        reason=(
            f"Refreshed {elapsed} days ago on {last.refreshed_on.isoformat()} under "
            f"{last.watchlist}; {watchlist}'s tier is {interval} days and this is "
            "not an escalation."
        ),
    )


def _escalation(
    watchlist: str,
    prior: str,
    interval: int,
    settings: IngestionSettings,
) -> DueDecision | None:
    """A move into a strictly shorter tier, which forces a refresh now."""
    try:
        prior_interval = settings.interval_days(prior)
    except KeyError:
        # The tier this security was last refreshed under is gone from
        # configuration. There is no escalation to detect against it, so
        # fall through to interval arithmetic on the current tier.
        return None

    if interval >= prior_interval:
        return None

    return DueDecision(
        due=True,
        trigger=TIER_ESCALATION,
        reason=(
            f"Moved from {prior} ({prior_interval}-day tier) to {watchlist} "
            f"({interval}-day tier) since the last refresh. A security that has "
            "become more urgent is refreshed immediately rather than waiting out "
            "the remainder of an interval it is no longer on."
        ),
    )
