"""The success definition, and every other number this module applies.

## The definition is the dataset's ground truth

Everything downstream — Module 17's hit rate, Module 13's eventual
recalibration, the answer to "does ARGUS actually work" — is computed
against labels this module assigns. The criterion that assigns them is
therefore not an implementation detail; it is the claim being tested, and
it is stated once, here, versioned, and stored with every row it labels.

**`+10% before -5% within 60 trading days`, measured from the setup's
`activated` event.**

That is deliberately the same string Module 13 stores as
`PROBABILITY_DEFINITION`, and the sameness is load-bearing rather than
tidy. Module 13 reserves `probability` for a calibrated model that does
not exist yet; when it is built, it will be fitted against the labels this
module produces. If the two definitions ever drift, the model would be
calibrated to predict something other than what the dataset records, and
nothing would fail — the numbers would simply mean something nobody
intended. A test asserts they are identical.

## Unvalidated, like everything else

The three numbers in the criterion are invented. 10% and 5% are a
plausible asymmetry for a pattern that expects expansion, not a measured
one; 60 trading days is a guess at how long an expansion takes to start.
They are placeholders in exactly the sense Modules 10 to 14's thresholds
are, and every stored outcome cites the snapshot that pins them.

Changing any of them changes what SUCCESS means, so a change is a new
`data_snapshot` row and outcomes computed under the old definition stay
attributable to it. That is the whole reason `setup_outcomes` gained a
`data_snapshot_id` in migration 0006.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from infra.db.schema.versioning import data_snapshot

#: Follows from a definition; changing it changes meaning.
STRUCTURAL = "structural"
#: An invented magnitude. Recalibration starts here.
CALIBRATABLE = "calibratable"

#: The predefined outcome, in words. Identical to Module 13's
#: `PROBABILITY_DEFINITION` — see the module docstring on why that matters.
SUCCESS_DEFINITION = "+10% before -5% within 60 trading days"


@dataclass(frozen=True, slots=True)
class OutcomeThreshold:
    """One named number, with how much to trust it."""

    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value


def _t(value: float, kind: str, rationale: str) -> OutcomeThreshold:
    return OutcomeThreshold(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class OutcomeThresholds:
    """The success criterion, plus the numbers the CASE heuristics apply."""

    # -- The criterion itself ------------------------------------------------
    target_gain: OutcomeThreshold = field(
        default_factory=lambda: _t(
            0.10,
            CALIBRATABLE,
            "Gain from entry that counts as the expansion having happened. "
            "Asymmetric against the stop on purpose — the pattern expects "
            "expansion, so a symmetric criterion would label a sideways "
            "drift as often as a real move — but the specific 2:1 ratio is "
            "invented.",
        )
    )
    stop_loss: OutcomeThreshold = field(
        default_factory=lambda: _t(
            -0.05,
            CALIBRATABLE,
            "Adverse move from entry that counts as the thesis having "
            "failed. Negative by convention so it is never mistaken for a "
            "magnitude.",
        )
    )
    horizon_trading_days: OutcomeThreshold = field(
        default_factory=lambda: _t(
            60.0,
            CALIBRATABLE,
            "Trading days, not calendar days: the criterion is about how "
            "many sessions the market had to resolve it, and a calendar "
            "window would silently shorten across a holiday stretch.",
        )
    )

    # -- CASE classification heuristics --------------------------------------
    expansion_floor: OutcomeThreshold = field(
        default_factory=lambda: _t(
            0.03,
            CALIBRATABLE,
            "Favourable excursion below which a setup is judged to have "
            "gone nowhere at all — false-positive type B, pattern without "
            "expansion. Distinguishes 'never moved' from 'moved and "
            "failed', which are different lessons.",
        )
    )
    breakdown_floor: OutcomeThreshold = field(
        default_factory=lambda: _t(
            -0.15,
            CALIBRATABLE,
            "Realized return below which the base is judged to have broken "
            "down rather than merely failed — type D. Well past the stop, "
            "so an ordinary stop-out is not miscounted as a collapse.",
        )
    )
    illiquid_dollar_volume: OutcomeThreshold = field(
        default_factory=lambda: _t(
            250_000.0,
            CALIBRATABLE,
            "Average dollar volume below which an outcome is treated as "
            "possibly a liquidity artefact — type F. Above Module 09's "
            "$50,000 execution floor, because clearing that gate is not "
            "the same as the print being trustworthy.",
        )
    )
    catalyst_window_days: OutcomeThreshold = field(
        default_factory=lambda: _t(
            5.0,
            CALIBRATABLE,
            "How close a scheduled binary event must sit to the excursion "
            "for the move to be attributed to it — type E. A wider window "
            "would blame earnings for everything in the quarter.",
        )
    )

    # -- Evidence floors -----------------------------------------------------
    min_bars_for_outcome: OutcomeThreshold = field(
        default_factory=lambda: _t(
            2.0,
            STRUCTURAL,
            "An excursion needs an entry bar and at least one bar after it. "
            "Below that there is no series to measure, and reporting zeros "
            "would be indistinguishable from a setup that genuinely did "
            "not move.",
        )
    )

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
    def from_definition(cls, definition: dict[str, Any]) -> OutcomeThresholds:
        stored = definition.get("thresholds", {})
        template = cls()
        overrides = {}
        for name in cls.names():
            if name not in stored:
                continue
            existing = getattr(template, name)
            overrides[name] = OutcomeThreshold(
                value=float(stored[name]), kind=existing.kind, rationale=existing.rationale
            )
        return cls(**overrides)


@dataclass(frozen=True, slots=True)
class OutcomeConfig:
    """The module's complete, versioned configuration."""

    name: str = "argus-outcome"
    thresholds: OutcomeThresholds = field(default_factory=OutcomeThresholds)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success_definition": SUCCESS_DEFINITION,
            "thresholds": self.thresholds.as_dict(),
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"


def publish_outcome_snapshot(
    connection: Connection,
    config: OutcomeConfig | None = None,
    *,
    as_of: datetime,
    description: str | None = None,
) -> UUID:
    """Get or create the `data_snapshot` row an outcome will cite.

    The snapshot pins **both** the PIT cutoff and the success definition,
    which is what makes a stored outcome reproducible: re-running against
    the same snapshot sees exactly the rows knowable at its cutoff and
    applies exactly the criterion that labelled it. Module 11's
    `publish_similarity_configuration` established the pattern; the reason
    is stronger here, because a changed criterion silently relabels the
    dataset every later statistic is built on.

    Idempotent by checksum, matching Modules 08 to 13.
    """
    config = config or OutcomeConfig()
    checksum = hashlib.sha256(
        f"{config.content_checksum()}|{as_of.isoformat()}".encode()
    ).hexdigest()

    existing = connection.execute(
        select(data_snapshot.c.id).where(data_snapshot.c.content_checksum == checksum).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        data_snapshot.insert()
        .values(
            version_label=f"{config.version_label()}@{as_of.date().isoformat()}",
            as_of_time=as_of,
            definition=config.definition(),
            content_checksum=checksum,
            description=description or "Unvalidated placeholder success criterion (Module 15).",
            published_at=datetime.now(UTC),
        )
        .returning(data_snapshot.c.id)
    ).scalar_one()
