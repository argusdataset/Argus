"""Distinct failure modes for FMP requests.

Downstream code has to tell these apart, and conflating them is a real
hazard: "this ticker was delisted in 2009 and has no data after that" and
"we were rate limited" both produce zero rows, but one is a fact about
the world and the other is a fact about our request budget. Treating the
second as the first would silently put holes in the historical record and
inflate backtest results — so nothing here ever returns empty data to
signal an error.
"""

from __future__ import annotations


class FmpError(Exception):
    """Base class for every FMP adapter failure."""


class FmpAuthenticationError(FmpError):
    """The API key was missing, malformed, or rejected (401/403).

    Distinct from a rate limit: retrying will not help.
    """


class FmpRateLimitError(FmpError):
    """The provider returned 429, or a documented limit was exceeded.

    Carries the server's Retry-After when it supplied one.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FmpSymbolNotFoundError(FmpError):
    """The requested symbol is not one FMP knows about.

    NOTE: FMP does not reliably distinguish this from "symbol exists but
    has no data in the requested range" — both commonly return an empty
    array with HTTP 200. The adapter therefore only raises this when it
    can actually tell (an explicit error payload, or a symbol absent from
    the stock list). Otherwise an empty response is reported as
    EmptyReason.NO_DATA_RETURNED and the caller decides. See the module
    README.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(f"FMP does not recognise symbol {symbol!r}.")
        self.symbol = symbol


class FmpProviderError(FmpError):
    """The provider failed: a 5xx, an unparseable body, or an error payload."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FmpTransportError(FmpError):
    """The request never completed — timeout, DNS failure, connection reset."""
