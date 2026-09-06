"""Building a universe version, and the two ways it can look like it worked.

`ARGUS_UNIVERSE_VERSION` gates both deployable jobs, and nothing outside
the test suite ever built one. So the first assertion here is simply that
a version exists afterwards and its membership is in the database.

The other two are the ways this could go wrong quietly, which is what the
entrypoint exists to prevent:

**The transaction.** `core/universe/README.md`'s example never committed,
and under SQLAlchemy 2.0 a connection that closes without `commit()`
rolls back. Following it would fetch ten thousand tickers, register their
identities, write the version and its membership, and discard all of it —
with a log that says success. The test reads the rows back through a
*separate* connection, which is the only way to tell a commit from a
convincing in-transaction read.

**The timing trap.** FMP's stock-list carries no IPO date, so a security
with no price history is dated from the moment ARGUS first saw it. The
ingestion reads members as of the last completed session, which can be
earlier — and then finds nothing, logs `universe_size: 0`, and exits
healthy. The entrypoint slices its own intervals at the instant the next
ingestion will use and refuses with exit 1 rather than letting that be
discovered from an ingestion log.

The provider is a fake for the usual reason: whether `fetch_stock_list`
parses FMP's JSON is Module 04's business and Module 04 tests it. What is
under test here is what this process does with what comes back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select

from data.provider_adapters.fmp.models import DelistedSecurity, FetchProvenance, SecurityListing
from infra.db.schema.identity import universe_membership, universe_version
from infra.deploy.migrate import upgrade_to_head
from infra.deploy.universe import build_universe_version

#: The next ingestion's cutoff is `session_close(scan_date) + 17h`, and
#: `scan_date` is the last session already due. For a build on Wednesday
#: the next run covers Tuesday's session, whose cutoff falls at 14:00 UTC
#: on Wednesday — so the build instant either precedes that cutoff or
#: does not, and that is the whole of the timing question.
#:
#: Verified against `scan_date_for`/`as_of_for` rather than reasoned
#: about: for both instants below, `scan_date` is 2026-03-03 and the
#: cutoff is 2026-03-04 14:00 UTC.
BEFORE_THE_CUTOFF = datetime(2026, 3, 4, 14, 0, tzinfo=UTC)
AFTER_THE_CUTOFF = datetime(2026, 3, 4, 23, 0, tzinfo=UTC)


def _provenance(endpoint: str) -> FetchProvenance:
    return FetchProvenance(
        endpoint=endpoint, url_path=f"/stable/{endpoint}", fetched_at=BEFORE_THE_CUTOFF
    )


class FakeListingSource:
    """Module 04's two universe endpoints, and nothing else.

    `FmpFetcher` has thirty-odd methods; the builder calls two of them.
    Standing in for only those keeps the double honest about what this
    path actually depends on.
    """

    def __init__(self, *, listed: tuple[str, ...], delisted: tuple[str, ...] = ()) -> None:
        self.listed = listed
        self.delisted = delisted
        self.calls: list[str] = []

    async def fetch_stock_list(self):
        self.calls.append("stock_list")
        return _Result(
            [
                SecurityListing(
                    provenance=_provenance("stock_list"),
                    symbol=symbol,
                    name=f"{symbol} Corp.",
                    exchange="NASDAQ Global Select",
                    exchange_short_name="NASDAQ",
                    security_type="stock",
                )
                for symbol in self.listed
            ]
        )

    async def fetch_delisted_companies(self):
        self.calls.append("delisted_companies")
        return _Result(
            [
                DelistedSecurity(
                    provenance=_provenance("delisted_companies"),
                    symbol=symbol,
                    company_name=f"{symbol} Corp.",
                    exchange="NASDAQ",
                    delisted_date=datetime(2025, 6, 30, tzinfo=UTC).date(),
                )
                for symbol in self.delisted
            ]
        )


class _Result:
    def __init__(self, records: list) -> None:
        self.records = records
        self.empty_reason = None


@pytest.fixture
def at_head(fresh_engine: Engine, alembic_target) -> Engine:
    upgrade_to_head(fresh_engine)
    return fresh_engine


def _build(engine: Engine, source: FakeListingSource, *, now: datetime, monkeypatch):
    """Run the entrypoint's own function with the fake provider.

    The client and fetcher are patched at the point `universe.py` uses
    them, so everything else in that function — the two transactions, the
    bar-bounds read, the interval slice — is the real code path.
    """
    import contextlib

    import infra.deploy.universe as module

    @contextlib.asynccontextmanager
    async def _client():
        yield object()

    monkeypatch.setattr(module, "FmpClient", lambda *a, **k: _client())
    monkeypatch.setattr(module, "FmpFetcher", lambda _client: source)

    from infra.deploy.config import PROFILES
    from packages.config.environment import Environment

    return asyncio.run(
        build_universe_version(engine, now=now, profile=PROFILES[Environment.DEVELOPMENT])
    )


def _committed_counts(engine: Engine) -> tuple[int, int]:
    """Versions and memberships, read on a connection of their own.

    The separate connection is the assertion. Reading inside the
    building transaction would show the same rows whether or not they
    were ever committed, which is exactly the failure the README example
    had.
    """
    with engine.connect() as connection:
        versions = connection.execute(
            select(func.count()).select_from(universe_version)
        ).scalar_one()
        members = connection.execute(
            select(func.count()).select_from(universe_membership)
        ).scalar_one()
    return versions, members


def test_a_build_writes_a_version_and_its_membership(at_head: Engine, monkeypatch):
    """The thing that could not be done before this module existed."""
    source = FakeListingSource(listed=("AAA", "BBB", "CCC"))

    result = _build(at_head, source, now=AFTER_THE_CUTOFF, monkeypatch=monkeypatch)

    assert source.calls == ["stock_list", "delisted_companies"]
    assert result.version.member_count == 3

    versions, members = _committed_counts(at_head)
    assert versions == 1
    assert members == 3


def test_the_work_survives_the_transaction_closing(at_head: Engine, monkeypatch):
    """The README example's bug, asserted rather than assumed.

    A connection that closes without committing rolls back, silently. The
    counts above are read on a fresh connection after the function
    returned, so a missing `commit()` shows up here as zero rows and a
    successful-looking return value.
    """
    _build(
        at_head,
        FakeListingSource(listed=("AAA", "BBB")),
        now=AFTER_THE_CUTOFF,
        monkeypatch=monkeypatch,
    )

    versions, members = _committed_counts(at_head)
    assert (versions, members) == (1, 2)


def test_the_label_is_reported_so_it_can_be_set_as_an_environment_variable(
    at_head: Engine, monkeypatch
):
    """The whole point of running this: one string to paste."""
    result = _build(
        at_head,
        FakeListingSource(listed=("AAA",)),
        now=AFTER_THE_CUTOFF,
        monkeypatch=monkeypatch,
    )

    assert result.version.version_label
    assert result.as_dict()["universe_version_label"] == result.version.version_label
    assert result.as_dict()["universe_version_id"] == str(result.version.id)


def test_a_second_identical_build_reuses_the_version_rather_than_duplicating_it(
    at_head: Engine, monkeypatch
):
    """Re-running is safe, which is what makes this a command not a cron.

    Versions are immutable and append-only, so a second copy of the same
    membership would only make the history harder to read.
    """
    source = FakeListingSource(listed=("AAA", "BBB"))
    first = _build(at_head, source, now=AFTER_THE_CUTOFF, monkeypatch=monkeypatch)
    second = _build(at_head, source, now=AFTER_THE_CUTOFF, monkeypatch=monkeypatch)

    assert second.version.id == first.version.id
    assert second.version.reused is True
    versions, _members = _committed_counts(at_head)
    assert versions == 1


def test_a_build_after_the_next_cutoff_reports_itself_unusable(at_head: Engine, monkeypatch):
    """The timing trap, caught here instead of in an ingestion log.

    With no price history every interval starts at the build instant.
    The next ingestion covers a session whose cutoff has already passed,
    so it reads members as of an instant *before* any interval begins —
    finds nobody, logs `universe_size: 0`, and exits healthy.

    This is the common case rather than the exotic one, which is worth
    saying plainly: the cutoff for the session already due passes at
    14:00 UTC, so any build later in the working day lands here.
    """
    result = _build(
        at_head,
        FakeListingSource(listed=("AAA", "BBB")),
        now=AFTER_THE_CUTOFF,
        monkeypatch=monkeypatch,
    )

    # The version itself is real and correct — it is the *next run* that
    # would be empty.
    assert result.version.member_count == 2
    assert result.members_for_next_ingestion == 0
    assert result.usable is False
    assert result.next_ingestion_as_of < AFTER_THE_CUTOFF


def test_a_build_before_the_next_cutoff_is_usable(at_head: Engine, monkeypatch):
    """The same build nine hours earlier, and the trap is not there.

    The next ingestion's cutoff has not passed, so it reads members at an
    instant at or after the build and sees all of them. Asserted
    alongside the case above because "unusable" only means something if
    the usable case is reachable.
    """
    result = _build(
        at_head,
        FakeListingSource(listed=("AAA", "BBB")),
        now=BEFORE_THE_CUTOFF,
        monkeypatch=monkeypatch,
    )

    assert result.members_for_next_ingestion == 2
    assert result.usable is True


def test_delisted_securities_are_still_members_of_an_earlier_universe(at_head: Engine, monkeypatch):
    """Survivorship bias, which is the whole reason Module 06 exists.

    A security delisted in June 2025 is not in today's universe and must
    still be in one built as of March 2025 — otherwise every backtest
    looks better than reality and nothing fails to say so.
    """
    source = FakeListingSource(listed=("AAA",), delisted=("GONE",))

    result = _build(at_head, source, now=AFTER_THE_CUTOFF, monkeypatch=monkeypatch)

    symbols = {interval.symbol for interval in result.construction.intervals}
    assert "GONE" in symbols
    # And it is excluded from the current version, which is the other half
    # of the same property.
    assert result.version.member_count == 1
