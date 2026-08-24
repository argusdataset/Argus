"""Assembling one evaluation report, and writing it against a validation run.

## The report is one row, and it cites its own inputs

`model_evaluation_reports` carries five headline metrics as columns —
they are what runs get compared and sorted on — and everything else in a
JSONB `metrics` payload whose shape this module owns. The split is Module
03's and it is the right one: a per-regime breakdown crossed with score
buckets is not a column, and a precision that cannot be ordered against
another run's precision is not much use as JSONB.

What travels in the payload beyond the numbers is the part that keeps the
report honest: the dataset's bounds (`as_of`, period, snapshot), the
configuration's version label, and the sufficiency of every breakdown. A
stored report that did not say which labelling it evaluated would become
un-interpretable the first time Module 15's success criterion is revised —
which migration 0007 exists to make possible, so it will happen.

## Nothing here decides whether the numbers are good

`evaluate` computes and stores. Whether a run's results may be published
is `review.py`'s question and a human's answer. A report is written
regardless of what it says, including when it says the sample was too
thin to support any claim at all — which is what it will say until a real
scan has run, and is the correct output rather than a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from core.model_validation_evaluation.evaluation.breakdowns import (
    Breakdown,
    by_evidence_scope,
    by_regime,
    by_regime_and_bucket,
    sufficiency_note,
)
from core.model_validation_evaluation.evaluation.buckets import (
    CalibrationCurve,
    MonotonicityAnalysis,
    analyse_monotonicity,
    calibration_curve,
)
from core.model_validation_evaluation.evaluation.config import EvaluationConfig
from core.model_validation_evaluation.evaluation.dataset import (
    EvaluationDataset,
    load_evaluation_dataset,
)
from core.model_validation_evaluation.evaluation.metrics import MetricSuite, compute_metrics
from core.model_validation_evaluation.evaluation.walk_forward import (
    WalkForwardAnalysis,
    walk_forward,
)
from core.model_validation_evaluation.validation.runs import load_run
from infra.db.schema.validation import model_evaluation_reports


class EvaluationRefused(ValueError):
    """An evaluation was asked for against something it cannot evaluate."""


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """One run's complete evaluation. Everything a recalibration needs."""

    run_id: UUID
    dataset: EvaluationDataset
    overall: MetricSuite
    monotonicity: MonotonicityAnalysis
    calibration: CalibrationCurve
    walk_forward: WalkForwardAnalysis
    regime: Breakdown
    evidence_scope: Breakdown
    regime_by_bucket: dict[str, Any] = field(default_factory=dict)
    config_version: str = ""
    report_id: UUID | None = None

    def headline(self) -> dict[str, Any]:
        """The five columns, plus the sample they came from."""
        return {
            "precision": self.overall.precision,
            "recall": self.overall.recall,
            "hit_rate": self.overall.hit_rate,
            "false_positive_rate": self.overall.false_positive_rate,
            "expectancy": self.overall.expectancy,
            "sample_size": self.overall.sample_size,
        }

    def metrics_payload(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "dataset": self.dataset.bounds(),
            "overall": self.overall.as_dict(),
            "score_monotonicity": self.monotonicity.as_dict(),
            "calibration": self.calibration.as_dict(),
            "walk_forward": self.walk_forward.as_dict(),
            "by_regime": self.regime.as_dict(),
            "by_evidence_scope": self.evidence_scope.as_dict(),
            "by_regime_and_bucket": self.regime_by_bucket,
            "notes": {
                "regime": sufficiency_note(self.regime),
                "evidence_scope": sufficiency_note(self.evidence_scope),
                "monotonicity": self.monotonicity.summary(),
                "walk_forward": self.walk_forward.summary(),
            },
            "calibration_status": "UNVALIDATED_PLACEHOLDERS",
        }

    def summary(self) -> str:
        lines = [
            f"Run {self.run_id}: {self.overall.sample_size} evaluated setup(s), "
            f"{self.overall.confusion.total} resolved.",
            f"  {self.monotonicity.summary()}",
            f"  {self.walk_forward.summary()}",
            f"  {sufficiency_note(self.regime)}",
        ]
        if self.overall.reportable:
            lines.append(
                f"  precision={_fmt(self.overall.precision)} "
                f"recall={_fmt(self.overall.recall)} "
                f"hit_rate={_fmt(self.overall.hit_rate)} "
                f"expectancy={_fmt(self.overall.expectancy)} (benchmark-relative)"
            )
        else:
            lines.append(
                "  No headline metrics: the population is below the sample floor. "
                "Expected until a real historical scan has run."
            )
        return "\n".join(lines)


