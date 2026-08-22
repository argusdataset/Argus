"""The refusal to emit a risk number, made checkable.

The Module 12 brief is explicit: "No scoring of any kind — this module
produces risk *inputs*, Module 13 combines them into `risk_score`." A
docstring saying so is worth little; the failure mode is a well-meaning
future edit adding a convenience `severity` or `total` property, which
every consumer would then read instead of the items.

So this scans the result objects' own surfaces for aggregate-shaped names
and for a float-valued whole-object summary. It is crude on purpose, in
the same way Module 10's threshold scan is crude: satisfiable only by not
doing the thing.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.risk_context.assessment import RiskContext
from core.risk_context.events import EventCoverage, PendingEventsView
from core.risk_context.flags import (
    EVENT_PROXIMITY,
    LIQUIDITY_DEGREE,
    VOLATILITY_SPIKE,
    RiskFlag,
)
from core.risk_context.invalidation import (
    EligibilityChange,
    EligibilityTrend,
    InvalidationSignals,
)

#: Names that would signal an aggregate. `degree` is deliberately absent:
#: it is one input's own reading in its own units, not a combination.
AGGREGATE_NAMES = frozenset(
    {
        "score",
        "risk_score",
        "total",
        "overall",
        "severity",
        "aggregate",
        "combined",
        "weighted",
        "summary_score",
        "rating",
        "level",
    }
)

RESULT_TYPES = (RiskContext, InvalidationSignals, PendingEventsView, RiskFlag)


def _public_surface(cls) -> set[str]:
    names = {f.name for f in fields(cls)}
    names |= {
        name
        for name, value in vars(cls).items()
        if not name.startswith("_") and isinstance(value, property)
    }
    return names


@pytest.mark.parametrize("cls", RESULT_TYPES, ids=lambda c: c.__name__)
def test_no_result_object_exposes_an_aggregate_number(cls):
    offenders = _public_surface(cls) & AGGREGATE_NAMES
    assert not offenders, (
        f"{cls.__name__} exposes {sorted(offenders)}. Module 12 emits itemized "
        "inputs; combining them into risk_score is Module 13's job."
    )


def _context() -> RiskContext:
    security_id = uuid4()
    as_of = datetime(2024, 6, 3, tzinfo=UTC)
    return RiskContext(
        security_id=security_id,
        as_of=as_of,
        flags={
            LIQUIDITY_DEGREE: RiskFlag(name=LIQUIDITY_DEGREE, raised=True, degree=0.9),
            VOLATILITY_SPIKE: RiskFlag(name=VOLATILITY_SPIKE, raised=False, degree=1.0),
            EVENT_PROXIMITY: RiskFlag(name=EVENT_PROXIMITY, raised=True, degree=3.0),
        },
        events=PendingEventsView(
            security_id=security_id,
            as_of=as_of,
            horizon_days=90.0,
            coverage=EventCoverage.NONE_SCHEDULED,
        ),
        invalidation=InvalidationSignals(
            security_id=security_id,
            as_of=as_of,
            current_state=None,
            backward_transitions=0,
            last_backward_at=None,
            last_transition=None,
            transitions_observed=0,
            eligibility=EligibilityChange(trend=EligibilityTrend.NEVER_EVALUATED),
        ),
        config_version="test",
    )


def test_the_serialized_form_carries_no_aggregate_either():
    """A number added only to `as_dict()` would evade the field scan and
    still be the thing everyone downstream read."""
    payload = _context().as_dict()

    assert not set(payload) & AGGREGATE_NAMES
    top_level_numbers = {key for key, value in payload.items() if isinstance(value, int | float)}
    assert not top_level_numbers, f"unexpected top-level number(s): {top_level_numbers}"


def test_the_result_reports_which_flags_fired_by_name_not_by_count():
    """Names, so an explanation can cite them. A count would already be
    the beginning of a score."""
    context = _context()

    assert set(context.raised_flags()) == {LIQUIDITY_DEGREE, EVENT_PROXIMITY}
    assert context.undetermined_flags() == ()


def test_undetermined_flags_are_absent_from_raised_rather_than_counted_as_safe():
    context = _context()
    context.flags[VOLATILITY_SPIKE] = RiskFlag(name=VOLATILITY_SPIKE, raised=None)

    assert VOLATILITY_SPIKE not in context.raised_flags()
    assert VOLATILITY_SPIKE in context.undetermined_flags()
