"""Which FMP endpoint the daily price pull uses, decided from configuration.

Module 04 provides both paths and its own docstring explains why both are
right for different jobs:

- `fetch_eod_for_date()` calls `/stable/eod-bulk` — one request for the
  entire universe, on the bulk rate-limit tier. Overwhelmingly the better
  choice for a daily incremental pull.
- `fetch_daily_history()` per symbol calls
  `/stable/historical-price-eod/full` — one request per symbol, on the
  standard tier. Roughly ten thousand requests instead of one.

So bulk would obviously win, except for one thing the API does not tell
us: **bulk endpoints are believed to require FMP's Ultimate plan**, and
the plan ARGUS is recommended to run is Premium. Calling a bulk endpoint
without the entitlement does not return a smaller answer; it returns an
error, and the daily pull fails.

## Inferring an entitlement from a rate limit

There is no endpoint that reports which plan a key is on. What the
configuration does carry is the per-minute limit the plan allows, and
FMP's published limits are distinct per plan — 300 Starter, 750 Premium,
3000 Ultimate. So `fmp_requests_per_minute` at or above
`bulk_entitlement_requests_per_minute` is read as "Ultimate", and
anything below it as "no bulk entitlement, use the per-symbol path".

This is a proxy and it is stated as one. It is the least-bad signal
available, and it fails in the safe direction: an operator who has
Ultimate but left the rate limit at its conservative default gets the
per-symbol path, which works and is merely slower. The unsafe direction
— assuming bulk on a plan that does not have it — requires someone to
raise the limit past 3000 without having the plan, which is a thing
nobody does by accident.

**Revisit this file the day the FMP tier changes.** Two knobs, both in
`config.py`: set `fmp_requests_per_minute` to the new plan's published
limit, and if FMP moves bulk endpoints to a cheaper plan, lower
`bulk_entitlement_requests_per_minute` to that plan's limit.

## Why the per-symbol path is not a hardship

At Premium's 750 requests per minute a ten-thousand-symbol universe
completes in about fourteen minutes; at Starter's 300 it takes about
thirty-three. The ingestion cron fires ninety minutes before the
scanner, so either finishes with margin. See `core/ingestion/README.md`
for the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.ingestion.config import IngestionConfig
from packages.config.settings import ProvidersSettings

__all__ = ["EndpointStrategy", "StrategyDecision", "select_strategy"]


class EndpointStrategy(StrEnum):
    """Which of Module 04's two daily-price paths this run will use."""

    #: One request per symbol, standard rate-limit tier. The default.
    PER_SYMBOL = "per_symbol"
    #: One request for the whole universe, bulk tier. Needs Ultimate.
    BULK = "bulk"


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """The chosen path and the reasoning, so a log line explains itself."""

    strategy: EndpointStrategy
    reason: str
    requests_per_minute: int
    bulk_entitlement_threshold: int

    @property
    def is_bulk(self) -> bool:
        return self.strategy is EndpointStrategy.BULK

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "reason": self.reason,
            "requests_per_minute": self.requests_per_minute,
            "bulk_entitlement_threshold": self.bulk_entitlement_threshold,
        }


def select_strategy(
    providers: ProvidersSettings,
    config: IngestionConfig | None = None,
) -> StrategyDecision:
    """Pick the daily-price path from what is actually provisioned."""
    resolved = config or IngestionConfig()
    threshold = resolved.settings.bulk_entitlement
    configured = int(providers.fmp_requests_per_minute)

    if configured >= threshold:
        return StrategyDecision(
            strategy=EndpointStrategy.BULK,
            reason=(
                f"fmp_requests_per_minute is {configured}, at or above the "
                f"{threshold}/min limit FMP publishes for the Ultimate plan, so the "
                "bulk EOD endpoint is assumed entitled. One request covers the whole "
                "universe."
            ),
            requests_per_minute=configured,
            bulk_entitlement_threshold=threshold,
        )

    return StrategyDecision(
        strategy=EndpointStrategy.PER_SYMBOL,
        reason=(
            f"fmp_requests_per_minute is {configured}, below the {threshold}/min "
            "limit FMP publishes for the Ultimate plan, so bulk endpoints are not "
            "assumed entitled. One standard-tier request per symbol instead, which "
            "works on every paid plan."
        ),
        requests_per_minute=configured,
        bulk_entitlement_threshold=threshold,
    )
