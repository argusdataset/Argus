"""Which entities this module enforces PIT correctness for.

Deliberately limited to entities that exist in Module 03's schema today.
Adding a kind here is a schema-driven decision for whichever module owns
that entity, not something to anticipate speculatively.
"""

from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    OHLCV = "ohlcv"
    FUNDAMENTALS = "fundamentals"
    CORPORATE_ACTIONS = "corporate_actions"
    UNIVERSE_MEMBERSHIP = "universe_membership"
    FEATURE_VECTOR = "feature_vector"
