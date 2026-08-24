"""The response contract, tested as a contract.

These assertions look pedantic until you remember who reads this API:
something that never saw the eighteen modules behind it. A field renamed
here is a broken client, and the whole reason these shapes exist in one
file is so that breaking one is a visible act rather than an accident.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from core.data_validation.result import MissReason
from services.terminal.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    TerminalConfig,
    TerminalLimits,
)
from services.terminal.datafeed import ADVERTISED_RESOLUTIONS, RESOLUTIONS, datafeed_config
from services.terminal.errors import TerminalError, error_payload, security_not_found
from services.terminal.schemas import (
    BarsResponse,
    CompanyProfile,
    FundamentalsResponse,
    ScanStatusResponse,
    Unavailable,
)

NOW = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)


def _profile() -> CompanyProfile:
    return CompanyProfile(security_id=uuid4(), ticker="MLSS", name="Test Co", as_of=NOW)


# --------------------------------------------------------------------------
# Absence says why
# --------------------------------------------------------------------------


def test_a_missing_block_is_present_and_explains_itself_rather_than_being_null():
    """Eighteen modules have enforced `None` is never `0.0`. This is that
    rule surviving contact with JSON, where `null` means everything."""
    response = FundamentalsResponse(
        security=_profile(),
        as_of=NOW,
        unavailable={
            "BALANCE_SHEET": Unavailable(
                reason=MissReason.NOT_YET_AVAILABLE.value, explanation="Not filed yet."
            )
        },
    )
    payload = response.model_dump(mode="json")

    assert payload["unavailable"]["BALANCE_SHEET"]["available"] is False
    assert payload["unavailable"]["BALANCE_SHEET"]["reason"] == MissReason.NOT_YET_AVAILABLE.value
    assert payload["statements"] == {}
    # And the key is not simply absent, which a consumer could not tell
    # from "you did not ask for it".
    assert "BALANCE_SHEET" in payload["unavailable"]


def test_the_unavailable_reason_is_a_real_miss_reason_not_free_text():
    """A consumer branching on the reason needs a closed set."""
    entry = Unavailable(reason=MissReason.NOT_YET_AVAILABLE.value, explanation="x")

    assert entry.reason in {reason.value for reason in MissReason}


def test_available_false_is_a_literal_and_cannot_be_set_true():
    """An `Unavailable` that could claim availability would be a shape
    that means two opposite things."""
    with pytest.raises(ValueError, match="available"):
        Unavailable(available=True, reason="X", explanation="y")


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_every_company_response_carries_identity_alongside_the_ticker():
    """Tickers are recycled; ARGUS's identity is not. A client storing
    `security_id` survives a rename and one storing the ticker does not."""
    payload = _profile().model_dump(mode="json")

    assert "security_id" in payload
    assert "ticker" in payload
    # RFC 3339 UTC. Pinned because a client parsing timestamps cares, and
    # because the shape of a date is exactly the kind of thing that
    # changes silently under a library upgrade.
    assert payload["as_of"] == "2026-03-03T21:00:00Z"
    assert datetime.fromisoformat(payload["as_of"]) == NOW


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_every_error_has_the_same_envelope():
    payload = error_payload("SOME_CODE", "Some message.", {"field": "x"})

    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "detail"}


def test_an_error_carries_a_machine_readable_code_not_just_a_status():
    """404 is 'no such ticker' and also 'ticker exists, nothing ingested'.
    A status alone cannot tell a UI which message to show."""
    error = security_not_found("ZZZZ")

    assert error.code == "SECURITY_NOT_FOUND"
    assert error.status == 404
    assert error.payload()["error"]["detail"]["ticker"] == "ZZZZ"


def test_detail_is_optional_and_a_consumer_reading_only_the_code_is_correct():
    payload = TerminalError("X", "y").payload()

    assert payload["error"]["detail"] == {}


# --------------------------------------------------------------------------
# Datafeed protocol shapes
# --------------------------------------------------------------------------


def test_the_bars_response_serializes_the_protocols_single_letter_names():
    """`l` is a Python builtin-adjacent name and `nextTime` is camelCase.
    Both are the library's contract; renaming them breaks the widget."""
    response = BarsResponse(s="ok", t=[1], o=[1.0], h=[2.0], l=[0.5], c=[1.5], v=[100.0])
    payload = response.model_dump(mode="json", by_alias=True)

    assert set(payload) >= {"s", "t", "o", "h", "l", "c", "v"}
    assert payload["l"] == [0.5]
    assert "low" not in payload


