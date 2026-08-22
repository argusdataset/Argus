"""AsOfResult: no implicit 'current' fallback."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.data_validation.result import AsOfResult, MissReason

AS_OF = datetime(2024, 1, 1, tzinfo=UTC)


def test_hit_is_truthy_and_unwraps():
    result = AsOfResult.hit("value", as_of=AS_OF)
    assert bool(result) is True
    assert result.unwrap() == "value"


def test_miss_is_falsy():
    result = AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=AS_OF)
    assert bool(result) is False
    assert result.value is None


def test_miss_carries_an_explicit_reason_rather_than_bare_none():
    """The whole point: 'not available' must be checkable, not guessed at."""
    result = AsOfResult.miss(MissReason.OUTSIDE_INTERVAL, as_of=AS_OF)
    assert result.reason is MissReason.OUTSIDE_INTERVAL


def test_unwrap_on_a_miss_raises_rather_than_returning_none():
    result = AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=AS_OF)
    with pytest.raises(LookupError, match="not_yet_available"):
        result.unwrap()


def test_unwrap_or_returns_the_default_on_a_miss():
    result = AsOfResult.miss(MissReason.NOT_YET_AVAILABLE, as_of=AS_OF)
    assert result.unwrap_or("fallback") == "fallback"


def test_unwrap_or_returns_the_value_on_a_hit():
    result = AsOfResult.hit("value", as_of=AS_OF)
    assert result.unwrap_or("fallback") == "value"
