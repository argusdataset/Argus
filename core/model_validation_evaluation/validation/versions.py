"""Correction 3: making "replayed under the wrong version" impossible to do quietly.

## The failure this exists to prevent

Module 14's report found it, and it is subtle. A correction in that module
changed how `pattern_quality` is computed. `pattern_quality` feeds
`argus_score`, and `argus_score` gates qualification — so the correction
moved the qualification boundary. Nothing was renamed, nothing was
republished, and no error was raised.

Now replay 2015 with today's code, citing the `target_model_version_id`
recorded on 2015's signals. Every stored artefact will say those results
were produced under that version. They were not. They were produced under
this code, which computes a different number, and there is no longer any
way to tell the two apart — the corrupted comparison is invisible *and*
permanent, because the version ID is the only handle on "what produced
this".

So the rule, from the module brief verbatim:

> **any historical replay must use the `target_model_version` (and
> `scoring_configuration`, `feature_schema_version`, etc.) that was
> actually in effect for the period being replayed, or explicitly publish
> and use a new version if intentionally re-scoring history under current
> code.** Never silently reuse a recorded version ID across a code change.

## How it is enforced

Two independent checks, because they catch different mistakes.

**Code drift** (`code_drift`). Every versioned configuration in ARGUS is
published idempotently by `content_checksum` — Modules 08-13 all do this.
So the check is exact rather than heuristic: recompute the checksum of the
config object the replay is *about to use*, and compare it against the
checksum stored on the row whose ID the replay cites. A difference means
the code no longer produces what that row describes. This is the check
that would have caught Module 14's correction, at the next replay,
automatically.

**Period drift** (`period_drift`). Read the versions the artefacts already
in `[period_start, period_end]` were recorded under, and compare them
against the versions the replay proposes. A difference means the replay is
about to write results into a period that was scored under something else,
which makes the period's results a mixture nobody can decompose afterwards.

## The escape hatch, and why it is not one

A deliberate re-score of history under current code is legitimate — it is
how a model gets compared against its predecessor. `ReplayIntent.RESCORE`
permits it, and then applies a check of its own: the re-score must cite a
`target_model_version_id` that is **not** one already recorded in the
period. Reusing the recorded ID and calling it a re-score is exactly the
silent reuse the rule forbids, so it is refused under both intents. There
is no combination of arguments that gets a mismatched version past this
module without publishing a new one first.

Refusal is a raised `VersionMismatch`, not a returned flag. A caller that
wants the findings without the exception calls `check_versions` directly;
`require_consistent_versions` is what the replay engine uses, and it stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.scoring.engine import Lineage
from infra.db.schema.intelligence import signals
from infra.db.schema.setups import setups
from infra.db.schema.versioning import (
    detection_configuration,
    feature_schema_version,
    scoring_configuration,
    target_model_version,
)


class ReplayIntent(StrEnum):
    """What the caller means by running this replay.

    REPLAY is the default and the strict one: reproduce what the pipeline
    would have produced at the time. RESCORE is the deliberate,
    stated-out-loud alternative — score history under current code, under
    a newly published version, knowing the results are not comparable to
    the originals.
    """

    REPLAY = "REPLAY"
    RESCORE = "RESCORE"


CODE_DRIFT = "code_drift"
PERIOD_DRIFT = "period_drift"
REUSED_VERSION = "reused_version"
MISSING_VERSION = "missing_version"


class VersionedConfig(Protocol):
    """Any of ARGUS's publishable configuration objects.

    Structural typing, matching the `TargetModel` / `AnalogueCounter` /
    `Renderer` seams: Modules 08-13 each grew this shape independently,
    and nothing needs them to share a base class.
    """

    def content_checksum(self) -> str: ...

    def version_label(self) -> str: ...


#: Which lineage field each version table backs, for the code-drift check.
#: `universe_version` and `data_snapshot` are deliberately absent — they
#: describe *data*, not code, and there is no config object whose checksum
#: could disagree with them. They are still covered by the period check.
CODE_BACKED_VERSIONS: dict[str, Any] = {
    "target_model_version_id": target_model_version,
    "feature_schema_version_id": feature_schema_version,
    "scoring_configuration_id": scoring_configuration,
    "detection_configuration_id": detection_configuration,
}

#: Every lineage field, for the period check.
LINEAGE_FIELDS: tuple[str, ...] = (
    "target_model_version_id",
    "feature_schema_version_id",
    "scoring_configuration_id",
    "detection_configuration_id",
    "universe_version_id",
    "data_snapshot_id",
)


class VersionMismatch(RuntimeError):
    """A replay was refused because its versions do not describe its code."""

    def __init__(self, report: ConsistencyReport) -> None:
        super().__init__(report.summary())
        self.report = report


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason a replay is not consistent."""

    kind: str
    subject: str
    recorded: str | None
    proposed: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "recorded": self.recorded,
            "proposed": self.proposed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """Whether this replay may proceed, and what was checked."""

    intent: ReplayIntent
    period_start: datetime
    period_end: datetime
    findings: list[Finding] = field(default_factory=list)
    #: Lineage fields that were checked against a recorded period version.
    #: Empty when the period holds no prior artefacts — see `checked_period`.
    checked_period: bool = False

    @property
    def consistent(self) -> bool:
        return not self.findings

    def of_kind(self, kind: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.kind == kind]

    def summary(self) -> str:
        if self.consistent:
            return (
                f"{self.intent.value} over "
                f"{self.period_start.date()}..{self.period_end.date()} is version-consistent."
            )
        lines = [
            f"{self.intent.value} over "
            f"{self.period_start.date()}..{self.period_end.date()} refused; "
            f"{len(self.findings)} version inconsistency(ies):"
        ]
        lines += [f"  - [{f.kind}] {f.subject}: {f.detail}" for f in self.findings]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "consistent": self.consistent,
            "checked_period": self.checked_period,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def check_versions(
    connection: Connection,
    *,
    lineage: Lineage,
    period_start: datetime,
    period_end: datetime,
    configs: dict[str, VersionedConfig],
    intent: ReplayIntent = ReplayIntent.REPLAY,
) -> ConsistencyReport:
    """Everything wrong with running this lineage over this period. Never raises.

    `configs` maps a lineage field name to the configuration object the
    replay will actually use — `{"scoring_configuration_id":
    ScoringConfig(), ...}`. A field absent from the mapping is not
    checked for code drift, and the report says so rather than passing it
    silently: an unchecked version is not a verified one.
    """
    findings: list[Finding] = []
    findings += _code_drift(connection, lineage=lineage, configs=configs)

    recorded = recorded_versions(connection, period_start=period_start, period_end=period_end)
    findings += _period_drift(lineage=lineage, recorded=recorded, intent=intent)

    return ConsistencyReport(
        intent=intent,
        period_start=period_start,
        period_end=period_end,
        findings=findings,
        checked_period=bool(recorded),
    )


