"""End-to-end normalization of one security's provider data.

Ties the pieces together in the order correctness requires:

1. resolve the ticker to a stable `security_id`, as of each record's own
   date — so a bar filed under an old ticker lands on the same identity
   as one filed under the new;
2. translate provider records to canonical ones, stamping the four PIT
   timestamps from the correct source fields;
3. validate for impossible values;
4. adjust prices for corporate actions, keeping the raw series intact;
5. persist, insert-only.

Adjustment runs *after* validation on purpose. A bar with an impossible
price would otherwise seed a dividend factor computed from it and quietly
distort every earlier bar in the series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from data.canonical_model.pit import DEFAULT_LAG_POLICY, ProviderLagPolicy
from data.canonical_model.records import (
    CanonicalCorporateAction,
    CanonicalFundamental,
    CanonicalOhlcvBar,
)
from data.normalization.adjustments import AdjustmentReport, apply_adjustments
from data.normalization.identity import SecurityIdentityResolver
from data.normalization.persistence import CanonicalWriter, WriteResult
from data.normalization.translate import (
    TranslationError,
    TranslationReport,
    translate_corporate_action,
    translate_daily_bar,
    translate_fundamental,
)
from data.normalization.validation import ValidationResult, validate_bars
from data.provider_adapters.fmp.models import CorporateAction, DailyBar, FinancialStatement


@dataclass(slots=True)
class NormalizationOutcome:
    """Everything one normalization pass produced, including what it refused."""

    bars: list[CanonicalOhlcvBar] = field(default_factory=list)
    fundamentals: list[CanonicalFundamental] = field(default_factory=list)
    corporate_actions: list[CanonicalCorporateAction] = field(default_factory=list)
    translation: TranslationReport = field(default_factory=TranslationReport)
    validation: ValidationResult = field(default_factory=ValidationResult)
    adjustment: AdjustmentReport = field(default_factory=AdjustmentReport)
    writes: dict[str, WriteResult] = field(default_factory=dict)


def normalize_security(
    *,
    security_id: UUID,
    bars: list[DailyBar] | None = None,
    statements: list[FinancialStatement] | None = None,
    actions: list[CorporateAction] | None = None,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
    include_dividend_adjustment: bool = True,
) -> NormalizationOutcome:
    """Translate, validate and adjust one security's records.

    Pure: no database access. Persisting the result is `persist`, kept
    separate so the transformation can be tested and reasoned about
    without a database, and so a caller controls transaction boundaries.

    A record that cannot be translated without guessing is rejected and
    recorded in `outcome.translation` rather than dropped, because a
    silently missing fundamental is indistinguishable from a company that
    never filed.
    """
    outcome = NormalizationOutcome()

    for action in actions or ():
        try:
            outcome.corporate_actions.append(
                translate_corporate_action(action, security_id, lag=lag)
            )
        except TranslationError as exc:
            outcome.translation.reject(
                f"{action.symbol}:{action.kind}:{action.event_date}", str(exc)
            )

    for statement in statements or ():
        try:
            outcome.fundamentals.append(translate_fundamental(statement, security_id, lag=lag))
        except TranslationError as exc:
            outcome.translation.reject(
                f"{statement.symbol}:{statement.statement_type}:{statement.fiscal_date}", str(exc)
            )

    translated_bars: list[CanonicalOhlcvBar] = []
    for bar in bars or ():
        try:
            translated_bars.append(translate_daily_bar(bar, security_id, lag=lag))
        except TranslationError as exc:
            outcome.translation.reject(f"{bar.symbol}:{bar.bar_date}", str(exc))

    outcome.validation = validate_bars(translated_bars, outcome.corporate_actions)
    outcome.bars = apply_adjustments(
        outcome.validation.accepted,
        outcome.corporate_actions,
        include_dividends=include_dividend_adjustment,
        report=outcome.adjustment,
    )
    return outcome


def persist(outcome: NormalizationOutcome, writer: CanonicalWriter) -> NormalizationOutcome:
    """Write a normalization outcome. Insert-only; re-runs are idempotent.

    Corporate actions go first: they are what the adjusted price series
    is derived from, so a run interrupted between the two leaves the
    actions available to the next pass rather than orphaning adjusted
    bars whose provenance cannot be reconstructed.
    """
    outcome.writes["corporate_actions"] = writer.write_corporate_actions(outcome.corporate_actions)
    outcome.writes["fundamentals"] = writer.write_fundamentals(outcome.fundamentals)
    outcome.writes["bars"] = writer.write_bars(outcome.bars)
    return outcome


def resolve_and_normalize(
    *,
    symbol: str,
    resolver: SecurityIdentityResolver,
    bars: list[DailyBar] | None = None,
    statements: list[FinancialStatement] | None = None,
    actions: list[CorporateAction] | None = None,
    lag: ProviderLagPolicy = DEFAULT_LAG_POLICY,
) -> NormalizationOutcome:
    """Resolve `symbol` to its current identity, then normalize.

    Convenience for live ingestion, where every record concerns the
    ticker's current holder. Historical backfills that span a ticker
    change must resolve per record date instead — see
    `SecurityIdentityResolver.resolve`.
    """
    security_id = resolver.resolve(symbol)
    return normalize_security(
        security_id=security_id,
        bars=bars,
        statements=statements,
        actions=actions,
        lag=lag,
    )
