"""ARGUS execution mode."""

from enum import StrEnum


class ExecutionMode(StrEnum):
    """Which of the two required run modes the Intelligence Core is in.

    Every module from Feature Engineering (08) through the Explanation
    Layer (16) must run the same code path in both modes — see
    docs/architecture/CROSS_CUTTING_REQUIREMENTS.md, section A. Nothing
    reads this yet; it exists so that requirement has a home from the
    start rather than being bolted on when Module 17 (historical scan)
    or Module 18 (live scanner) arrive.
    """

    LIVE = "live"
    HISTORICAL_BATCH = "historical_batch"
