"""Replaying the whole pipeline across history, in batch, PIT-correctly.

This is the first place in ARGUS where Modules 06-15 are wired end to end.
Every module was built to be driven this way — `as_of` is a plain argument
throughout, per `CROSS_CUTTING_REQUIREMENTS.md`, and nothing reads a wall
clock — so this module is orchestration, not adaptation. Nothing below
reaches into another module's internals or reimplements its logic.

## What "batch" has to mean at real scale

A real run is ~10,000 securities over ~15-30 years. That is the number
that decides the shape of this file, even though every test here runs at
fixture scale — the same discipline Module 08 followed when it built
genuinely vectorized feature computation rather than a loop that happened
to pass.

The cost model this engine is built to:

- **Per scan date, per universe**: one panel load and one vectorized
  feature pass (Module 08), one cross-sectional ranking (Module 09), one
  classification over a single (securities x features) frame (Module 10),
  one transition write, one case load (Module 11). None of these is a
  per-security round trip, and none becomes one at 10,000 names.
- **Per scan date, per candidate**: risk context (Module 12) and
  similarity (Module 11) are per candidate by their own modules'
  deliberate design, and run only over the pool Module 09 has already
  reduced the universe to. Module 11's case set is loaded **once per scan
  date** and passed in, which is the difference between one query and one
  per candidate.
- **Scan dates**: `scan_step_days` apart, not daily. See `config.py`.

The two things that would quietly destroy this — computing features one
security at a time, and reloading the case set per candidate — are both
prevented by structure rather than by a comment, and
`tests/.../test_batch_scale.py` counts the queries to prove it.

## Chunking is a memory bound, not a semantic one

Feature computation runs in chunks of `batch_size` securities because the
price panel is a dense frame over the maximum lookback and one unchunked
pass at full universe size is a multi-gigabyte allocation. Chunking
provably cannot change a result: each security's features come from its
own history plus shared benchmarks, and the one genuinely cross-sectional
step — Module 09's ranking — runs once over the merged result, never per
chunk. A test asserts identical output at two chunk sizes.

## Version consistency

`replay` refuses before writing anything if the versions it was handed do
not describe the code about to run, or do not match what the period was
already scored under. See `versions.py` — that refusal is Correction 3,
and it is the reason this function takes a `configs` mapping rather than
letting each module default its own configuration silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Connection

from core.candidate_detection.config import DetectionConfig
from core.candidate_detection.detection import detect_candidates
from core.candidate_detection.eligibility.runner import evaluate_eligibility
from core.candidate_detection.persistence import write_eligibility_results
from core.candidate_detection.pool import CandidatePool
from core.data_validation.calendar import is_trading_day
from core.data_validation.universe import list_universe_members_as_of
from core.feature_engine.engine import compute_features_batch
from core.feature_engine.persistence import write_feature_vectors
from core.feature_engine.spec import FeatureSpec
from core.feature_engine.vector import BatchFeatureResult
from core.historical_similarity.cases import load_cases
from core.historical_similarity.config import SimilarityConfig
from core.historical_similarity.engine import find_similar_setups
from core.historical_similarity.persistence import write_similarity_results
from core.lifecycle.config import LifecycleConfig
from core.lifecycle.engine import CandidateObservation, advance_lifecycle
from core.market_state.classifier import (
    ClassificationResult,
    StateAssignment,
    classify_states,
)
from core.market_state.target_model_matching.interface import (
    TargetModelAssessment,
    assessment_from_evidence,
)
from core.market_state.thresholds import MarketStateConfig
from core.market_state.transitions import backward_transition_count, record_transitions
from core.model_validation_evaluation.validation.config import ValidationConfig
from core.model_validation_evaluation.validation.runs import finish_run, start_run
from core.model_validation_evaluation.validation.versions import (
    ConsistencyReport,
    ReplayIntent,
    VersionedConfig,
    require_consistent_versions,
)
from core.outcome_tracking.config import OutcomeConfig
from core.outcome_tracking.engine import process_concluded_setups
from core.risk_context.assessment import assess_risk_context
from core.risk_context.config import RiskConfig
from core.scoring.components import ScoringInputs
from core.scoring.config import ScoringConfig
from core.scoring.engine import Lineage, ScoredSignal, score_candidates
from core.scoring.persistence import resolve_signal_id, write_signal
from data.canonical_model.records import CanonicalTimeframe
from infra.db.enums import ValidationRunStatus


class ReplayRefused(ValueError):
    """The replay was asked for something it should not do."""


@dataclass(frozen=True, slots=True)
class ModuleConfigs:
    """One object holding every module's configuration for this replay.

    Explicit rather than defaulted per call site. A replay that let each
    module reach for its own default would be reproducible only for as
    long as nobody changed a default, and the whole point of recording a
    lineage is that "whatever was deployed" is not an answer.
    """

    features: FeatureSpec = field(default_factory=FeatureSpec)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    market_state: MarketStateConfig = field(default_factory=MarketStateConfig)
    similarity: SimilarityConfig = field(default_factory=SimilarityConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    outcome: OutcomeConfig = field(default_factory=OutcomeConfig)

    def for_version_check(self) -> dict[str, VersionedConfig]:
        """The subset whose checksums back a published version row.

        `universe_version` and `data_snapshot` are absent because they
        describe data rather than code — `versions.py` covers those by a
        different check.
        """
        return {
            "target_model_version_id": self.market_state,
            "feature_schema_version_id": self.features,
            "scoring_configuration_id": self.scoring,
            "detection_configuration_id": self.detection,
        }


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """What to replay, over what, under which versions."""

    period_start: datetime
    period_end: datetime
    lineage: Lineage
    intent: ReplayIntent = ReplayIntent.REPLAY
    #: Restrict the replay to these securities. None means "whatever the
    #: universe version says was listed on each scan date", which is the
    #: real-run path; a list is how a test or an investigation narrows it.
    security_ids: list[UUID] | None = None
    benchmark_security_id: UUID | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ScanDateResult:
    """What one replayed scan date produced."""

    as_of: datetime
    universe_size: int
    features_computed: int
    candidates: int
    eligible: int
    signals_scored: int
    signals_written: int
    lifecycle_counts: dict[str, int] = field(default_factory=dict)
    outcomes_recorded: int = 0
    state_distribution: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "universe_size": self.universe_size,
            "features_computed": self.features_computed,
            "candidates": self.candidates,
            "eligible": self.eligible,
            "signals_scored": self.signals_scored,
            "signals_written": self.signals_written,
            "lifecycle_counts": self.lifecycle_counts,
            "outcomes_recorded": self.outcomes_recorded,
            "state_distribution": self.state_distribution,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """A whole replay: the run record, what it checked, and what it produced."""

    run_id: UUID
    request: ReplayRequest
    consistency: ConsistencyReport
    scan_dates: list[ScanDateResult] = field(default_factory=list)
    #: Every signal produced, in scan order. Held in memory because
    #: evaluation reads them immediately; a full-scale run would stream
    #: these to the database and re-read, which is why nothing downstream
    #: requires this list rather than the stored rows.
    signals: list[ScoredSignal] = field(default_factory=list)

    @property
    def scored(self) -> list[ScoredSignal]:
        return [signal for signal in self.signals if signal.argus_score is not None]

    def totals(self) -> dict[str, int]:
        return {
            "scan_dates": len(self.scan_dates),
            "signals_scored": sum(d.signals_scored for d in self.scan_dates),
            "signals_written": sum(d.signals_written for d in self.scan_dates),
            "outcomes_recorded": sum(d.outcomes_recorded for d in self.scan_dates),
            "candidates": sum(d.candidates for d in self.scan_dates),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "period_start": self.request.period_start.isoformat(),
            "period_end": self.request.period_end.isoformat(),
            "intent": self.request.intent.value,
            "consistency": self.consistency.as_dict(),
            "totals": self.totals(),
            "scan_dates": [d.as_dict() for d in self.scan_dates],
            **self.request.lineage.as_dict(),
        }


def scan_dates(
    period_start: datetime, period_end: datetime, *, config: ValidationConfig | None = None
) -> list[datetime]:
    """The dates a replay will visit: trading days, `scan_step_days` apart.

    Trading days only, because a scan on a closed market re-reads the
    previous close and produces a duplicate observation that would then be
    counted twice in every statistic downstream. The step is applied first
    and the result advanced to the next trading day, so a holiday shifts a
    scan rather than deleting it.
    """
    config = config or ValidationConfig()
    settings = config.settings

    if period_end < period_start:
        raise ReplayRefused(
            f"period_end {period_end.isoformat()} precedes period_start {period_start.isoformat()}."
        )

    step = max(int(settings.scan_step_days.value), 1)
    limit = int(settings.max_scan_dates.value)

    dates: list[datetime] = []
    current = period_start
    while current <= period_end:
        moment = _next_trading_moment(current, period_end)
        if moment is None:
            break
        if not dates or moment > dates[-1]:
            dates.append(moment)
        if len(dates) > limit:
            raise ReplayRefused(
                f"This period and step would visit more than {limit} scan dates. "
                "Check the period bounds and scan_step_days — that is usually a "
                "reversed range or a step meant to be larger."
            )
        current = moment + timedelta(days=step)

    return dates


def replay(
    connection: Connection,
    request: ReplayRequest,
    *,
    config: ValidationConfig | None = None,
    modules: ModuleConfigs | None = None,
    analogue_counter: Any = None,
) -> ReplayResult:
    """Replay the pipeline across `request`'s period, writing full lineage.

    Refuses before writing anything if the versions are inconsistent
    (Correction 3). Opens a RUNNING run row, walks the scan dates, and
    closes the run COMPLETED — or FAILED, with the exception re-raised, so
    a crashed replay leaves a row naming what was in flight rather than
    nothing at all.
    """
    config = config or ValidationConfig()
    modules = modules or ModuleConfigs()

    consistency = require_consistent_versions(
        connection,
        lineage=request.lineage,
        period_start=request.period_start,
        period_end=request.period_end,
        configs=modules.for_version_check(),
        intent=request.intent,
    )

    dates = scan_dates(request.period_start, request.period_end, config=config)
    run = start_run(
        connection,
        lineage=request.lineage,
        period_start=request.period_start,
        period_end=request.period_end,
        notes=request.notes,
    )

    results: list[ScanDateResult] = []
    signals: list[ScoredSignal] = []
    try:
        for as_of in dates:
            scan_result, scan_signals = _replay_one_date(
                connection,
                as_of=as_of,
                request=request,
                config=config,
                modules=modules,
                analogue_counter=analogue_counter,
            )
            results.append(scan_result)
            signals.extend(scan_signals)
    except Exception as error:
        finish_run(
            connection,
            run.id,
            status=ValidationRunStatus.FAILED,
            notes=f"{type(error).__name__}: {error}",
        )
        raise

    finish_run(connection, run.id, status=ValidationRunStatus.COMPLETED)
    return ReplayResult(
        run_id=run.id,
        request=request,
        consistency=consistency,
        scan_dates=results,
        signals=signals,
    )


# --------------------------------------------------------------------------
# One scan date
# --------------------------------------------------------------------------


def _replay_one_date(
    connection: Connection,
    *,
    as_of: datetime,
    request: ReplayRequest,
    config: ValidationConfig,
    modules: ModuleConfigs,
    analogue_counter: Any,
) -> tuple[ScanDateResult, list[ScoredSignal]]:
    lineage = request.lineage
    universe = _universe_at(connection, request, as_of=as_of)

    features = compute_features_in_chunks(
        connection,
        universe,
        as_of=as_of,
        spec=modules.features,
        feature_schema_version_id=lineage.feature_schema_version_id,
        chunk_size=config.settings.chunk,
        market_security_id=request.benchmark_security_id,
    )
    write_feature_vectors(connection, features)

    # Ranking is cross-sectional and therefore runs once, over the merged
    # result — never per chunk. Chunking the ranks would rank each chunk
    # against itself and select the top slice of every chunk, which at
    # 10,000 names across five chunks is a different candidate pool.
    pool = detect_candidates(
        features,
        config=modules.detection,
        detection_configuration_id=lineage.detection_configuration_id,
        run_id=uuid4(),
    )

    eligibility = evaluate_eligibility(
        connection,
        pool,
        features,
        lineage.universe_version_id,
        config=modules.detection,
        analogue_counter=analogue_counter,
    )
    write_eligibility_results(connection, eligibility)

    states = classify_states(
        features,
        eligibility=eligibility,
        config=modules.market_state,
        target_model_version_id=lineage.target_model_version_id,
    )
    record_transitions(connection, states)

    scored = _score_pool(
        connection,
        pool=pool,
        features=features,
        eligibility_outcomes=eligibility.outcomes,
        states=states,
        as_of=as_of,
        lineage=lineage,
        modules=modules,
    )

    written, signal_ids = _persist_signals(connection, scored)

    lifecycle = advance_lifecycle(
        connection,
        _observations(connection, scored, states=states, as_of=as_of, signal_ids=signal_ids),
        as_of=as_of,
        lineage=lineage,
        config=modules.lifecycle,
    )

    outcomes = process_concluded_setups(
        connection,
        as_of=as_of,
        data_snapshot_id=lineage.data_snapshot_id,
        config=modules.outcome,
        benchmark_security_id=request.benchmark_security_id,
    )

    result = ScanDateResult(
        as_of=as_of,
        universe_size=len(universe),
        features_computed=len(features.vectors),
        candidates=len(pool),
        eligible=len(eligibility.eligible()),
        signals_scored=len(scored),
        signals_written=written,
        lifecycle_counts={action: count for action, count in lifecycle.counts().items() if count},
        outcomes_recorded=len(outcomes.written),
        state_distribution={
            state.value: count for state, count in states.distribution().items() if count
        },
    )
    return result, scored


def compute_features_in_chunks(
    connection: Connection,
    security_ids: list[UUID],
    *,
    as_of: datetime,
    spec: FeatureSpec,
    feature_schema_version_id: UUID | None,
    chunk_size: int,
    market_security_id: UUID | None = None,
) -> BatchFeatureResult:
    """Module 08's batch pass, bounded in memory, merged back into one result.

    Public because the chunk-equivalence test drives it directly: the
    claim that `batch_size` is operational rather than calibratable is
    only worth making if something checks it.
    """
    if not security_ids:
        return BatchFeatureResult(
            as_of=as_of,
            feature_schema_version_id=feature_schema_version_id,
            timeframe=CanonicalTimeframe.DAILY,
            vectors={},
        )

    size = max(chunk_size, 1)
    chunks = [security_ids[i : i + size] for i in range(0, len(security_ids), size)]

    merged: BatchFeatureResult | None = None
    for chunk in chunks:
        part = compute_features_batch(
            connection,
            chunk,
            as_of,
            spec=spec,
            feature_schema_version_id=feature_schema_version_id,
            market_security_id=market_security_id,
        )
        if merged is None:
            merged = part
            continue
        merged = BatchFeatureResult(
            as_of=merged.as_of,
            feature_schema_version_id=merged.feature_schema_version_id,
            timeframe=merged.timeframe,
            vectors={**merged.vectors, **part.vectors},
            missing_securities={**merged.missing_securities, **part.missing_securities},
        )

    assert merged is not None
    return merged


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _next_trading_moment(current: datetime, period_end: datetime) -> datetime | None:
    """`current`, or the next trading day after it, or None past the end."""
    moment = current
    while moment <= period_end:
        if is_trading_day(_as_date(moment)):
            return moment
        moment += timedelta(days=1)
    return None


def _as_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date() if moment.tzinfo else moment.date()


def _universe_at(connection: Connection, request: ReplayRequest, *, as_of: datetime) -> list[UUID]:
    """Who was listed on this date, per the replay's universe version.

    Not "who is listed today". Module 06 built `universe_membership` as
    intervals precisely so a replay of 2015 sees 2015's listings, and
    using today's would be survivorship bias introduced at the one point
    in the system where it is invisible afterwards.
    """
    if request.security_ids is not None:
        return list(request.security_ids)
    members = list_universe_members_as_of(connection, request.lineage.universe_version_id, as_of)
    return [member.security_id for member in members]


def _score_pool(
    connection: Connection,
    *,
    pool: CandidatePool,
    features: BatchFeatureResult,
    eligibility_outcomes: dict[UUID, Any],
    states: ClassificationResult,
    as_of: datetime,
    lineage: Lineage,
    modules: ModuleConfigs,
) -> list[ScoredSignal]:
    """Assemble scoring inputs for the candidate pool and score them.

    The case set is loaded **once** here and handed to every candidate's
    similarity call. Module 11 accepts `cases=` for exactly this reason;
    omitting it would issue one case query per candidate per scan date,
    which over a real run is the difference between thousands of queries
    and millions.

    The target-model assessment comes from the classification Module 10
    just produced, not from a second `assess()` call. Two calls would be
    two chances to disagree, and passing None instead — the easy mistake
    here — would leave `pattern_quality` permanently unavailable and
    quietly remove a quarter of the score's weight from every replayed
    result.
    """
    candidates = list(pool.candidates)
    if not candidates:
        return []

    cases = load_cases(connection, as_of, lineage.feature_schema_version_id)

    inputs: list[ScoringInputs] = []
    for security_id in candidates:
        vector = features.vectors.get(security_id)
        risk = assess_risk_context(
            connection,
            security_id,
            as_of=as_of,
            features=vector,
            config=modules.risk,
        )
        similarity = find_similar_setups(
            connection,
            security_id,
            vector.features if vector is not None else {},
            as_of,
            lineage.feature_schema_version_id,
            config=modules.similarity,
            data_snapshot_id=lineage.data_snapshot_id,
            cases=cases,
        )
        write_similarity_results(connection, similarity)
        inputs.append(
            ScoringInputs(
                risk=risk,
                features=vector,
                assessment=scoring_assessment(security_id, states.assignments.get(security_id)),
                similarity=similarity,
            )
        )

    return score_candidates(
        inputs,
        as_of=as_of,
        lineage=lineage,
        config=modules.scoring,
        eligibility=eligibility_outcomes,
    )


def scoring_assessment(
    security_id: UUID, assignment: StateAssignment | None
) -> TargetModelAssessment | None:
    """Module 10's assessment of this security, as Module 13 wants it.

    A named function rather than three lines inline, because it is the
    single easiest seam in this module to break silently. `StateAssignment`
    keeps the assessment in serialized form; Module 13's `pattern_quality`
    component reads the object. Passing None instead compiles, runs, and
    produces plausible scores with a quarter of the composite's weight
    quietly missing.

    That break was made deliberately while writing this module and the
    entire integration suite still passed — because today every candidate
    is gated before components are computed (Module 14's bootstrap
    analysis), so no end-to-end path reaches `pattern_quality` at all.
    Hence a unit test, on a function that exists to be unit-testable.
    """
    if assignment is None:
        return None
    return assessment_from_evidence(security_id, assignment.evidence.get("target_model_assessment"))


def _persist_signals(
    connection: Connection, scored: list[ScoredSignal]
) -> tuple[int, dict[UUID, UUID]]:
    """Write every persistable signal, keeping the IDs by security.

    The IDs are what migration 0007's `setups.qualifying_signal_id` needs.
    `write_signal` returns None for a row that already existed, which is a
    real outcome and not an error, so the ID is resolved rather than
    assumed — otherwise a re-run over the same date would silently stop
    populating the join key.
    """
    written = 0
    ids: dict[UUID, UUID] = {}
    for signal in scored:
        if not signal.writes_signal:
            continue
        signal_id = write_signal(connection, signal)
        if signal_id is not None:
            written += 1
        else:
            signal_id = resolve_signal_id(connection, signal)
        if signal_id is not None:
            ids[signal.security_id] = signal_id
    return written, ids


def _observations(
    connection: Connection,
    scored: list[ScoredSignal],
    *,
    states: ClassificationResult,
    as_of: datetime,
    signal_ids: dict[UUID, UUID],
) -> list[CandidateObservation]:
    """Module 14's input: the state, the verdict, and the retreat count.

    `backward_transitions` is read per security. That is one query per
    *candidate* — not per universe member — because only scored
    candidates reach the lifecycle, and Module 10 stores the count rather
    than recomputing it.
    """
    assignments = states.assignments
    return [
        CandidateObservation(
            security_id=signal.security_id,
            state=(
                assignments[signal.security_id].state if signal.security_id in assignments else None
            ),
            signal=signal,
            backward_transitions=backward_transition_count(
                connection, signal.security_id, as_of=as_of
            ),
            signal_id=signal_ids.get(signal.security_id),
        )
        for signal in scored
    ]