def evaluate(
    connection: Connection,
    run_id: UUID,
    *,
    as_of: datetime,
    config: EvaluationConfig | None = None,
    data_snapshot_id: UUID | None = None,
    persist: bool = True,
) -> EvaluationReport:
    """Evaluate one validation run's results and, by default, store the report.

    The run's own `period_start` / `period_end` bound the dataset, so an
    evaluation cannot accidentally measure a run against setups from
    outside the period it replayed. Its `universe_version_id` narrows it
    further, which is what keeps two runs over the same years but
    different universes from being scored against each other's setups.
    """
    config = config or EvaluationConfig()
    run = load_run(connection, run_id)
    if run is None:
        raise EvaluationRefused(f"No validation run {run_id}.")

    dataset = load_evaluation_dataset(
        connection,
        as_of=as_of,
        period_start=run.period_start,
        period_end=run.period_end,
        data_snapshot_id=data_snapshot_id,
        universe_version_id=run.lineage.universe_version_id,
    )

    report = build_report(
        dataset,
        run_id=run_id,
        period_start=run.period_start,
        period_end=run.period_end,
        config=config,
    )
    if not persist:
        return report
    return _store(connection, report)


def build_report(
    dataset: EvaluationDataset,
    *,
    run_id: UUID,
    period_start: datetime,
    period_end: datetime,
    config: EvaluationConfig | None = None,
) -> EvaluationReport:
    """Every analysis, over one already-loaded dataset. No I/O.

    Separated from `evaluate` for the reason Modules 09 and 10 separate
    their pure stages: an analysis that could reach the database is one
    nobody can test against a constructed population, and constructed
    populations are how the monotonicity analysis is proved to fail when
    it should.
    """
    config = config or EvaluationConfig()
    frame = dataset.frame

    return EvaluationReport(
        run_id=run_id,
        dataset=dataset,
        overall=compute_metrics(frame, config),
        monotonicity=analyse_monotonicity(frame, config),
        calibration=calibration_curve(frame, config),
        walk_forward=walk_forward(
            dataset, period_start=period_start, period_end=period_end, config=config
        ),
        regime=by_regime(frame, config),
        evidence_scope=by_evidence_scope(frame, config),
        regime_by_bucket=by_regime_and_bucket(frame, config),
        config_version=config.version_label(),
    )


def _store(connection: Connection, report: EvaluationReport) -> EvaluationReport:
    headline = report.headline()
    report_id = connection.execute(
        model_evaluation_reports.insert()
        .values(
            model_validation_run_id=report.run_id,
            precision=headline["precision"],
            recall=headline["recall"],
            hit_rate=headline["hit_rate"],
            false_positive_rate=headline["false_positive_rate"],
            expectancy=headline["expectancy"],
            sample_size=headline["sample_size"],
            metrics=report.metrics_payload(),
        )
        .returning(model_evaluation_reports.c.id)
    ).scalar_one()

    return EvaluationReport(
        run_id=report.run_id,
        dataset=report.dataset,
        overall=report.overall,
        monotonicity=report.monotonicity,
        calibration=report.calibration,
        walk_forward=report.walk_forward,
        regime=report.regime,
        evidence_scope=report.evidence_scope,
        regime_by_bucket=report.regime_by_bucket,
        config_version=report.config_version,
        report_id=report_id,
    )


def _fmt(value: float | None) -> str:
    return "None" if value is None else f"{value:.4f}"
