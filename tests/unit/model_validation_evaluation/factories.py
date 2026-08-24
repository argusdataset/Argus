"""Constructed evaluation populations, successes and failures alike.

Module 15 established that failures need equal representation in the case
dataset. It applies with more force here: an evaluation fixture made only
of winners cannot distinguish a metric suite that works from one that
silently drops the losing rows, and every rate in `metrics.py` would come
back 1.0 and look correct.

The shapes are the project's known historical cases (MLSS/SLS/HIVE/ALX/
QBTS-like: a long decline, a quiet base, an awakening, an expansion) plus
synthetic failures — a base that never expanded, one that broke down, one
that expired mid-structure. Nothing here is real market data and nothing
pretends to be; these are populations with known metric answers, which is
what a test needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pandas as pd

from core.model_validation_evaluation.evaluation.dataset import (
    EVALUATION_COLUMNS,
    EvaluationDataset,
)
from infra.db.enums import MarketState, OutcomeStatus

BASE = datetime(2020, 1, 6, tzinfo=UTC)


def setup_row(
    *,
    security_id: UUID | None = None,
    detected_at: datetime = BASE,
    argus_score: float | None = None,
    outcome_status: OutcomeStatus = OutcomeStatus.SUCCESS,
    relative_return: float = 0.18,
    mfe: float = 0.34,
    mae: float = -0.07,
    regime: MarketState = MarketState.UPTREND,
    confidence: float | None = 70.0,
) -> dict[str, object]:
    """One evaluated setup.

    `argus_score=None` means the setup was detected and never qualified —
    which is not a defect in the fixture but the population recall and the
    true negatives are computed from.
    """
    qualified = argus_score is not None
    return {
        "setup_id": uuid4(),
        "security_id": security_id or uuid4(),
        "detected_at": detected_at,
        "concluded_at": detected_at + timedelta(days=60),
        "qualifying_signal_id": uuid4() if qualified else None,
        "argus_score": argus_score,
        "confidence": confidence if qualified else None,
        "opportunity_score": 60.0 if qualified else None,
        "risk_score": 30.0 if qualified else None,
        "signal_event_time": detected_at if qualified else None,
        "outcome_status": outcome_status.value,
        "mfe": mfe,
        "mae": mae,
        "realized_return": relative_return + 0.05,
        "benchmark_relative_return": relative_return,
        "volatility_adjusted_outcome": relative_return / 0.2,
        "time_to_mfe": timedelta(days=30),
        "outcome_window": timedelta(days=60),
        "market_regime_at_outcome": regime.value,
        "review_confidence": None,
        "false_positive_type": None,
        "recorded_at": detected_at + timedelta(days=61),
        "data_snapshot_id": uuid4(),
        "target_model_version_id": uuid4(),
        "universe_version_id": uuid4(),
    }


def frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    """Rows as the frame `load_evaluation_dataset` would have produced."""
    built = pd.DataFrame(rows, columns=list(EVALUATION_COLUMNS))
    for column in ("detected_at", "concluded_at", "recorded_at", "signal_event_time"):
        built[column] = pd.to_datetime(built[column], utc=True, errors="coerce")
    return built


def dataset(
    rows: list[dict[str, object]],
    *,
    as_of: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> EvaluationDataset:
    return EvaluationDataset(
        frame=frame(rows),
        as_of=as_of or datetime(2026, 1, 1, tzinfo=UTC),
        period_start=period_start,
        period_end=period_end,
    )


def monotone_population(per_bucket: int = 25) -> list[dict[str, object]]:
    """A population where the score genuinely orders outcomes.

    Each bucket's mean benchmark-relative return rises with the score, and
    each bucket holds enough setups to clear the default floor. Success
    rate rises with it, so precision and the calibration curve move
    together — which is what a working scoring system looks like and what
    the analysis must confirm rather than assume.
    """
    bands = [(30.0, -0.04), (50.0, 0.01), (65.0, 0.06), (75.0, 0.12), (85.0, 0.20), (95.0, 0.30)]
    rows: list[dict[str, object]] = []
    for index, (score, centre) in enumerate(bands):
        for n in range(per_bucket):
            # A deterministic spread inside the band, so the buckets have
            # variance without the test depending on a random seed.
            offset = ((n % 5) - 2) * 0.01
            rows.append(
                setup_row(
                    detected_at=BASE + timedelta(days=7 * (index * per_bucket + n)),
                    argus_score=score,
                    relative_return=centre + offset,
                    outcome_status=(
                        OutcomeStatus.SUCCESS if centre + offset > 0.05 else OutcomeStatus.FAILED
                    ),
                    mfe=max(centre + offset, 0.0) + 0.10,
                    mae=-0.12 + index * 0.01,
                )
            )
    return rows


def flat_population(per_bucket: int = 25) -> list[dict[str, object]]:
    """The failure case: every bucket performs identically.

    The pattern may still have an edge here — every bucket returns a
    healthy 12% — but the score carries no information about which setup
    to prefer. A monotonicity analysis that passes this population is
    worthless, which is why a test asserts it does not.
    """
    rows: list[dict[str, object]] = []
    for index, score in enumerate((30.0, 50.0, 65.0, 75.0, 85.0, 95.0)):
        for n in range(per_bucket):
            rows.append(
                setup_row(
                    detected_at=BASE + timedelta(days=7 * (index * per_bucket + n)),
                    argus_score=score,
                    relative_return=0.12 + ((n % 3) - 1) * 0.005,
                    outcome_status=OutcomeStatus.SUCCESS,
                )
            )
    return rows


def inverted_population(per_bucket: int = 25) -> list[dict[str, object]]:
    """The worse failure: high scores do *worse* than low ones."""
    bands = [(30.0, 0.28), (50.0, 0.20), (65.0, 0.12), (75.0, 0.05), (85.0, -0.02), (95.0, -0.10)]
    rows: list[dict[str, object]] = []
    for index, (score, centre) in enumerate(bands):
        for n in range(per_bucket):
            rows.append(
                setup_row(
                    detected_at=BASE + timedelta(days=7 * (index * per_bucket + n)),
                    argus_score=score,
                    relative_return=centre,
                    outcome_status=(
                        OutcomeStatus.SUCCESS if centre > 0.05 else OutcomeStatus.FAILED
                    ),
                )
            )
    return rows


def mixed_population(count: int = 60) -> list[dict[str, object]]:
    """Qualified and unqualified, resolved and unresolved, several regimes.

    The realistic shape: most setups never qualify, some that qualify
    fail, some that never qualified would have succeeded, and a slice
    never resolved at all. Every metric in the suite has something to bite
    on here, including the ones that should come back None.
    """
    regimes = (
        MarketState.UPTREND,
        MarketState.DISTRIBUTION,
        MarketState.DOWN_TREND,
    )
    statuses = (
        OutcomeStatus.SUCCESS,
        OutcomeStatus.FAILED,
        OutcomeStatus.EXPIRED,
    )
    rows: list[dict[str, object]] = []
    for n in range(count):
        qualified = n % 3 != 2
        status = statuses[n % 3]
        rows.append(
            setup_row(
                detected_at=BASE + timedelta(days=7 * n),
                argus_score=60.0 + (n % 40) if qualified else None,
                outcome_status=status,
                relative_return=0.20 if status is OutcomeStatus.SUCCESS else -0.06,
                regime=regimes[n % 3],
            )
        )
    return rows


def repeated_security(count: int = 4) -> list[dict[str, object]]:
    """One security with several setups, for the same-asset breakdown."""
    security_id = uuid4()
    return [
        setup_row(
            security_id=security_id,
            detected_at=BASE + timedelta(days=200 * n),
            argus_score=70.0,
            outcome_status=OutcomeStatus.SUCCESS,
        )
        for n in range(count)
    ]
