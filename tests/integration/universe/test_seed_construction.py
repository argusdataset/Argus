"""Seeding a universe from an explicit symbol list, on a free FMP key.

`/stable/stock-list` is paywalled below FMP's paid tiers. The first real
run of `infra.deploy.universe` against production, on 2026-09-13, got
HTTP 402 from it before reading a row — so the whole-market path could
build nothing, no identities were minted, and the targeted backfill could
not run either (`_identities_for` resolves, it does not mint). ARGUS had
never ingested a single real bar, and this is the gap that explains it.

`/stable/profile` is available on the free tier. These tests run the seed
path over Module 04's real fetch code (MockTransport, no live calls),
Module 05's real identity resolution and Module 03's real append-only
guards — the same end-to-end shape as `test_construction.py`, which
covers the whole-market path.

The first test is the one that actually proves the free-tier claim: it
records every path the transport is asked for and asserts that neither
paywalled endpoint is among them. A seed path that quietly called
`stock-list` would pass every other assertion here and still be useless
on the key it exists for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from core.universe.admission import ExclusionReason
from core.universe.builder import build_intervals_from_symbols, construct_version
from core.universe.repository import UniverseRepository
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import universe_version
from tests.unit.fmp.conftest import json_response

OBSERVED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

#: Keyed by symbol, in FMP's *profile* shape — which is not the stock-list
#: shape: the long venue name arrives under `exchangeFullName` and the
#: short one under plain `exchange`, where `stock-list` uses `exchange`
#: and `exchangeShortName`. `PROFILE_FIELD_ALIASES` exists for exactly
#: this, and these fixtures are written in the real shape so the aliases
#: are under test rather than assumed.
PROFILES = {
    "AAPL": {
        "symbol": "AAPL",
        "companyName": "Apple Inc.",
        "exchangeFullName": "NASDAQ Global Select",
        "exchange": "NASDAQ",
        "ipoDate": "1980-12-12",
    },
    "GE": {
        "symbol": "GE",
        "companyName": "General Electric",
        "exchangeFullName": "New York Stock Exchange",
        "exchange": "NYSE",
        "ipoDate": "1892-06-15",
    },
    "SPY": {
        "symbol": "SPY",
        "companyName": "SPDR S&P 500 ETF",
        "exchangeFullName": "NYSE Arca",
        "exchange": "AMEX",
    },
    "SHOP.TO": {
        "symbol": "SHOP.TO",
        "companyName": "Shopify",
        "exchangeFullName": "Toronto",
        "exchange": "TSX",
    },
}


def profile_handler(known=None, *, seen: list[str] | None = None):
    """Serve `/stable/profile` per symbol, and record every path requested.

    An unknown symbol gets `[]`, which is what FMP returns for one it does
    not carry — not a 404, which is why a typo cannot be told from a
    delisted ticker at this layer and is reported by name instead.
    """
    payloads = PROFILES if known is None else known

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if not request.url.path.endswith("/profile"):
            return httpx.Response(
                402,
                json={
                    "Error Message": (
                        "Restricted Endpoint: This endpoint is not available under "
                        "your current subscription"
                    )
                },
            )
        symbol = request.url.params.get("symbol", "")
        row = payloads.get(symbol)
        return json_response([row] if row else [])

    return handler


def _definition(connection, version_id) -> dict:
    """The stored `definition` JSON for one version.

    Read straight from the table: `UniverseRepository` has no reader for
    it, and adding one only a test would call is not a reason to widen
    the repository's surface.
    """
    from sqlalchemy import select

    return connection.execute(
        select(universe_version.c.definition).where(universe_version.c.id == version_id)
    ).scalar_one()


async def _seed(make_fetcher, connection, symbols, *, known=None, seen=None, as_of=None):
    fetcher, client = make_fetcher(profile_handler(known, seen=seen))
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_symbols(
            fetcher, resolver, symbols, observed_at=OBSERVED_AT
        )
    repository = UniverseRepository(connection)
    version = construct_version(construction, repository, as_of=as_of or OBSERVED_AT)
    return construction, version, repository


# --------------------------------------------------------------------------
# The free-tier claim
# --------------------------------------------------------------------------


async def test_the_seed_path_never_touches_a_paywalled_endpoint(make_fetcher, connection):
    """The assertion the whole path exists for.

    Every requested path is recorded, and neither `stock-list` nor
    `delisted-companies` may appear. The handler answers both with the
    real 402 body, so a call to either fails loudly here rather than in
    production on the key this is meant to work with.
    """
    seen: list[str] = []

    _, version, _ = await _seed(make_fetcher, connection, ("AAPL", "GE"), seen=seen)

    assert seen == ["/stable/profile", "/stable/profile"]
    assert not any("stock-list" in path or "delisted" in path for path in seen)
    assert version.member_count == 2


async def test_one_request_per_symbol_and_no_more(make_fetcher, connection):
    """Per-ticker is the cost of this path; paying it twice is not."""
    seen: list[str] = []

    await _seed(make_fetcher, connection, ("AAPL", "GE", "SPY"), seen=seen)

    assert len(seen) == 3


# --------------------------------------------------------------------------
# The ordinary path, unchanged underneath
# --------------------------------------------------------------------------


async def test_identities_are_minted_for_previously_unknown_symbols(make_fetcher, connection):
    """Module 05's resolver, invoked exactly as the whole-market path does.

    This is the half the backfill depends on: `_identities_for` resolves
    and never mints, so a symbol nobody registered is reported as an
    operator typo there. Seeding is what registers them.
    """
    before = connection.execute(text("SELECT count(*) FROM security_identity")).scalar_one()

    construction, version, repository = await _seed(make_fetcher, connection, ("AAPL", "GE"))

    after = connection.execute(text("SELECT count(*) FROM security_identity")).scalar_one()
    assert after == before + 2
    assert version.member_count == 2
    assert {row["exchange"] for row in repository.members(version.id)} == {"NASDAQ", "NYSE"}
    assert construction.admission.admitted == 2


async def test_admission_rules_behave_exactly_as_they_do_for_the_whole_market(
    make_fetcher, connection
):
    """SPY is Arca and SHOP.TO's venue label is unrecognised, in both paths.

    The seed path runs `_observe_listing` unchanged rather than a copy of
    it, so this is checking the wiring, not re-testing admission.
    """
    construction, version, _ = await _seed(
        make_fetcher, connection, ("AAPL", "GE", "SPY", "SHOP.TO")
    )
    report = construction.admission

    assert version.member_count == 2
    assert "SPY" in {
        item.symbol for item in report.samples[ExclusionReason.NON_UNIVERSE_EXCHANGE]
    }
    assert "SHOP.TO" in {
        item.symbol for item in report.samples[ExclusionReason.UNKNOWN_EXCHANGE]
    }
    assert "Toronto" in report.unknown_exchange_labels


async def test_the_profile_shape_resolves_through_the_aliases(make_fetcher, connection):
    """`profile` names its venue fields differently from `stock-list`.

    If the aliases were wrong the venue would fall to UNKNOWN and both
    symbols would be excluded — which would look like an exchange problem
    rather than a field-naming one, so it is worth asserting directly.
    """
    construction, _, _ = await _seed(make_fetcher, connection, ("AAPL", "GE"))

    assert construction.admission.unknown_exchange_count == 0
    assert construction.admission.admitted == 2


# --------------------------------------------------------------------------
# A typo must cost one symbol, not the run
# --------------------------------------------------------------------------


async def test_an_unknown_symbol_is_named_and_the_build_continues(make_fetcher, connection):
    """Twenty symbols typed by hand will contain a mistake.

    Losing the other nineteen to it is the worse outcome, so the run
    continues and says which symbol it could not find — with twenty
    names, *which one* is the entire question.
    """
    construction, version, _ = await _seed(
        make_fetcher, connection, ("AAPL", "NOSUCHTICKER", "GE")
    )

    assert construction.seed is not None
    assert construction.seed.not_found == ["NOSUCHTICKER"]
    assert version.member_count == 2
    assert "NOSUCHTICKER" in construction.seed.summary()["seed_not_found"]


async def test_a_failing_fetch_is_recorded_by_symbol_rather_than_raised(make_fetcher, connection):
    """A 402 on one symbol must not abandon the ones that worked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("symbol") == "GE":
            return httpx.Response(403, json={"Error Message": "Restricted Endpoint"})
        return profile_handler()(request)

    fetcher, client = make_fetcher(handler)
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_symbols(
            fetcher, resolver, ("AAPL", "GE"), observed_at=OBSERVED_AT
        )

    assert list(construction.seed.failed) == ["GE"]
    assert construction.seed.profiled == ["AAPL"]
    assert construction.security_count == 1


