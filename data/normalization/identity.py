"""Ticker to security-identity resolution.

Everything in ARGUS references `security_identity.id`. A ticker is only
ever a lookup key, resolved *as of a date*, because tickers change hands:
a security that traded as FB until June 2022 and META afterwards is one
security with one identity, and a 2019 bar filed under FB must land on
the same `security_id` as a 2024 bar filed under META. Getting this wrong
produces two disconnected half-histories with nothing visibly failing.

Resolution reads `security_ticker_history`, whose two gist exclusion
constraints (added in migration 0002) guarantee the lookup is
unambiguous: one security holds one ticker at a time, and one ticker maps
to one security at a time. So "who was trading as AAPL on 2013-06-01" has
exactly one answer, enforced by the database rather than assumed here.

On scope: registering identities is done here rather than in Module 06
because canonical rows cannot be written without a `security_id` to
reference, and Module 06 owns *universe versioning*
(`universe_version` / `universe_membership`) — different tables, a
different question. Flagged in the module README.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from data.canonical_model.exchanges import CanonicalExchange, normalize_symbol
from infra.db.schema.identity import security_identity, security_ticker_history


class TickerResolutionError(LookupError):
    """A ticker could not be resolved to a security identity."""

    def __init__(self, symbol: str, as_of: datetime | None = None) -> None:
        when = f" as of {as_of.isoformat()}" if as_of else ""
        super().__init__(f"No security identity for ticker {symbol!r}{when}.")
        self.symbol = symbol
        self.as_of = as_of


@dataclass(frozen=True, slots=True)
class ResolvedSecurity:
    """A ticker resolved to its stable identity."""

    security_id: UUID
    symbol: str
    exchange: CanonicalExchange


class SecurityIdentityResolver:
    """Resolves tickers to `security_id`, with a per-connection cache.

    A backfill resolves the same ticker once per fetched record, so the
    cache turns tens of thousands of lookups into one per (ticker, date
    window). It is keyed by the resolved *row*, not by ticker alone,
    because the correct answer genuinely depends on the as-of date.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._cache: dict[tuple[str, datetime | None], UUID] = {}

    def resolve(self, symbol: str, as_of: datetime | None = None) -> UUID:
        """The `security_id` trading under `symbol` at `as_of`.

        `as_of` of None means "whichever record is current" — correct for
        live ingestion, wrong for historical bars, which must pass the
        bar's own date so a ticker change resolves to the holder at that
        time rather than to today's.
        """
        key = (normalize_symbol(symbol), as_of)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        security_id = self._lookup(key[0], as_of)
        if security_id is None:
            raise TickerResolutionError(key[0], as_of)
        self._cache[key] = security_id
        return security_id

    def try_resolve(self, symbol: str, as_of: datetime | None = None) -> UUID | None:
        """Like `resolve`, but returns None instead of raising."""
        try:
            return self.resolve(symbol, as_of)
        except TickerResolutionError:
            return None

    def _lookup(self, symbol: str, as_of: datetime | None) -> UUID | None:
        history = security_ticker_history
        query = select(history.c.security_id).where(history.c.ticker == symbol)

        if as_of is None:
            # Current holder: the open-ended record, or the latest one.
            query = query.order_by(history.c.valid_to.is_(None).desc(), history.c.valid_from.desc())
        else:
            moment = _as_utc(as_of)
            query = query.where(
                history.c.valid_from <= moment,
                (history.c.valid_to.is_(None)) | (history.c.valid_to > moment),
            )

        return self._connection.execute(query.limit(1)).scalar_one_or_none()

    # -- Registration -------------------------------------------------------

    def register(
        self,
        symbol: str,
        *,
        exchange: CanonicalExchange,
        valid_from: datetime,
        name: str | None = None,
        security_id: UUID | None = None,
        valid_to: datetime | None = None,
    ) -> UUID:
        """Record a ticker's validity window, creating an identity if needed.

        Pass `security_id` to attach a *new ticker to an existing
        security* — that is how a ticker change is recorded, and it is
        what keeps the two halves of the history joined. Omit it to mint
        a new identity.

        The caller is responsible for closing the previous ticker's
        window first (see `record_ticker_change`); the database's
        exclusion constraints will reject an overlap outright rather than
        let an ambiguous mapping exist.
        """
        symbol = normalize_symbol(symbol)

        if security_id is None:
            security_id = self._connection.execute(
                security_identity.insert().values(name=name).returning(security_identity.c.id)
            ).scalar_one()

        self._connection.execute(
            security_ticker_history.insert().values(
                security_id=security_id,
                ticker=symbol,
                exchange=exchange.value,
                valid_from=_as_utc(valid_from),
                valid_to=_as_utc(valid_to) if valid_to else None,
            )
        )
        self._cache.clear()
        return security_id

    def record_ticker_change(
        self,
        *,
        old_symbol: str,
        new_symbol: str,
        changed_at: datetime,
        exchange: CanonicalExchange,
    ) -> UUID:
        """Close the old ticker's window and open the new one.

        Both windows belong to the same `security_id`, so history filed
        under either ticker resolves to one security. The old window is
        closed at exactly the instant the new one opens — the exclusion
        constraints treat the range as half-open, so touching endpoints
        do not overlap.
        """
        moment = _as_utc(changed_at)
        security_id = self.resolve(old_symbol)

        history = security_ticker_history
        self._connection.execute(
            history.update()
            .where(
                history.c.security_id == security_id,
                history.c.ticker == normalize_symbol(old_symbol),
                history.c.valid_to.is_(None),
            )
            .values(valid_to=moment)
        )
        self.register(
            new_symbol,
            exchange=exchange,
            valid_from=moment,
            security_id=security_id,
        )
        return security_id

    def ensure(
        self,
        symbol: str,
        *,
        exchange: CanonicalExchange,
        valid_from: datetime,
        name: str | None = None,
    ) -> UUID:
        """Resolve `symbol`, registering it if it is not known yet.

        Convenience for ingestion, where most symbols already exist and a
        few are new. Resolution is by current holder, so this must not be
        used to backfill a *historical* ticker window.
        """
        existing = self.try_resolve(symbol)
        if existing is not None:
            return existing
        return self.register(symbol, exchange=exchange, valid_from=valid_from, name=name)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
