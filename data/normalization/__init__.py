"""Normalization — built in Module 05.

Turns provider records into ARGUS canonical records: PIT timestamps
stamped from the correct source fields, tickers resolved to stable
identities, corporate actions applied to produce an adjusted series
alongside the retained raw one, and the result persisted insert-only.

This is the provider-independence boundary. Nothing downstream of here
imports `data.provider_adapters` or references a provider's field names.
"""

from data.normalization.adjustments import (
    AdjustmentReport,
    apply_adjustments,
    build_adjustment_factors,
)
from data.normalization.envelopes import unwrap_envelope
from data.normalization.identity import (
    ResolvedSecurity,
    SecurityIdentityResolver,
    TickerResolutionError,
)
from data.normalization.persistence import CanonicalWriter, WriteResult
from data.normalization.pipeline import (
    NormalizationOutcome,
    normalize_security,
    persist,
    resolve_and_normalize,
)
from data.normalization.translate import (
    TranslationError,
    TranslationReport,
    translate_corporate_action,
    translate_daily_bar,
    translate_fundamental,
    translate_news,
)
from data.normalization.validation import (
    FlaggedBar,
    ValidationIssue,
    ValidationResult,
    validate_bars,
)

__all__ = [
    "AdjustmentReport",
    "CanonicalWriter",
    "FlaggedBar",
    "NormalizationOutcome",
    "ResolvedSecurity",
    "SecurityIdentityResolver",
    "TickerResolutionError",
    "TranslationError",
    "TranslationReport",
    "ValidationIssue",
    "ValidationResult",
    "WriteResult",
    "apply_adjustments",
    "build_adjustment_factors",
    "normalize_security",
    "persist",
    "resolve_and_normalize",
    "translate_corporate_action",
    "translate_daily_bar",
    "translate_fundamental",
    "translate_news",
    "unwrap_envelope",
    "validate_bars",
]
