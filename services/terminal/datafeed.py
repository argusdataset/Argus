"""The TradingView Advanced Charts datafeed — server side only.

## What this is

TradingView's Charting Library ships a JavaScript widget that asks a
server for symbols and bars over a small HTTP protocol (the "UDF"
datafeed). This module is that server. The widget, its configuration, and
every line of chart JavaScript are frontend territory and are not here.

The project chose Advanced Charts over the plain embeddable widgets
specifically so ARGUS can draw its own overlays — consolidation zones,
state-transition markers — on top of its own data. Those overlays are
Module 21's concern. This module deliberately advertises
`supports_marks: false`, because a datafeed that claims to serve marks and
returns none makes the widget ask forever.

## What is implemented, and what is left out on purpose

Implemented: `/config`, `/time`, `/symbols` (resolveSymbol), `/search`,
`/history` (getBars). That is the complete set required for a working
historical chart.

Left out, each for a reason rather than for time:

- **`/marks`, `/timescale_marks`** — Module 21's overlays. Serving them
  here would be building Intelligence output inside the Terminal.
- **`/quotes`, streaming** — ARGUS holds end-of-day bars. There is no
  real-time source to stream, and a `subscribeBars` that never fires is
  worse than one the library knows not to call. `data_status` says
  `endofday` and `has_intraday` is false, so the widget behaves
  accordingly.
- **`/symbol_info` (group request)** — an optimisation for exchanges
  serving thousands of symbols in one payload. `supports_group_request`
  is false; per-symbol resolution is correct and simpler, and the
  Charting Library handles it natively.

## Resolutions

`1D`, `1W`, `1M`. Nothing intraday, because only daily bars are sourced
from a provider — Module 05 is explicit that H4 cannot be derived from
daily data without fabricating intraday structure, and the same reasoning
rules out serving a 1-minute chart from end-of-day bars. Weekly and
monthly *are* derivable, and Module 08 derives them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.engine import Connection

from data.canonical_model.records import CanonicalTimeframe
from data.normalization.identity import SecurityIdentityResolver
from infra.db.schema.identity import security_identity, security_ticker_history
from services.terminal.bars import load_bars
from services.terminal.config import TerminalConfig
from services.terminal.errors import security_not_found
from services.terminal.schemas import (
    BarsResponse,
    DatafeedConfig,
    SymbolInfo,
    SymbolSearchResult,
)

__all__ = [
    "RESOLUTIONS",
    "STATUS_ERROR",
    "STATUS_NO_DATA",
    "STATUS_OK",
    "datafeed_config",
    "resolve_resolution",
    "resolve_symbol",
    "search_symbols",
    "server_time",
    "symbol_history",
]

#: The library's resolution strings, mapped to ARGUS timeframes. `D` and
#: `1D` are both sent by different versions of the widget; both mean the
#: same thing.
RESOLUTIONS: dict[str, CanonicalTimeframe] = {
    "D": CanonicalTimeframe.DAILY,
    "1D": CanonicalTimeframe.DAILY,
    "W": CanonicalTimeframe.WEEKLY,
    "1W": CanonicalTimeframe.WEEKLY,
    "M": CanonicalTimeframe.MONTHLY,
    "1M": CanonicalTimeframe.MONTHLY,
}

#: What the widget is told it may ask for. A subset of the keys above —
#: the bare `D`/`W`/`M` forms are accepted on the way in but not
#: advertised, so the widget sends one canonical spelling.
ADVERTISED_RESOLUTIONS: tuple[str, ...] = ("1D", "1W", "1M")

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_ERROR = "error"

_DEFAULT_EXCHANGE = "ARGUS"


def datafeed_config(config: TerminalConfig | None = None) -> DatafeedConfig:
    """`/config`. What this datafeed can and cannot do, stated honestly."""
    return DatafeedConfig(
        supported_resolutions=list(ADVERTISED_RESOLUTIONS),
        exchanges=[
            {"value": "", "name": "All Exchanges", "desc": "All exchanges"},
            {"value": "NASDAQ", "name": "NASDAQ", "desc": "NASDAQ"},
            {"value": "NYSE", "name": "NYSE", "desc": "NYSE"},
        ],
        symbols_types=[{"name": "Stock", "value": "stock"}],
    )


def server_time(now: datetime | None = None) -> int:
    """`/time`. Unix seconds, so the widget can align its axis."""
    return int((now or datetime.now(UTC)).timestamp())


def resolve_resolution(resolution: str | None) -> CanonicalTimeframe:
    """Map the library's resolution string to an ARGUS timeframe.

    Unknown resolutions fall back to daily rather than raising. The widget
    can request a resolution the server never advertised — a saved chart
    layout, a user typing one in — and answering with the nearest thing
    ARGUS actually has beats returning an error the chart renders as a
    blank screen.
    """
    if not resolution:
        return CanonicalTimeframe.DAILY
    return RESOLUTIONS.get(resolution.strip().upper(), CanonicalTimeframe.DAILY)


def resolve_symbol(
    connection: Connection, symbol: str, *, as_of: datetime | None = None
) -> SymbolInfo:
    """`/symbols?symbol=X`. The widget's `resolveSymbol`.

    Accepts both `AAPL` and `NASDAQ:AAPL`; the library sends the prefixed
    form once a symbol has been resolved once, and the bare form when a
    user types it.
    """
    moment = as_of or datetime.now(UTC)
    ticker = _bare_ticker(symbol)

    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.try_resolve(ticker, moment)
    if security_id is None:
        raise security_not_found(ticker)

    row = connection.execute(
        select(security_ticker_history.c.ticker, security_ticker_history.c.exchange)
        .where(
            security_ticker_history.c.security_id == security_id,
            security_ticker_history.c.valid_from <= moment,
        )
        .order_by(desc(security_ticker_history.c.valid_from))
        .limit(1)
    ).one_or_none()
    name = connection.execute(
        select(security_identity.c.name).where(security_identity.c.id == security_id)
    ).scalar_one_or_none()

    current = row.ticker if row is not None else ticker
    exchange = row.exchange if row is not None else _DEFAULT_EXCHANGE

    return SymbolInfo(
        name=current,
        ticker=current,
        description=name or current,
        exchange=exchange,
        listed_exchange=exchange,
        supported_resolutions=list(ADVERTISED_RESOLUTIONS),
    )


def search_symbols(
    connection: Connection,
    query: str,
    *,
    limit: int | None = None,
    exchange: str | None = None,
    config: TerminalConfig | None = None,
    as_of: datetime | None = None,
) -> list[SymbolSearchResult]:
    """`/search`. Prefix match on currently-valid tickers and names.

    Searches only tickers valid at `as_of` — a recycled ticker should
    surface whoever holds it now, not every company that ever did. An
    empty query returns nothing rather than the universe; see
    `min_search_query_length` in `config.py`.
    """
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    needle = query.strip()
    if len(needle) < int(config.limits.min_search_query_length):
        return []

    count = config.limits.bounded_search_limit(limit)
    pattern = f"{needle.upper()}%"
    name_pattern = f"%{needle}%"

    conditions = [
        security_ticker_history.c.valid_from <= moment,
        or_(
            security_ticker_history.c.valid_to.is_(None),
            security_ticker_history.c.valid_to > moment,
        ),
        or_(
            security_ticker_history.c.ticker.like(pattern),
            security_identity.c.name.ilike(name_pattern),
        ),
    ]
    if exchange:
        conditions.append(security_ticker_history.c.exchange == exchange)

    rows = connection.execute(
        select(
            security_ticker_history.c.ticker,
            security_ticker_history.c.exchange,
            security_identity.c.name,
        )
        .select_from(
            security_ticker_history.join(
                security_identity, security_identity.c.id == security_ticker_history.c.security_id
            )
        )
        .where(*conditions)
        .order_by(security_ticker_history.c.ticker)
        .limit(count)
    ).all()

    return [
        SymbolSearchResult(
            symbol=row.ticker,
            full_name=f"{row.exchange}:{row.ticker}",
            description=row.name or row.ticker,
            exchange=row.exchange,
            ticker=row.ticker,
        )
        for row in rows
    ]


def symbol_history(
    connection: Connection,
    symbol: str,
    *,
    resolution: str,
    start: datetime,
    end: datetime,
    countback: int | None = None,
    as_of: datetime | None = None,
    config: TerminalConfig | None = None,
) -> BarsResponse:
    """`/history`. The widget's `getBars`.

    Returns `no_data` rather than an error when the range is empty — the
    protocol's own distinction, and the one that lets a chart page
    backwards past a security's listing date without showing a failure.
    `nextTime` points at the oldest bar ARGUS holds, so the widget knows
    where to stop.
    """
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    timeframe = resolve_resolution(resolution)

    security_id = _resolve_id(connection, symbol, moment)

    limit = int(config.limits.max_bars_per_request)
    if countback is not None:
        limit = min(limit, max(1, countback))

    series = load_bars(
        connection,
        security_id,
        start=start,
        end=end,
        as_of=moment,
        timeframe=timeframe,
        max_bars=limit,
    )

    if series.is_empty:
        return BarsResponse(s=STATUS_NO_DATA, nextTime=series.earliest_available)

    return BarsResponse(
        s=STATUS_OK,
        t=series.times,
        o=series.opens,
        h=series.highs,
        l=series.lows,
        c=series.closes,
        v=series.volumes,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _resolve_id(connection: Connection, symbol: str, as_of: datetime) -> UUID:
    resolver = SecurityIdentityResolver(connection)
    security_id = resolver.try_resolve(_bare_ticker(symbol), as_of)
    if security_id is None:
        raise security_not_found(_bare_ticker(symbol))
    return security_id


def _bare_ticker(symbol: str) -> str:
    """`NASDAQ:AAPL` and `AAPL` both mean AAPL."""
    cleaned = (symbol or "").strip().upper()
    return cleaned.split(":")[-1] if ":" in cleaned else cleaned
