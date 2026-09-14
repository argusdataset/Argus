"""Building a universe version. The one thing nothing else could do.

## Why this file exists

`ARGUS_UNIVERSE_VERSION` gates both deployable jobs: `ingestion.py` and
`scanner.py` resolve it through `resolve_universe_version`, and both exit
2 without it. Module 06 has the code to create one —
`build_intervals_from_fetch` and `construct_version`, both complete and
both tested — and **nothing called either of them outside the test
suite**. Twelve entrypoints exist under `infra/deploy/`; none built a
universe.

It is G2's shape one level up: the pipe was laid and no water went in.
There, the consequence was wrong data; here it is no data at all, and no
way to start.

## Run it by hand, once, then set the variable

This is not a cron. A universe version is a durable artefact and building
a second identical one is a no-op by design (`construct_version` reuses an
equivalent version rather than duplicating it), but a *schedule* would
imply the version rotates on its own, and the two jobs that read it would
then be pointed at whatever the last run produced — the "most recent
version wins" ordering hazard Module 23 catalogued and this project has
refused four times.

So: run it, read the label out of the log, set it in the platform.

```
railway run --service ingestion python -m infra.deploy.universe
```

The label is logged under `universe_version_label`, and printed to stdout
on its own line so it can be copied without reading JSON.

## The transaction is committed, and that is not a detail

`core/universe/README.md`'s example never committed. Under SQLAlchemy 2.0
a `Connection` that closes without `commit()` rolls back, so following it
would fetch ten thousand tickers, register their identities, write the
version and its membership, and then silently discard all of it — with a
successful-looking log. The example is corrected in the same change.

## The timing trap, and why this refuses rather than warns

FMP's `stock-list` carries no IPO date. So for a security with no price
history yet, `build_intervals` can only claim it was listed from the
moment ARGUS first saw it (`IntervalEvidence.FIRST_OBSERVED`) — erring
narrow on purpose, because claiming an earlier listing ARGUS cannot
evidence is the survivorship-bias lie Module 06 exists to prevent.

Meanwhile the ingestion reads its members at `as_of_for(trading_date)`,
where `trading_date` is the session already due — and that cutoff is the
session close plus seventeen hours, which lands at **14:00 UTC**. So a
universe built at any point after 14:00 UTC is dated *later* than the
instant the next run will ask about, and that run finds **zero members**,
logs `universe_size: 0` and "no members listed on this date", and exits
healthy.

That is the common case, not an edge one: a build during a European or
American working afternoon lands there. Which is why this refuses rather
than warning. A silently empty ingestion that reports success is the
worst available outcome, so the entrypoint slices its own intervals at
the instant the next ingestion will actually use, counts what it finds,
and exits 1 with the dates spelled out if the answer is nothing.

Two ways out, both stated in the failure message: build before 14:00 UTC,
or ingest price history first — which dates the intervals from real bars
instead of from the build instant, and is what
`core/universe/README.md` means by running the backfill first.

## Seeding a small universe on a free FMP key

`/stable/stock-list` is paywalled below FMP's paid tiers. The first real
attempt at this entrypoint, on 2026-09-13, got HTTP 402 on that endpoint
before it had read a single row — so on a free key the whole-market path
cannot build anything, and nothing downstream of it has ever run.

`/stable/profile` is available on the free tier, so one symbol at a time
works where the whole market does not:

    ARGUS_UNIVERSE_SYMBOLS=MLSS,SLS,HIVE,ALXO,QBTS,AAPL,MSFT

Unset, this file behaves exactly as it did before the variable existed.
Set, it takes `build_intervals_from_symbols` instead — a per-ticker path
that exists for testing and says so in the version label it produces
(`universe-<date>-seed7-<checksum>`), in the stored definition, and in
every line it logs. **A seeded universe is a test fixture, not a
universe**: it covers the names that were typed in, so a statistic
computed over one describes those names and not the market.

Everything else is unchanged and deliberately so — the 14:00 UTC timing
check applies to a seeded build exactly as it does to a real one. A
seeded universe the next ingestion cannot see is just as useless as a
real one nobody can see, and it fails the same way rather than being
special-cased into looking healthy.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime

from sqlalchemy import Engine

from core.live_scanner.schedule import as_of_for, scan_date_for
from core.universe import (
    UniverseConstruction,
    UniverseRepository,
    bar_date_bounds,
    build_intervals_from_fetch,
    build_intervals_from_symbols,
    construct_version,
    intervals_covering,
)
from data.normalization.identity import SecurityIdentityResolver
from data.provider_adapters.fmp.client import FmpClient
from data.provider_adapters.fmp.fetchers import FmpFetcher
from infra.db.connection import create_db_engine
from infra.deploy.cli import refuse_arguments
from infra.deploy.config import DeploymentProfile, profile_for
from infra.observability.logging import configure_logging, get_logger

__all__ = [
    "SEED_SYMBOLS_ENV_VAR",
    "UniverseBuildResult",
    "build_universe_version",
    "main",
    "seed_symbols_from_environment",
]

_log = get_logger("argus.deploy.universe")

#: Optional. Set it to seed a small universe from an explicit symbol
#: list; unset, the whole-market path runs unchanged. Named and read the
#: way `infra/deploy/backfill.py` reads `ARGUS_BACKFILL_SYMBOLS`, since an
#: operator who has met one should not have to learn a second convention.
SEED_SYMBOLS_ENV_VAR = "ARGUS_UNIVERSE_SYMBOLS"


def seed_symbols_from_environment(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """The seed symbol list, or empty for the whole-market path.

    Empty is the default and means "behave exactly as before", so an
    unset variable, an empty one, and one holding nothing but separators
    are all the same answer. Duplicates are collapsed — asking for AAPL
    twice is a typo, and paying for the request twice to build the same
    interval twice serves nobody — while the operator's order is kept, so
    the log reads back in the order it was typed.
    """
    source = env if env is not None else dict(os.environ)
    seen: dict[str, None] = {}
    for part in source.get(SEED_SYMBOLS_ENV_VAR, "").split(","):
        symbol = part.strip().upper()
        if symbol:
            seen.setdefault(symbol, None)
    return tuple(seen)


class UniverseBuildResult:
    """What one build produced, and whether it is usable yet.

    A class rather than a tuple because `usable_from` is the field an
    operator actually needs and it deserves a name — the version id is
    what they copy, but the date is what tells them whether tonight's
    ingestion will see anything.
    """

    __slots__ = ("version", "construction", "next_ingestion_as_of", "members_for_next_ingestion")

    def __init__(
        self,
        *,
        version: object,
        construction: UniverseConstruction,
        next_ingestion_as_of: datetime | None,
        members_for_next_ingestion: int,
    ) -> None:
        self.version = version
        self.construction = construction
        self.next_ingestion_as_of = next_ingestion_as_of
        self.members_for_next_ingestion = members_for_next_ingestion

    @property
    def usable(self) -> bool:
        """Whether the next scheduled ingestion will find any members.

        False is not a build failure — the version is written and is
        correct. It means the *next* run would be a no-op, which is the
        thing that must never be silent.
        """
        return self.members_for_next_ingestion > 0

    def as_dict(self) -> dict[str, object]:
        version = self.version
        return {
            "universe_version_id": str(getattr(version, "id", "")),
            "universe_version_label": getattr(version, "version_label", None),
            "as_of": getattr(version, "as_of_date", None),
            "member_count": getattr(version, "member_count", 0),
            "reused_existing_version": getattr(version, "reused", False),
            "intervals_built": len(self.construction.intervals),
            "next_ingestion_as_of": (
                self.next_ingestion_as_of.isoformat() if self.next_ingestion_as_of else None
            ),
            "members_for_next_ingestion": self.members_for_next_ingestion,
            "usable": self.usable,
            **self.construction.admission.summary(),
            # Only on a seeded build, and then in every line: which
            # symbols were asked for, which the provider did not know,
            # and that no delisted sweep ran. A seeded universe that
            # logged like a real one is the confusion this prevents.
            **(
                self.construction.seed.summary() if self.construction.seed is not None else {}
            ),
        }


async def build_universe_version(
    engine: Engine,
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
    description: str | None = None,
    profile: DeploymentProfile | None = None,
    seed_symbols: Sequence[str] | None = None,
) -> UniverseBuildResult:
    """Fetch listings, build intervals, and persist one universe version.

    Two transactions, deliberately. The first registers identities during
    the fetch — Module 05's resolver mints them for admitted symbols —
    and the second writes the version. Holding one across a fetch of ten
    thousand tickers would keep a connection idle for minutes and would
    put identity registration and version creation in the same
    all-or-nothing unit, so a single unparseable listing would discard
    the entire universe.

    `as_of` defaults to the observation instant, which is what a first
    build wants. A historical universe is the same call with an earlier
    date and is only meaningful once price history exists — see
    `core/universe/README.md` on why a cold system cannot honestly claim
    a security was listed before it first saw it.

    `seed_symbols`, when non-empty, takes the per-ticker seed path
    instead — see the module docstring on why that exists and what it
    costs. Empty or None is the whole-market path, unchanged.
    """
    (profile or profile_for()).validate()
    observed_at = now or datetime.now(UTC)
    seed = tuple(seed_symbols or ())

    with engine.begin() as connection:
        # Price history where there is any. Without it every currently
        # listed security's interval starts at the observation instant,
        # which is exactly the timing trap this module's docstring
        # describes — so this is not an optimisation, it is what makes a
        # historical `as_of` possible at all.
        first_bars, last_bars = bar_date_bounds(connection)

    _log.info(
        "universe build starting",
        extra={
            "event": "universe_build_starting",
            "observed_at": observed_at.isoformat(),
            "securities_with_price_history": len(first_bars),
            "seed_symbols": list(seed),
            "whole_market": not seed,
        },
    )

    async with FmpClient() as client:
        with engine.begin() as connection:
            fetcher = FmpFetcher(client)
            resolver = SecurityIdentityResolver(connection)
            construction = (
                await build_intervals_from_symbols(
                    fetcher,
                    resolver,
                    seed,
                    observed_at=observed_at,
                    first_bar_dates=first_bars,
                    last_bar_dates=last_bars,
                )
                if seed
                else await build_intervals_from_fetch(
                    fetcher,
                    resolver,
                    observed_at=observed_at,
                    first_bar_dates=first_bars,
                    last_bar_dates=last_bars,
                )
            )

    with engine.begin() as connection:
        version = construct_version(
            construction,
            UniverseRepository(connection),
            as_of=as_of or observed_at,
            description=description or _default_description(construction),
        )

    next_as_of = _next_ingestion_as_of(observed_at)
    covering = len(intervals_covering(construction.intervals, next_as_of)) if next_as_of else 0

    return UniverseBuildResult(
        version=version,
        construction=construction,
        next_ingestion_as_of=next_as_of,
        members_for_next_ingestion=covering,
    )


def _default_description(construction: UniverseConstruction) -> str:
    """What the version says about itself when nobody supplied a description.

    The seed case spells out that this is a test fixture, in the field a
    person reads when they are deciding whether to trust a number
    computed over it.
    """
    seed = construction.seed
    if seed is None:
        return "Built by infra.deploy.universe"
    return (
        f"SEED universe built by infra.deploy.universe from {len(seed.requested)} "
        "explicit symbols, one /stable/profile call each. A test fixture, not the "
        "market: no stock-list sweep, no delisted sweep, so every figure derived "
        "from it describes these symbols only."
    )


def _next_ingestion_as_of(now: datetime) -> datetime | None:
    """The cutoff the next scheduled ingestion will read members at.

    Derived from the scanner's own schedule rather than restated, so the
    two cannot disagree about which session is current. None when no
    session is due yet — a Saturday build, say — in which case there is
    nothing to check against and the caller says so rather than guessing.
    """
    trading_date: date | None = scan_date_for(now)
    return as_of_for(trading_date) if trading_date is not None else None


def main(argv: list[str] | None = None) -> int:
    """`python -m infra.deploy.universe` — build one version, print its label.

    Exit codes match the other entrypoints:

    - `0` a version exists and the next ingestion will find members in it.
    - `1` the version was built but the next ingestion would see zero
      members — see the module docstring on the timing trap. The version
      is real and keeping it is fine; what is not fine is discovering
      this from an ingestion log that says `universe_size: 0`.
    - `2` a prerequisite is missing and nothing was built.
    """
    refuse_arguments("infra.deploy.universe", argv)
    configure_logging()
    profile = profile_for()
    # Configuration comes from the environment, not argv — `refuse_arguments`
    # stays, and this reads the same way `backfill.py` reads its own.
    seed_symbols = seed_symbols_from_environment()

    try:
        engine = create_db_engine(pool_pre_ping=True, pool_size=5, max_overflow=0)
        result = asyncio.run(
            build_universe_version(engine, profile=profile, seed_symbols=seed_symbols)
        )
    except Exception as error:  # noqa: BLE001 - the exit code is the signal
        _log.error(
            "universe build failed",
            extra={
                "event": "universe_build_failed",
                "error_type": type(error).__name__,
                "detail": str(error),
            },
        )
        return 2

    _log.info(
        "universe build finished", extra={"event": "universe_build_finished", **result.as_dict()}
    )

    label = getattr(result.version, "version_label", None)
    # On stdout as well as in the log. The next step is a human copying
    # this into a platform variable, and making them extract it from a
    # JSON log line is a step where mistakes happen.
    print(f"ARGUS_UNIVERSE_VERSION={label}")

    if not result.usable:
        _log.error(
            "the next scheduled ingestion would find no members in this universe",
            extra={
                "event": "universe_not_yet_usable",
                "detail": (
                    "Every interval begins at or after this build, and the next "
                    "ingestion reads members as of the last completed session — which "
                    "is earlier. The run would log `universe_size: 0` and do nothing. "
                    "Either wait for the next session to close and let ingestion run "
                    "for it, or ingest price history first and rebuild, which dates "
                    "the intervals from real bars instead of from this moment."
                ),
                **result.as_dict(),
            },
        )
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - the container's entrypoint
    raise SystemExit(main(sys.argv[1:]))