def require_consistent_versions(
    connection: Connection,
    *,
    lineage: Lineage,
    period_start: datetime,
    period_end: datetime,
    configs: dict[str, VersionedConfig],
    intent: ReplayIntent = ReplayIntent.REPLAY,
) -> ConsistencyReport:
    """`check_versions`, but a refusal stops the caller.

    This is what the replay engine calls, before anything is written.
    """
    report = check_versions(
        connection,
        lineage=lineage,
        period_start=period_start,
        period_end=period_end,
        configs=configs,
        intent=intent,
    )
    if not report.consistent:
        raise VersionMismatch(report)
    return report


def recorded_versions(
    connection: Connection, *, period_start: datetime, period_end: datetime
) -> dict[str, set[UUID]]:
    """Which versions the artefacts already in this period were produced under.

    Reads `signals` (Module 13's output, which carries all six lineage
    columns) and `setups` (Module 14's, which carries five of them since
    migration 0007). Returns a set per field, because a period legitimately
    spans several versions — the point is to compare against what is there,
    not to assume there is one answer.
    """
    found: dict[str, set[UUID]] = {name: set() for name in LINEAGE_FIELDS}

    signal_columns = [signals.c[name] for name in LINEAGE_FIELDS]
    for row in connection.execute(
        select(*signal_columns)
        .where(signals.c.event_time >= period_start, signals.c.event_time <= period_end)
        .distinct()
    ):
        for name, value in zip(LINEAGE_FIELDS, row, strict=True):
            if value is not None:
                found[name].add(value)

    setup_fields = tuple(name for name in LINEAGE_FIELDS if name in setups.c)
    setup_columns = [setups.c[name] for name in setup_fields]
    for row in connection.execute(
        select(*setup_columns)
        .where(setups.c.detected_at >= period_start, setups.c.detected_at <= period_end)
        .distinct()
    ):
        for name, value in zip(setup_fields, row, strict=True):
            if value is not None:
                found[name].add(value)

    return {name: values for name, values in found.items() if values}