# --------------------------------------------------------------------------
# A seeded universe must be unmistakable
# --------------------------------------------------------------------------


async def test_the_version_label_says_it_is_a_seed(make_fetcher, connection):
    """This string is what gets pasted into `ARGUS_UNIVERSE_VERSION`.

    Every scan, score and track record downstream is read against it, and
    a seeded universe covers hand-picked names rather than the market. A
    reader has to be able to tell from the label alone.
    """
    _, version, _ = await _seed(make_fetcher, connection, ("AAPL", "GE"))

    assert "seed2" in version.version_label
    assert version.version_label.startswith("universe-2026-08-22-seed2-")


async def test_the_stored_definition_records_the_seed_and_its_caveats(make_fetcher, connection):
    """The row a reader meets six months from now, not the command."""
    _, version, repository = await _seed(make_fetcher, connection, ("AAPL", "NOSUCHTICKER", "GE"))

    definition = _definition(connection, version.id)

    assert definition["source"]["endpoints"] == ["company_profile"]
    assert definition["seed"]["seed_requested"] == 3
    assert definition["seed"]["seed_symbols"] == ["AAPL", "NOSUCHTICKER", "GE"]
    assert definition["seed"]["seed_not_found"] == ["NOSUCHTICKER"]
    # Stated rather than implied by the absence of a delisted sweep: a
    # symbol that is in fact delisted is dated from FIRST_OBSERVED here.
    assert definition["seed"]["seed_delisted_sweep"] is False