def test_a_no_data_response_is_a_status_not_an_error():
    """The protocol's own distinction. A chart paging back past a listing
    date must not render a failure."""
    response = BarsResponse(s="no_data", nextTime=1600000000)
    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["s"] == "no_data"
    assert payload["nextTime"] == 1600000000
    assert payload["t"] == []


def test_the_datafeed_advertises_only_what_argus_actually_has():
    """Marks are Module 21's overlays. A datafeed that claims to serve
    them and returns none makes the widget ask forever."""
    config = datafeed_config()

    assert config.supports_marks is False
    assert config.supports_timescale_marks is False
    assert config.supported_resolutions == list(ADVERTISED_RESOLUTIONS)
    # Nothing intraday: only daily bars are sourced from a provider.
    assert not any(res.endswith("S") or res.isdigit() for res in config.supported_resolutions)


def test_every_advertised_resolution_can_actually_be_resolved():
    """Advertising a resolution the mapping does not know would send the
    widget asking for something that silently becomes daily."""
    for resolution in ADVERTISED_RESOLUTIONS:
        assert resolution in RESOLUTIONS


# --------------------------------------------------------------------------
# Scan status
# --------------------------------------------------------------------------


def test_not_scanned_and_scanned_but_empty_are_different_answers():
    """Module 18's binding distinction. Today 'scanned, nothing scored' is
    the *normal* outcome, so flattening the two would report a working
    system as a broken one every day."""
    never = ScanStatusResponse(
        scan_date=date(2026, 3, 3), available=False, explanation="nobody looked"
    )
    empty = ScanStatusResponse(
        scan_date=date(2026, 3, 3),
        available=True,
        status="COMPLETED",
        scored_signals=0,
        explanation="ran, found nothing",
    )

    assert never.available is not empty.available
    assert never.model_dump() != empty.model_dump()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_every_limit_declares_a_kind_and_a_rationale():
    for name, entry in TerminalLimits().describe().items():
        assert entry["kind"] in KINDS, name
        assert entry["rationale"].strip(), f"{name} has no stated reasoning"


def test_the_two_real_product_limits_are_the_calibratable_ones():
    """Everything else bounds how much of an answer is returned, never
    what the answer is. If a future limit is tagged calibratable without
    that property, this stops matching and says so."""
    assert set(TerminalLimits().calibratable()) == {
        "max_watchlists_per_user",
        "max_watchlist_items",
    }


def test_page_sizes_are_operational_because_they_cannot_change_an_answer():
    described = TerminalLimits().describe()

    for name in ("default_news_limit", "max_news_limit", "max_bars_per_request"):
        assert described[name]["kind"] == OPERATIONAL


def test_a_requested_page_size_is_clamped_rather_than_refused():
    limits = TerminalLimits()

    assert limits.bounded_news_limit(None) == int(limits.default_news_limit)
    assert limits.bounded_news_limit(10_000) == int(limits.max_news_limit)
    assert limits.bounded_news_limit(0) == 1


def test_changing_a_limit_changes_the_configuration_checksum():
    baseline = TerminalConfig()
    changed = TerminalConfig(stub_identity_enabled=False)

    assert baseline.content_checksum() != changed.content_checksum()
    assert CALIBRATABLE in {entry["kind"] for entry in TerminalLimits().describe().values()}