# --------------------------------------------------------------------------
# The two checks
# --------------------------------------------------------------------------


def _code_drift(
    connection: Connection, *, lineage: Lineage, configs: dict[str, VersionedConfig]
) -> list[Finding]:
    """Does each cited version row still describe the code about to run?"""
    findings: list[Finding] = []

    for field_name, table in CODE_BACKED_VERSIONS.items():
        version_id = getattr(lineage, field_name)
        config = configs.get(field_name)
        if config is None:
            findings.append(
                Finding(
                    kind=MISSING_VERSION,
                    subject=field_name,
                    recorded=str(version_id),
                    proposed=None,
                    detail=(
                        "No configuration object supplied for this version, so whether "
                        "the code still produces what the recorded row describes could "
                        "not be checked. An unchecked version is not a verified one."
                    ),
                )
            )
            continue

        stored = connection.execute(
            select(table.c.content_checksum, table.c.version_label).where(table.c.id == version_id)
        ).one_or_none()
        if stored is None:
            findings.append(
                Finding(
                    kind=MISSING_VERSION,
                    subject=field_name,
                    recorded=str(version_id),
                    proposed=config.version_label(),
                    detail=(
                        f"{table.name} has no row with id {version_id}. The replay cites "
                        "a version that was never published."
                    ),
                )
            )
            continue

        current = config.content_checksum()
        if stored.content_checksum != current:
            findings.append(
                Finding(
                    kind=CODE_DRIFT,
                    subject=field_name,
                    recorded=stored.version_label,
                    proposed=config.version_label(),
                    detail=(
                        f"{table.name} row {stored.version_label} has checksum "
                        f"{stored.content_checksum}, but the configuration this replay "
                        f"would run has checksum {current}. The code no longer produces "
                        "what that version describes. Publish the current configuration "
                        "and cite the new ID, or check out the code that produced the "
                        "recorded one."
                    ),
                )
            )

    return findings


def _period_drift(
    *, lineage: Lineage, recorded: dict[str, set[UUID]], intent: ReplayIntent
) -> list[Finding]:
    """Does the proposed lineage match what the period already holds?"""
    findings: list[Finding] = []

    for field_name in LINEAGE_FIELDS:
        in_period = recorded.get(field_name)
        if not in_period:
            continue  # Nothing recorded here yet; nothing to contradict.

        proposed = getattr(lineage, field_name)
        if proposed in in_period:
            if intent is ReplayIntent.RESCORE and field_name == "target_model_version_id":
                findings.append(
                    Finding(
                        kind=REUSED_VERSION,
                        subject=field_name,
                        recorded=str(proposed),
                        proposed=str(proposed),
                        detail=(
                            "A deliberate re-score must publish a new "
                            "target_model_version. Reusing the one the period was "
                            "already scored under is the silent reuse this check "
                            "exists to prevent — afterwards nothing distinguishes "
                            "the original results from the re-scored ones."
                        ),
                    )
                )
            continue

        if intent is ReplayIntent.RESCORE:
            continue  # A stated re-score is allowed to differ. That is the point.

        findings.append(
            Finding(
                kind=PERIOD_DRIFT,
                subject=field_name,
                recorded=", ".join(sorted(str(value) for value in in_period)),
                proposed=str(proposed),
                detail=(
                    "This period's existing results were produced under a different "
                    f"{field_name}. Replaying under the proposed one would mix two "
                    "populations in a window nobody can decompose afterwards. Use the "
                    "version that was in effect, or state ReplayIntent.RESCORE and "
                    "publish a new target model version."
                ),
            )
        )

    return findings
