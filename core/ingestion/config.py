"""Every number Module 26 uses, isolated the way Module 18 isolated its own.

Same three methods `ScannerConfig` carries — `definition()`,
`content_checksum()`, `version_label()` — so a run can record which tier
policy was in force, and two runs under unchanged configuration record
the same label.

## The label on the tier intervals is not `UNVALIDATED_PLACEHOLDERS`

Every other versioned configuration in this project stamps its
definition `"calibration_status": "UNVALIDATED_PLACEHOLDERS"`, and that
label carries a specific meaning: *this magnitude was invented, no
outcome data has been fitted to it, and recalibration starts here.*

The tier intervals are not that kind of number. "Refresh a security in a
long decline every 30 days and one approaching a breakout every day" is
a declared spending decision about somebody else's API quota. No amount
of outcome data would validate it, because it is not a claim about the
market — it is a claim about what ARGUS is willing to pay for. Stamping
it `UNVALIDATED_PLACEHOLDERS` would put it in a queue it can never leave
and dilute a label that currently means something precise.

So the definition carries `"basis": "COST_POLICY"` instead, and the
settings below carry a matching per-setting `kind`. The distinction is
worth keeping sharp: a future reader recalibrating ARGUS's thresholds
against real outcomes should skip every number tagged `cost_policy`, and
a future reader trimming the FMP bill should look at nothing else.

## Which numbers are which

- `cost_policy` — the four tier intervals, and the rate limit at which
  bulk endpoints are assumed to be entitled. Business decisions.
- `operational` — how much history each request asks for. Bounds effort,
  never what gets computed, because `persist()` is insert-only and
  idempotent: asking for eight quarters instead of two hundred changes
  the size of a response, not the contents of the database.

Nothing here is `calibratable`. If a number in this module ever is, it
belongs to whichever module owns the judgement it encodes, not to a
fetch scheduler.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from core.market_state.watchlists import WATCHLIST_NAMES
from core.model_validation_evaluation.validation.config import (
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
)
from data.provider_adapters.fmp.fetchers import STATEMENT_ENDPOINTS

#: A declared spending decision. Not a magnitude anyone could validate
#: against outcomes, and deliberately not `calibratable` — see the module
#: docstring on why conflating the two would spoil both labels.
COST_POLICY = "cost_policy"

#: The three kinds Module 17 established, plus this module's fourth.
INGESTION_KINDS = (*KINDS, COST_POLICY)

#: The label stamped on `definition()` in place of a calibration status.
COST_POLICY_BASIS = "COST_POLICY"

__all__ = [
    "COST_POLICY",
    "COST_POLICY_BASIS",
    "INGESTION_KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "TIER_SETTINGS",
    "IngestionConfig",
    "IngestionSetting",
    "IngestionSettings",
]


@dataclass(frozen=True, slots=True)
class IngestionSetting:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _s(value: float, kind: str, rationale: str) -> IngestionSetting:
    return IngestionSetting(value=value, kind=kind, rationale=rationale)


#: Watchlist name -> the setting holding its refresh interval.
#:
#: Keyed by Module 10's own watchlist names rather than by a private
#: enum, so a fifth watchlist added there fails a test here instead of
#: silently getting no tier. The mapping is data because the alternative
#: — an `if name == "DOWN_TREND"` chain in the due-ness logic — is
#: exactly the inline threshold this project's isolation tests forbid.
TIER_SETTINGS: dict[str, str] = {
    "DOWN_TREND": "down_trend_interval_days",
    "CONSOLIDATION": "consolidation_interval_days",
    "BREAKOUT_READY": "breakout_ready_interval_days",
    "UPTREND": "uptrend_interval_days",
}


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    """How often to look, and how much to ask for."""

    # -- Tier intervals ------------------------------------------------------
    down_trend_interval_days: IngestionSetting = field(
        default_factory=lambda: _s(
            30.0,
            COST_POLICY,
            "Days between deep refreshes for a security in DOWN_TREND or "
            "BASE_FORMING. A name a month into a decline is not one whose "
            "fundamentals or news ARGUS needs re-read daily: the pattern "
            "being watched for takes months to form, and nothing in the "
            "scan depends on fundamentals at all. Thirty days is the "
            "cheapest cadence that still catches a filing before the "
            "security reaches a phase where it matters.",
        )
    )
    consolidation_interval_days: IngestionSetting = field(
        default_factory=lambda: _s(
            10.0,
            COST_POLICY,
            "Days between deep refreshes in CONSOLIDATION or ACCUMULATION. "
            "A base can resolve in either direction within weeks, so the "
            "context wants to be current before it does — but not daily, "
            "because most bases spend most of their life going nowhere.",
        )
    )
    breakout_ready_interval_days: IngestionSetting = field(
        default_factory=lambda: _s(
            1.0,
            COST_POLICY,
            "Daily, for BREAKOUT_WATCH and BREAKOUT_READY. This is where "
            "fresh information is worth the most: a security days from a "
            "confirmed move is one a human will actually open and read, "
            "and stale news on that page is the most visible way this "
            "system could look wrong.",
        )
    )
    uptrend_interval_days: IngestionSetting = field(
        default_factory=lambda: _s(
            1.0,
            COST_POLICY,
            "Daily, for UPTREND. Same reasoning as BREAKOUT_READY, and "
            "the same interval, which means a BREAKOUT_READY -> UPTREND "
            "transition is not an escalation — both are already at the "
            "floor. The two are kept as separate settings anyway so that "
            "changing one does not silently change the other.",
        )
    )

    # -- Provider tier -------------------------------------------------------
    bulk_entitlement_requests_per_minute: IngestionSetting = field(
        default_factory=lambda: _s(
            3000.0,
            COST_POLICY,
            "The standard-endpoint rate limit at which this module assumes "
            "bulk endpoints are entitled. FMP's published limits are 300 "
            "(Starter), 750 (Premium) and 3000 (Ultimate), and the bulk "
            "endpoints are believed to require Ultimate. So a configured "
            "`fmp_requests_per_minute` at or above this is read as 'the "
            "Ultimate plan is provisioned' and the daily pull switches to "
            "the single bulk request. A proxy for a plan entitlement the "
            "API does not report — see `strategy.py` on why it is the "
            "least-bad available signal and what to change if FMP moves "
            "bulk to a cheaper plan.",
        )
    )

    # -- Request sizing ------------------------------------------------------
    incremental_lookback_days: IngestionSetting = field(
        default_factory=lambda: _s(
            7.0,
            OPERATIONAL,
            "Calendar days of price history each per-symbol daily request "
            "asks for, ending at the target session. More than one because "
            "it is what makes a missed run self-repairing: a security that "
            "got no bar on Tuesday is fetched on Wednesday with a window "
            "that covers both, so the gap closes without a catch-up "
            "mechanism. Operational because `persist()` is insert-only — a "
            "wider window re-offers rows that are already there and "
            "inserts none of them.",
        )
    )
    statement_limit: IngestionSetting = field(
        default_factory=lambda: _s(
            8.0,
            OPERATIONAL,
            "Periods requested per statement type. Eight annual or "
            "quarterly periods reaches back two to eight years, which "
            "covers a restatement of anything recent; the full history was "
            "already fetched by the backfill, and re-requesting two "
            "hundred periods daily would be paying to re-download the same "
            "rows `persist()` then declines to insert.",
        )
    )
    news_limit: IngestionSetting = field(
        default_factory=lambda: _s(
            50.0,
            OPERATIONAL,
            "Articles requested per security per refresh. Fifty covers a "
            "day of coverage on all but the most-written-about names, and "
            "the ones it does not cover are exactly the ones whose next "
            "refresh is tomorrow.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access, matching ScannerSettings.
    # ---------------------------------------------------------------------

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def cost_policy(self) -> dict[str, float]:
        """The spending decisions. What a cost review reads and nothing else."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == COST_POLICY
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> IngestionSettings:
        stored = definition.get("settings", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = IngestionSetting(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    def interval_days(self, watchlist: str) -> int:
        """Days between deep refreshes for a security on `watchlist`.

        Raises for an unknown name rather than defaulting: a watchlist
        with no tier would silently never be refreshed, which is the
        failure this module exists to end.
        """
        try:
            setting_name = TIER_SETTINGS[watchlist]
        except KeyError:
            raise KeyError(
                f"No refresh tier for watchlist {watchlist!r}. Known: "
                f"{sorted(TIER_SETTINGS)}. A watchlist added to Module 10 needs "
                "an interval here, or securities on it are never refreshed."
            ) from None
        return int(getattr(self, setting_name).value)

    @property
    def lookback_days(self) -> int:
        return int(self.incremental_lookback_days.value)

    @property
    def statements(self) -> int:
        return int(self.statement_limit.value)

    @property
    def articles(self) -> int:
        return int(self.news_limit.value)

    @property
    def bulk_entitlement(self) -> int:
        return int(self.bulk_entitlement_requests_per_minute.value)


#: Which statement types a deep refresh asks for, read from Module 04's
#: own table rather than restated, so a statement type added there is
#: picked up here instead of quietly going un-refreshed.
#:
#: All five is the expensive choice and it is deliberate: `KEY_METRICS`
#: and `RATIOS` are derivable from the three statements in principle, but
#: FMP's definitions are the ones Module 19's Terminal displays, and
#: recomputing them here would mean this module owning a fundamentals
#: taxonomy that belongs to Module 05.
DEFAULT_STATEMENT_TYPES: tuple[str, ...] = tuple(sorted(STATEMENT_ENDPOINTS))

#: Annual, matching the period `fetch_financial_statement` defaults to.
DEFAULT_STATEMENT_PERIOD = "annual"


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """Module 26's complete, versioned configuration."""

    name: str = "argus-daily-ingestion"
    settings: IngestionSettings = field(default_factory=IngestionSettings)
    statement_types: tuple[str, ...] = DEFAULT_STATEMENT_TYPES
    statement_period: str = DEFAULT_STATEMENT_PERIOD

    def definition(self) -> dict[str, Any]:
        """The content a version label is computed over.

        `basis` rather than `calibration_status`, and the module
        docstring says why at length. The short version: these numbers
        are spending decisions, and the label that means "not yet fitted
        to outcome data" would be false of them in a way that erodes
        what it means everywhere else.
        """
        return {
            "name": self.name,
            "settings": self.settings.as_dict(),
            "statement_types": list(self.statement_types),
            "statement_period": self.statement_period,
            "basis": COST_POLICY_BASIS,
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"

    def watchlists(self) -> tuple[str, ...]:
        """The watchlist names this config gives a tier to, in Module 10's order."""
        return tuple(name for name in WATCHLIST_NAMES if name in TIER_SETTINGS)
