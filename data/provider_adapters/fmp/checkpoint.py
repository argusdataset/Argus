"""Resumable job checkpoints.

A full-universe historical fetch runs for hours and *will* be
interrupted — a dropped connection, a rate-limit stall, an operator
restarting the process. Restarting from zero each time would make the
backfill effectively impossible to complete, so completed work units are
recorded as they finish and skipped on the next run.

The store is an append-only JSON Lines file, chosen over rewriting a
single JSON document because appending one line is the closest thing to
an atomic operation available here: a process killed mid-write can leave
at most one malformed trailing line, which is detected and discarded on
read. Rewriting a whole document risks losing every prior record.

Deliberately not a database table. Module 05 owns persistence, and a
fetch job's progress is operational bookkeeping rather than ARGUS data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One completed (or permanently failed) unit of work."""

    unit: str
    succeeded: bool
    recorded_at: datetime
    detail: dict[str, Any]


class JobCheckpoint:
    """Tracks which units of a long-running fetch are already done.

    A "unit" is whatever the job iterates over — a symbol for a
    historical backfill, a date for a bulk EOD sweep.
    """

    def __init__(self, directory: str | Path, job_name: str) -> None:
        self._path = Path(directory) / f"{job_name}.jsonl"
        self._completed: dict[str, CheckpointRecord] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read prior progress. A missing file simply means a fresh start."""
        self._completed.clear()
        self._loaded = True
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                record = CheckpointRecord(
                    unit=payload["unit"],
                    succeeded=payload["succeeded"],
                    recorded_at=datetime.fromisoformat(payload["recorded_at"]),
                    detail=payload.get("detail", {}),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # A torn final line from an interrupted write. Everything
                # before it is still good, so drop this one and continue.
                continue
            self._completed[record.unit] = record

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def is_complete(self, unit: str) -> bool:
        """True if this unit finished successfully on an earlier run.

        A unit that failed is NOT complete: it is retried on resume,
        because a transient failure must not become a permanent hole in
        the historical record.
        """
        self._ensure_loaded()
        record = self._completed.get(unit)
        return record is not None and record.succeeded

    def record(self, unit: str, *, succeeded: bool = True, **detail: Any) -> None:
        """Append one unit's result, flushed immediately.

        Flushed rather than buffered because the value of a checkpoint is
        entirely in surviving an abrupt exit.
        """
        self._ensure_loaded()
        record = CheckpointRecord(
            unit=unit,
            succeeded=succeeded,
            recorded_at=datetime.now(UTC),
            detail=detail,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "unit": record.unit,
                "succeeded": record.succeeded,
                "recorded_at": record.recorded_at.isoformat(),
                "detail": record.detail,
            },
            separators=(",", ":"),
        )
        with self._path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
            file.flush()
        self._completed[record.unit] = record

    def pending(self, units: list[str]) -> list[str]:
        """The subset of `units` still needing work, in the given order."""
        self._ensure_loaded()
        return [unit for unit in units if not self.is_complete(unit)]

    def failures(self) -> list[CheckpointRecord]:
        self._ensure_loaded()
        return [record for record in self._completed.values() if not record.succeeded]
