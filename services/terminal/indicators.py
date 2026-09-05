"""Provider-computed technical indicators. ARGUS never computes one.

## Why fetch what a chart could calculate

An SMA over bars ARGUS already stores is trivial arithmetic, and doing it
here would still be wrong for the reason `company.py` gives about a
derived P/E: the number would have no `availability_time` of its own, no
lineage, and no other part of the system able to reproduce it. Stored
provider values have all three, and — the part that matters for a
Terminal — they are the *same* values a reader sees elsewhere, rather
than ARGUS's arithmetic disagreeing at the third decimal with everyone
else's.

There is a second reason, specific to this project. `core/market_state/`
and `core/scoring/` compute their own series from canonical bars, under
their own rules. If the Terminal computed indicators too, there would be
two implementations of "the 50-day average" in one codebase, and the
first time they disagreed the question of which was right would have no
answer. Fetching keeps the Terminal's numbers plainly the provider's and
the engine's plainly its own.

## The parameters are part of the identity

A 14-period RSI and a 50-period RSI are different series. All three of
`indicator`, `period_length` and `timeframe` are in the table's key, in
this module's arguments, and echoed back in the response — a chart that
lost track of which series it drew would be wrong in a way nothing would
catch.

`ALLOWED_INDICATORS` is the endpoint registry's own tuple rather than a
copy: an indicator FMP does not expose cannot be requested, and the check
cannot drift from the fetcher.

## Availability, and why these appear a day late

`translate_technical_indicator` reuses `ProviderLagPolicy.daily_bar` —
an indicator computed from a bar cannot be knowable before the bar is.
So a query at Tuesday noon does not see Tuesday's value, and that is
correct rather than a bug: the session has not closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.engine import Connection

from core.ingestion.terminal_data import INDICATOR_PERIODS
from data.provider_adapters.fmp.endpoints import TECHNICAL_INDICATORS
from services.terminal.company import resolve_company
from services.terminal.config import TerminalConfig
from services.terminal.errors import invalid_indicator
from services.terminal.schemas import IndicatorPoint, IndicatorResponse
from services.terminal.stored import indicator_series, unavailable

__all__ = ["ALLOWED_INDICATORS", "DEFAULT_TIMEFRAME", "default_period_length", "read_indicator"]

#: The nine FMP exposes, straight from the endpoint registry. Not copied:
#: a second list would eventually disagree with the fetcher, and the
#: disagreement would show up as an endpoint that accepts a request
#: nothing can ever satisfy.
ALLOWED_INDICATORS: tuple[str, ...] = TECHNICAL_INDICATORS

#: Daily. The only timeframe `core/ingestion/terminal_data.py` ingests,
#: because ARGUS's canonical bars are daily and an intraday indicator
#: would sit beside nothing it could be checked against. A caller may
#: still name another one — the parameter is part of the series key, so
#: asking for an uningested timeframe correctly reports nothing rather
#: than quietly serving daily values under an intraday label.
DEFAULT_TIMEFRAME = "1day"


def default_period_length(indicator: str) -> int:
    """The period ARGUS actually ingested for this indicator.

    Read from `core/ingestion/terminal_data.py` rather than copied. The
    default *must* equal what the ingestion writes — a 20-period default
    over a 50-period stored series returns nothing, correctly and
    uselessly — and two constants that must agree eventually will not.

    A caller naming a period nobody ingested still gets an honest empty
    series with `NEVER_INGESTED` rather than an error: unlike an unknown
    indicator name, an uningested period is a request that a later
    ingestion run could satisfy.
    """
    return INDICATOR_PERIODS[indicator]


def read_indicator(
    connection: Connection,
    ticker: str,
    indicator: str,
    *,
    period_length: int | None = None,
    timeframe: str = DEFAULT_TIMEFRAME,
    as_of: datetime | None = None,
    limit: int | None = None,
    config: TerminalConfig | None = None,
) -> IndicatorResponse:
    """One indicator series, newest bar first.

    An unknown indicator name raises rather than returning an empty
    series: "FMP has no such indicator" and "ARGUS has not ingested this
    one yet" are different facts, and an empty `points` list would state
    the second when the first is true.

    `period_length` defaults to the one the ingestion writes for this
    indicator, so a caller who does not care gets the series that exists
    rather than an empty one — see `default_period_length`.
    """
    normalised = indicator.strip().lower()
    if normalised not in ALLOWED_INDICATORS:
        raise invalid_indicator(indicator, ALLOWED_INDICATORS)

    period = default_period_length(normalised) if period_length is None else int(period_length)
    config = config or TerminalConfig()
    moment = as_of or datetime.now(UTC)
    profile = resolve_company(connection, ticker, as_of=moment)

    rows, reason = indicator_series(
        connection,
        profile.security_id,
        indicator=normalised,
        period_length=period,
        timeframe=timeframe,
        as_of=moment,
        limit=config.limits.bounded_indicator_points(limit),
    )

    return IndicatorResponse(
        security=profile,
        as_of=moment,
        indicator=normalised,
        period_length=period,
        timeframe=timeframe,
        points=[
            IndicatorPoint(
                event_time=row.event_time,
                value=_float(row.value),
                data=dict(row.data),
            )
            for row in rows
        ],
        unavailable=unavailable(reason) if reason is not None else None,
    )


def _float(value: Decimal | None) -> float | None:
    """The stored value as JSON sees it.

    `None` stays `None` rather than becoming `0.0` — the rule eighteen
    modules have enforced. A null here means the translation could not
    find the number under any of the keys it tried, and the untouched
    provider row sits beside it in `data` for whoever needs to look.
    """
    return None if value is None else float(value)
