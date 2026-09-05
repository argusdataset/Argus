"""Every number Module 29 uses, isolated per Module 10's discipline.

**None of these values has been validated.** Same status as Modules 10
through 12 and 28: no historical outcome data says whether two
independent buyers in thirty days is the right bar for a cluster, or
whether thirty days is the right window to look over. Every result this
module produces carries
`calibration_status: "UNVALIDATED_PLACEHOLDERS"`.

## Kind tagging

The same two kinds Modules 12 and 28 use:

- **`structural`** — follows from how something is defined, or from data
  latency. Changing it changes what the number *means*.
- **`calibratable`** — an invented magnitude. Recalibration starts here.

## Why the cluster threshold is a count and not a dollar figure

The obvious alternative — "raise when insiders bought more than $X" —
was deliberately not chosen. A $200,000 buy is a large personal
commitment from a biotech CFO and a rounding error from a mega-cap CEO,
so a dollar floor would systematically pick out large companies, which
is the same flat-threshold-across-a-heterogeneous-universe mistake
`docs/architecture/KNOWN_ISSUES.md` E5 records this project already
making once.

Counting *independent people* sidesteps the comparison entirely. Two
officers who both chose to buy in the same month is the same fact at any
market cap, which is exactly what
`core/candidate_detection/eligibility/bankruptcy.py` reasoned when it
required two independent distress signals rather than one severe one:
one insider's one purchase can be a scheduled diversification or a
personal liquidity event, and a cluster is harder to explain away.

## Why only open-market purchases count

Transaction code `P`. Not `A` (grant/award), not an option exercise:
those are compensation arriving on a schedule somebody else set, and
counting them would make the signal fire hardest in the months a
company happens to vest equity. A purchase is the one transaction type
where an insider chose to convert their own money into more of the stock
they already hold too much of.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
from typing import Any

#: Follows from a definition or from data latency.
STRUCTURAL = "structural"
#: An invented magnitude. Recalibration starts here.
CALIBRATABLE = "calibratable"

KINDS = (STRUCTURAL, CALIBRATABLE)

#: The SEC Form 4 transaction code for an open-market purchase. Structural
#: rather than configurable: it is what the code *means*, not a choice.
OPEN_MARKET_PURCHASE_CODE = "P"

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPEN_MARKET_PURCHASE_CODE",
    "STRUCTURAL",
    "OwnershipSignalConfig",
    "OwnershipThreshold",
    "OwnershipThresholds",
]


@dataclass(frozen=True, slots=True)
class OwnershipThreshold:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> OwnershipThreshold:
    return OwnershipThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class OwnershipThresholds:
    """The complete threshold set. Enumerable as a whole."""

    insider_cluster_window_days: OwnershipThreshold = field(
        default_factory=lambda: _t(
            30.0,
            CALIBRATABLE,
            "How many trailing days count as 'around the same time' for "
            "insider buys to be one cluster rather than unrelated "
            "decisions. A guess at long enough to catch buys spread over "
            "a filing window and an open trading window, short enough "
            "that two people acting on different information in "
            "different months do not read as agreement.",
        )
    )
    insider_cluster_min_buyers: OwnershipThreshold = field(
        default_factory=lambda: _t(
            2.0,
            CALIBRATABLE,
            "How many *distinct* insiders must have bought in the window "
            "before the signal raises. Two rather than one for the reason "
            "the bankruptcy gate requires two distress signals: a single "
            "purchase by a single person is explainable as personal "
            "liquidity or a diversification schedule, and a gate that "
            "fired on it would fire constantly.",
        )
    )
    insider_availability_lag_hours: OwnershipThreshold = field(
        default_factory=lambda: _t(
            48.0,
            STRUCTURAL,
            "Hours after an insider transaction date before ARGUS treats "
            "it as knowable. Form 4 is due within two business days of "
            "the trade, so the transaction date itself is *not* when the "
            "market could see it — filtering on the transaction date "
            "alone would let a backtest read a purchase up to two days "
            "before it was disclosed. Structural: it follows from the "
            "SEC's filing deadline, not from a trading judgement.",
        )
    )
    institutional_availability_lag_days: OwnershipThreshold = field(
        default_factory=lambda: _t(
            45.0,
            STRUCTURAL,
            "Days after a quarter ends before its 13F holdings are "
            "knowable. The SEC gives institutional managers 45 days after "
            "quarter end to file, so a quarter's ownership figures do not "
            "exist publicly until then. Anchoring availability to the "
            "quarter end itself would be the single largest leak "
            "available in this module — six weeks of hindsight on who was "
            "accumulating.",
        )
    )

    # ---------------------------------------------------------------------
    # Whole-set access. A recalibration tool uses these and nothing else.
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

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> OwnershipThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = OwnershipThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)

    @property
    def cluster_window_days(self) -> int:
        return int(self.insider_cluster_window_days.value)

    @property
    def cluster_window(self) -> timedelta:
        return timedelta(days=self.cluster_window_days)

    @property
    def min_buyers(self) -> int:
        return int(self.insider_cluster_min_buyers.value)

    @property
    def insider_availability_lag(self) -> timedelta:
        return timedelta(hours=self.insider_availability_lag_hours.value)

    @property
    def institutional_availability_lag(self) -> timedelta:
        return timedelta(days=self.institutional_availability_lag_days.value)


@dataclass(frozen=True, slots=True)
class OwnershipSignalConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-ownership-signals"
    thresholds: OwnershipThresholds = field(default_factory=OwnershipThresholds)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "thresholds": self.thresholds.as_dict(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