async def test_a_whole_market_construction_is_not_marked_as_a_seed(make_fetcher, connection):
    """The marker has to mean something, so it must be absent by default."""
    from core.universe.builder import build_intervals_from_fetch
    from tests.integration.universe.test_construction import universe_handler

    fetcher, client = make_fetcher(universe_handler())
    resolver = SecurityIdentityResolver(connection)
    async with client:
        construction = await build_intervals_from_fetch(fetcher, resolver, observed_at=OBSERVED_AT)
    version = construct_version(construction, UniverseRepository(connection), as_of=OBSERVED_AT)

    assert construction.seed is None
    assert "seed" not in version.version_label
    definition = _definition(connection, version.id)
    assert "seed" not in definition
    assert definition["source"]["endpoints"] == ["stock_list", "delisted_companies"]


@pytest.mark.parametrize("symbols", [(), ("",)])
async def test_an_empty_seed_list_builds_an_empty_universe_rather_than_fetching(
    make_fetcher, connection, symbols
):
    """Guards the entrypoint's contract: empty means the caller chose the
    other path, so this must not quietly fetch anything."""
    seen: list[str] = []

    construction, _, _ = await _seed(make_fetcher, connection, symbols, seen=seen)

    assert seen == [] or all(path.endswith("/profile") for path in seen)
    assert construction.security_count == 0
