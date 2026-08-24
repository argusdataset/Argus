"""Every number the Terminal serves under, isolated per Module 10's discipline.

Reuses Module 17's three kinds — `structural`, `calibratable`,
`operational`. Almost everything here is operational: a page size bounds
how much of an answer is returned, never what the answer is. A caller
asking for 25 news articles and one asking for 100 get the same articles
in the same order; one gets fewer of them.

## The exceptions, and why they are not operational

`max_watchlist_items` and `max_watchlists_per_user` are product limits.
They change what a user can do rather than how much of a response they
see, and both are invented numbers nobody has validated against how
people actually use a watchlist. Tagged `calibratable` so that when
somebody eventually looks at real usage, the two numbers worth revisiting
are already named.

## What is deliberately absent

No thresholds, no weights, no ramps. This module computes nothing about a
security — it reads what Modules 04-07 stored and shapes it for a
consumer. If a number ever appears here that decides something about a
company rather than about a response, that is a signal this module has
drifted into work that belongs somewhere else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from core.model_validation_evaluation.validation.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
)

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "TerminalConfig",
    "TerminalLimit",
    "TerminalLimits",
]


@dataclass(frozen=True, slots=True)
class TerminalLimit:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _l(value: float, kind: str, rationale: str) -> TerminalLimit:
    return TerminalLimit(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class TerminalLimits:
    """Response sizes, page bounds, and the two real product limits."""

    # -- Chart datafeed -----------------------------------------------------
    max_bars_per_request: TerminalLimit = field(
        default_factory=lambda: _l(
            5000.0,
            OPERATIONAL,
            "Bars returned by one `/history` call. TradingView's Charting "
            "Library pages backwards through history on its own, so a "
            "bound here costs the chart nothing and stops a single "
            "request materialising twenty years of daily bars. Operational "
            "because the bars returned are the same bars either way.",
        )
    )
    default_countback: TerminalLimit = field(
        default_factory=lambda: _l(
            300.0,
            OPERATIONAL,
            "Bars returned when the library sends `countback` without a "
            "usable `from`. Roughly a trading year, which is what a chart "
            "opens showing.",
        )
    )

    # -- News ---------------------------------------------------------------
    default_news_limit: TerminalLimit = field(
        default_factory=lambda: _l(
            25.0,
            OPERATIONAL,
            "Articles returned when a caller does not ask for a count. "
            "Enough to fill a Terminal panel without a scroll that never "
            "ends.",
        )
    )
    max_news_limit: TerminalLimit = field(
        default_factory=lambda: _l(
            100.0,
            OPERATIONAL,
            "Ceiling on the news page size, so a caller cannot ask for "
            "every article ever ingested in one request.",
        )
    )

    # -- Symbol search ------------------------------------------------------
    max_search_results: TerminalLimit = field(
        default_factory=lambda: _l(
            30.0,
            OPERATIONAL,
            "Ceiling on symbol-search results. TradingView's search box "
            "shows a short list and the library sends its own `limit`; "
            "this caps what it can ask for.",
        )
    )

    # -- Watchlists — the two real product limits ---------------------------
    max_watchlists_per_user: TerminalLimit = field(
        default_factory=lambda: _l(
            50.0,
            CALIBRATABLE,
            "How many named lists one user may keep. An invented number: "
            "nobody has looked at how people actually use watchlists, and "
            "the honest range is somewhere between 'five is plenty' and "
            "'let them have hundreds'. Named here so it is findable when "
            "real usage exists to check it against.",
        )
    )
    max_watchlist_items: TerminalLimit = field(
        default_factory=lambda: _l(
            250.0,
            CALIBRATABLE,
            "Securities on one watchlist. Also invented. Deliberately well "
            "under a universe's worth: a 'watchlist' with three thousand "
            "names is a screen, and ARGUS already has one of those.",
        )
    )
    max_watchlist_name_length: TerminalLimit = field(
        default_factory=lambda: _l(
            100.0,
            OPERATIONAL,
            "Characters in a watchlist name. A storage and display bound, "
            "not a judgement about names.",
        )
    )

    # -- Structural ---------------------------------------------------------
    min_search_query_length: TerminalLimit = field(
        default_factory=lambda: _l(
            1.0,
            STRUCTURAL,
            "A search needs something to search for. Not a knob: at zero "
            "the query matches every security in the universe, which is "
            "not a search result, it is a database dump.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access.
    # ---------------------------------------------------------------------

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def calibratable(self) -> dict[str, float]:
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == CALIBRATABLE
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    def bounded_news_limit(self, requested: int | None) -> int:
        if requested is None:
            return int(self.default_news_limit)
        return max(1, min(requested, int(self.max_news_limit)))

    def bounded_search_limit(self, requested: int | None) -> int:
        ceiling = int(self.max_search_results)
        if requested is None:
            return ceiling
        return max(1, min(requested, ceiling))


@dataclass(frozen=True, slots=True)
class TerminalConfig:
    """The Terminal's complete configuration.

    `stub_identity_enabled` is the one field here that is not a number and
    is the most consequential thing in the file — see `identity.py`. It
    exists so that shipping the temporary identity stub requires somebody
    to have left it on, rather than requiring them to remember to remove
    it.
    """

    name: str = "argus-terminal"
    limits: TerminalLimits = field(default_factory=TerminalLimits)
    #: Trust `X-Argus-User` as identity. A development affordance standing
    #: in for Module 22. False makes every user-scoped endpoint refuse.
    stub_identity_enabled: bool = True

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limits": self.limits.as_dict(),
            "stub_identity_enabled": self.stub_identity_enabled,
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
