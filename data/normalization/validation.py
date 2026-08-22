"""Sanity checks on incoming canonical records.

Not point-in-time enforcement — that is Module 07, at query time. This is
the narrower job of catching records that are *clearly* malformed before
they reach the database, so a provider glitch does not become a permanent
row that quietly distorts every feature computed from it.

The bar for flagging is deliberately high. ARGUS exists to find stocks
that fell 90% and then based for years; genuinely extreme data is the
signal, not the noise, and a validator tuned to "this looks unusual"
would discard exactly the cases the system is built to find. So the
checks here are limited to things that cannot be true — negative volume,
high below low, non-positive prices — plus one heuristic (a large
overnight gap with no corporate action to explain it) that is *flagged
for review*, never silently dropped.

Flagged records are returned to the caller rather than discarded inside
this module. Deciding what to do with a suspect row is an operational
choice, and hiding it here would make that choice invisible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from data.canonical_model.records import CanonicalCorporateAction, CanonicalOhlcvBar

#: A single-session move beyond this, with no corporate action on that
#: date, is flagged for review. Set high on purpose: real securities do
#: move 50% in a day on news, and ARGUS must not discard those.
DEFAULT_GAP_THRESHOLD = Decimal("0.5")


class ValidationIssue(StrEnum):
    """What is wrong with a record."""

    NEGATIVE_VOLUME = "negative_volume"
    NON_POSITIVE_PRICE = "non_positive_price"
    HIGH_BELOW_LOW = "high_below_low"
    CLOSE_OUTSIDE_RANGE = "close_outside_range"
    OPEN_OUTSIDE_RANGE = "open_outside_range"
    DUPLICATE = "duplicate"
    #: Advisory, not fatal — see the module docstring.
    UNEXPLAINED_PRICE_JUMP = "unexplained_price_jump"


#: Issues that make a record unusable. UNEXPLAINED_PRICE_JUMP is absent
#: deliberately: it is a review signal, and a real 60% move is data ARGUS
#: wants, not data it should refuse.
FATAL_ISSUES: frozenset[ValidationIssue] = frozenset(
    {
        ValidationIssue.NEGATIVE_VOLUME,
        ValidationIssue.NON_POSITIVE_PRICE,
        ValidationIssue.HIGH_BELOW_LOW,
        ValidationIssue.CLOSE_OUTSIDE_RANGE,
        ValidationIssue.OPEN_OUTSIDE_RANGE,
        ValidationIssue.DUPLICATE,
    }
)


@dataclass(frozen=True, slots=True)
class FlaggedBar:
    """A bar with at least one issue, kept alongside its reasons."""

    bar: CanonicalOhlcvBar
    issues: tuple[ValidationIssue, ...]

    @property
    def is_fatal(self) -> bool:
        return any(issue in FATAL_ISSUES for issue in self.issues)


@dataclass(slots=True)
class ValidationResult:
    """Bars split into those safe to persist and those needing attention."""

    accepted: list[CanonicalOhlcvBar] = field(default_factory=list)
    flagged: list[FlaggedBar] = field(default_factory=list)

    @property
    def fatal(self) -> list[FlaggedBar]:
        return [entry for entry in self.flagged if entry.is_fatal]

    @property
    def advisory(self) -> list[FlaggedBar]:
        return [entry for entry in self.flagged if not entry.is_fatal]

    def __bool__(self) -> bool:
        return not self.fatal


def validate_bars(
    bars: list[CanonicalOhlcvBar],
    actions: list[CanonicalCorporateAction] | None = None,
    *,
    gap_threshold: Decimal = DEFAULT_GAP_THRESHOLD,
) -> ValidationResult:
    """Check a security's bars for impossible values and unexplained gaps.

    Bars carrying only advisory issues are still accepted — they are
    reported so an operator can look, not withheld from the record.
    """
    result = ValidationResult()
    action_dates = {action.effective_date for action in actions or ()}
    seen: set[tuple[date, object]] = set()
    previous_close: Decimal | None = None

    for bar in sorted(bars, key=lambda item: item.pit.event_time):
        issues: list[ValidationIssue] = []
        bar_date = bar.pit.event_time.date()

        # A restatement legitimately repeats event_time with a later
        # observation_time, so identity here is the pair, matching
        # Module 03's uniqueness constraint.
        key = (bar_date, bar.pit.observation_time)
        if key in seen:
            issues.append(ValidationIssue.DUPLICATE)
        seen.add(key)

        if bar.volume_raw < 0:
            issues.append(ValidationIssue.NEGATIVE_VOLUME)
        if min(bar.open_raw, bar.high_raw, bar.low_raw, bar.close_raw) <= 0:
            issues.append(ValidationIssue.NON_POSITIVE_PRICE)
        if bar.high_raw < bar.low_raw:
            issues.append(ValidationIssue.HIGH_BELOW_LOW)
        if not (bar.low_raw <= bar.close_raw <= bar.high_raw):
            issues.append(ValidationIssue.CLOSE_OUTSIDE_RANGE)
        if not (bar.low_raw <= bar.open_raw <= bar.high_raw):
            issues.append(ValidationIssue.OPEN_OUTSIDE_RANGE)

        if (
            previous_close is not None
            and previous_close > 0
            and bar.close_raw > 0
            and bar_date not in action_dates
        ):
            move = abs(bar.close_raw - previous_close) / previous_close
            if move > gap_threshold:
                issues.append(ValidationIssue.UNEXPLAINED_PRICE_JUMP)

        # A bar with impossible prices must not seed the gap check for
        # the next one, or one bad row flags its neighbour too.
        if not any(issue in FATAL_ISSUES for issue in issues):
            previous_close = bar.close_raw

        if issues:
            flagged = FlaggedBar(bar=bar, issues=tuple(issues))
            result.flagged.append(flagged)
            if not flagged.is_fatal:
                result.accepted.append(bar)
        else:
            result.accepted.append(bar)

    return result
