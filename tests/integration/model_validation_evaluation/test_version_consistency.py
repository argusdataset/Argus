"""Correction 3: a replay under versions that don't describe its code is refused.

Module 14's report found the failure this prevents, and the important
thing about it is that it is *silent*. A correction there changed how
`pattern_quality` is computed; `argus_score` gates qualification; so the
qualification boundary moved. Nothing was renamed and nothing raised. A
later replay of 2015 citing 2015's `target_model_version_id` would produce
results attributed to a version that never produced them, and afterwards
there would be no way to tell.

Every test here is a way that could happen, and the assertion is that it
does not.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection

from core.market_state.thresholds import (
    MAGNITUDE,
    MarketStateConfig,
    StateThresholds,
    Threshold,
)
from core.model_validation_evaluation.validation.replay import (
    ModuleConfigs,
    ReplayRequest,
    replay,
)
from core.model_validation_evaluation.validation.versions import (
    CODE_DRIFT,
    MISSING_VERSION,
    PERIOD_DRIFT,
    REUSED_VERSION,
    ReplayIntent,
    VersionMismatch,
    check_versions,
    recorded_versions,
    require_consistent_versions,
)
from core.scoring.config import ScoringConfig
from core.scoring.engine import Lineage, score_candidate
from core.scoring.persistence import write_signal
from tests.integration.model_validation_evaluation.conftest import (
    PERIOD_END,
    PERIOD_START,
)
from tests.unit.scoring.factories import adequate, scoring_inputs


def _check(connection, lineage, modules, intent=ReplayIntent.REPLAY):
    return check_versions(
        connection,
        lineage=lineage,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        configs=modules.for_version_check(),
        intent=intent,
    )


def test_a_lineage_published_from_the_current_code_is_consistent(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    """The baseline. Every other test here is a departure from it."""
    report = _check(connection, lineage, modules)

    assert report.consistent
    assert report.findings == []
    assert "version-consistent" in report.summary()


def test_a_replay_is_refused_when_the_code_no_longer_matches_the_version(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    """Module 14's exact scenario, reproduced.

    `target-model-v1`'s thresholds are changed in code — standing in for
    any correction that alters what the model computes — while the replay
    still cites the version published before the change. That is the
    silent corruption, and it stops here.
    """
    drifted = replace(
        modules,
        market_state=MarketStateConfig(
            states=StateThresholds(
                downtrend_lower_low_frequency=Threshold(
                    value=0.99,
                    kind=MAGNITUDE,
                    rationale="stands in for a code change that moved the boundary",
                )
            )
        ),
    )

    report = _check(connection, lineage, drifted)

    assert not report.consistent
    findings = report.of_kind(CODE_DRIFT)
    assert [finding.subject for finding in findings] == ["target_model_version_id"]
    assert "no longer produces what that version describes" in findings[0].detail


def test_the_refusal_stops_the_replay_before_anything_is_written(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, register
):
    """A refusal after a partial write would be the worst of both."""
    from sqlalchemy import func, select

    from infra.db.schema.validation import model_validation_runs

    drifted = replace(
        modules,
        scoring=ScoringConfig(name="argus-scoring-changed-by-a-code-change"),
    )
    before = connection.execute(
        select(func.count()).select_from(model_validation_runs)
    ).scalar_one()

    with pytest.raises(VersionMismatch) as exc_info:
        replay(
            connection,
            ReplayRequest(
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                lineage=lineage,
                security_ids=[register("DRIFT")],
            ),
            modules=drifted,
        )

    after = connection.execute(select(func.count()).select_from(model_validation_runs)).scalar_one()
    assert after == before, "no run row may be opened for a refused replay"
    assert exc_info.value.report.of_kind(CODE_DRIFT)


def test_a_version_that_was_never_published_is_refused_rather_than_ignored(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    invented = replace(lineage, target_model_version_id=uuid4())
    report = _check(connection, invented, modules)

    findings = report.of_kind(MISSING_VERSION)
    assert [finding.subject for finding in findings] == ["target_model_version_id"]
    assert "never published" in findings[0].detail


def test_a_version_nobody_supplied_a_config_for_is_reported_not_passed(
    connection: Connection, lineage: Lineage
):
    """An unchecked version is not a verified one.

    The tempting shortcut is to skip fields the caller did not describe.
    That would make the check pass for a replay that supplied nothing at
    all, which is the emptiest possible form of "consistent".
    """
    report = check_versions(
        connection,
        lineage=lineage,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        configs={},
    )

    assert len(report.of_kind(MISSING_VERSION)) == 4
    assert not report.consistent


def test_a_period_already_scored_under_another_version_refuses_a_plain_replay(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    register,
    other_target_model: UUID,
):
    """The second check: what the period already holds.

    A signal exists in the period under one target model. Replaying the
    same period under a different one would mix two populations in a
    window nobody could decompose afterwards.
    """
    security_id = register("PERIOD")
    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START + timedelta(days=7),
        lineage=lineage,
    )
    write_signal(connection, signal)

    proposed = replace(lineage, target_model_version_id=other_target_model)
    report = _check(connection, proposed, modules)

    drift = report.of_kind(PERIOD_DRIFT)
    assert [finding.subject for finding in drift] == ["target_model_version_id"]
    assert "state ReplayIntent.RESCORE" in drift[0].detail


def test_a_stated_rescore_is_allowed_to_use_a_different_version(
    connection: Connection,
    lineage: Lineage,
    modules: ModuleConfigs,
    register,
    other_target_model: UUID,
):
    """The escape hatch, working as intended.

    The alternate model is published from a genuinely different config, so
    its checksum is its own; the caller says RESCORE out loud; the period
    check steps aside. This is how a model is compared against its
    predecessor.
    """
    security_id = register("RESCORE")
    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START + timedelta(days=7),
        lineage=lineage,
    )
    write_signal(connection, signal)

    proposed = replace(lineage, target_model_version_id=other_target_model)
    alternate = replace(
        modules,
        market_state=MarketStateConfig(target_model_name="target-model-v1-alternate"),
    )

    report = _check(connection, proposed, alternate, intent=ReplayIntent.RESCORE)

    assert report.consistent, report.summary()
    assert report.checked_period


def test_a_rescore_that_reuses_the_recorded_version_is_still_refused(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, register
):
    """The hole an escape hatch usually leaves, closed.

    Calling it a re-score does not make reusing the period's own version
    ID acceptable — afterwards nothing would distinguish the original
    results from the re-scored ones, which is the precise thing the rule
    forbids. There is no argument combination that gets this through.
    """
    security_id = register("REUSE")
    signal = score_candidate(
        scoring_inputs(security_id, cross=adequate()),
        as_of=PERIOD_START + timedelta(days=7),
        lineage=lineage,
    )
    write_signal(connection, signal)

    report = _check(connection, lineage, modules, intent=ReplayIntent.RESCORE)

    reused = report.of_kind(REUSED_VERSION)
    assert [finding.subject for finding in reused] == ["target_model_version_id"]
    assert "must publish a new target_model_version" in reused[0].detail
    assert not report.consistent


def test_a_rescore_does_not_excuse_code_drift(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, other_target_model: UUID
):
    """RESCORE relaxes the period check only.

    A re-score still has to cite a version that describes the code doing
    the re-scoring; otherwise the new results are as unattributable as
    the old ones would have been.
    """
    proposed = replace(lineage, target_model_version_id=other_target_model)
    report = _check(connection, proposed, modules, intent=ReplayIntent.RESCORE)

    assert report.of_kind(CODE_DRIFT)
    assert not report.consistent


def test_an_empty_period_is_consistent_and_says_it_checked_nothing(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    """The first replay of a period has nothing to contradict.

    `checked_period` is False rather than the report claiming a
    verification it did not perform.
    """
    report = _check(connection, lineage, modules)

    assert report.consistent
    assert not report.checked_period


def test_recorded_versions_reads_both_signals_and_setups(
    connection: Connection, lineage: Lineage, register, seed_setup
):
    """Setups carry five of the six lineage columns since migration 0007.

    Reading only `signals` would miss a period in which setups were opened
    but nothing scored — which, given Module 14's bootstrap analysis, is
    the *normal* state of the system today.
    """
    seed_setup(register("SETUPONLY"))

    found = recorded_versions(connection, period_start=PERIOD_START, period_end=PERIOD_END)

    assert found["target_model_version_id"] == {lineage.target_model_version_id}
    assert found["feature_schema_version_id"] == {lineage.feature_schema_version_id}
    # No signals were written, so no scoring configuration is recorded.
    assert "scoring_configuration_id" not in found


def test_require_consistent_versions_raises_with_the_whole_report_attached(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs
):
    """A refusal has to be actionable, which means naming every problem
    rather than the first one."""
    empty = replace(lineage, feature_schema_version_id=uuid4())

    with pytest.raises(VersionMismatch) as exc_info:
        require_consistent_versions(
            connection,
            lineage=empty,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            configs=modules.for_version_check(),
        )

    assert exc_info.value.report.of_kind(MISSING_VERSION)
    assert "refused" in str(exc_info.value)


def test_the_data_snapshot_and_universe_are_period_checked_but_not_code_checked(
    connection: Connection, lineage: Lineage, modules: ModuleConfigs, register, seed_setup
):
    """They describe data, not code — there is no config whose checksum
    could disagree with them. Their protection is the period check."""
    from dataclasses import replace as _replace

    seed_setup(register("DATACHECK"))
    proposed = _replace(lineage, universe_version_id=uuid4())

    report = _check(connection, proposed, modules)

    assert [finding.subject for finding in report.of_kind(PERIOD_DRIFT)] == ["universe_version_id"]
    assert not report.of_kind(CODE_DRIFT)
