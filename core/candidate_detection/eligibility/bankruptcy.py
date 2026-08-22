"""The `BANKRUPTCY_RISK` gate — a documented proxy, not a prediction model.

## Why this is a gate and not a score input

A company bleeding slowly toward zero produces price action that looks
like a consolidation: the range tightens, volatility falls, volume dries
up. Structurally it is indistinguishable from a base. It is not one. The
shape is a company dying quietly, and no amount of pattern quality makes
it an instance of the ARGUS setup.

That is categorically different from "the fundamentals are poor but the
pattern may still work", which is a legitimate case and precisely why
`argus_score` weights Fundamental Context at 0%. Bankruptcy risk is not
bad fundamentals. It is evidence that the observation is not a real
instance of the pattern at all — so it excludes upstream of scoring
rather than being weighted down inside it.

## The data gap this proxy exists to work around

Modules 04 and 06 both established that FMP does not report *why* a
security was delisted: bankruptcy and acquisition are indistinguishable
from the delisting record. So there is no label to learn from, and there
is no honest way to build a fitted model here. What follows is a
rules-based heuristic over fundamentals that were knowable before any
delisting, and it is offered as exactly that.

## The four signals

Each is computed from the most recent statement knowable at `as_of`, via
Module 07/08's PIT-correct primitive. Each is deliberately crude and
individually unreliable.

1. **Negative shareholders' equity** — liabilities exceed assets. The
   single strongest signal here, and the least ambiguous.
2. **Short cash runway while burning** — cash divided by the quarterly
   operating cash burn, counted only when operating cash flow is
   negative. Fires below one year of runway.
3. **Extreme leverage** — total debt as a fraction of total assets, above
   a level at which ordinary refinancing stress becomes existential.
4. **Heavy dilution** — share count growth over the trailing year.
   Measured PIT-correctly by comparing the statement knowable *now*
   against the statement that was knowable *a year ago*, never against a
   restated history. Repeated large equity raises at declining prices are
   what funding a burn without a business looks like.

## Two signals to exclude, not one — and why that matters here

A pre-revenue biotech burns cash with a short runway and dilutes heavily.
That is the normal operation of a clinical-stage company, not evidence of
death. Those are exactly the names ARGUS exists to find — MLSS, SLS,
QBTS and their kind — so a gate that excluded on any single signal would
systematically strip out the target population while feeling rigorous.

Requiring two independent signals is the compromise. It is not principled
statistics; it is a deliberate bias toward inclusion in a module whose
expensive error is exclusion. Single-signal securities pass the gate with
the signal recorded in `detail`, so a later module (or a human) can see
that the concern existed and was consciously not disqualifying.

## What this cannot catch — stated plainly

- **A solvent company that fails suddenly.** Fraud, a failed trial, a
  lost lawsuit, a covenant breach. None appear in these four signals.
- **Anything between filings.** Quarterly data is stale by up to a
  quarter plus filing lag. A company can file a healthy-looking balance
  sheet in February and be gone by May, and this gate would pass it
  throughout — correctly, in the point-in-time sense, which is the more
  important guarantee.
- **Going-concern audit opinions.** The clearest signal that exists, and
  ARGUS does not ingest it: FMP's statement payloads carry no auditor
  opinion field. This is the largest single gap in the proxy.
- **Companies with no fundamentals ingested at all.** They pass this
  gate, with `evaluated: false` recorded. That is deliberate: absence of
  fundamentals is a data-coverage fact, and adjudicating it is the
  `DATA_QUALITY` gate's job. Making it a bankruptcy verdict here would
  both duplicate that gate and quietly convert "we don't know" into "this
  company is dying".

## A field-naming caveat

`canonical_fundamentals.data` stores the provider payload as ingested, so
these signals read FMP's field names. Only the income statement has a
committed fixture (`tests/unit/fmp/fixtures/income_statement.json`); the
balance-sheet and cash-flow field names below are taken from FMP's
documented schema and have **not** been verified against a live response,
because outbound access to the API is blocked by the environment's egress
policy. Each concept therefore accepts several plausible spellings and
records which key actually resolved, so a mismatch surfaces as an
unevaluated signal in `detail` rather than as a silently-passed gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy.engine import Connection

from core.candidate_detection.config import EligibilityParameters
from core.candidate_detection.eligibility.gates import GateResult
from core.data_validation.bulk import load_latest_fundamentals_as_of
from data.canonical_model.records import CanonicalStatementType
from infra.db.enums import EligibilityGate

#: Accepted spellings per concept, tried in order. See the field-naming
#: caveat in the module docstring.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "cash": (
        "cashAndCashEquivalents",
        "cashAndShortTermInvestments",
        "cash",
    ),
    "total_assets": ("totalAssets",),
    "total_debt": ("totalDebt", "totalLiabilities"),
    "shareholders_equity": (
        "totalStockholdersEquity",
        "totalShareholdersEquity",
        "totalEquity",
    ),
    "operating_cash_flow": (
        "operatingCashFlow",
        "netCashProvidedByOperatingActivities",
    ),
    "shares_outstanding": (
        "weightedAverageShsOutDil",
        "weightedAverageShsOut",
        "commonStockSharesOutstanding",
    ),
}

#: Below one year of runway, a burning company is dependent on raising.
MIN_RUNWAY_QUARTERS = 4.0
#: Debt at this fraction of assets leaves no equity cushion.
MAX_DEBT_TO_ASSETS = 0.90
#: Share count more than doubling in a year.
MAX_ANNUAL_SHARE_GROWTH = 1.0

#: How far back the dilution comparison looks.
DILUTION_LOOKBACK = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class DistressSignals:
    """Which signals fired for one security, and what they measured."""

    security_id: UUID
    fired: tuple[str, ...]
    measured: dict[str, Any]
    #: False when no fundamentals were knowable at all — see the module
    #: docstring on why that passes rather than fails.
    evaluated: bool

    @property
    def signal_count(self) -> int:
        return len(self.fired)


def load_distress_signals(
    connection: Connection,
    security_ids: list[UUID],
    as_of: datetime,
) -> dict[UUID, DistressSignals]:
    """Evaluate every distress signal for the whole batch.

    Four bulk queries total, regardless of universe size — balance sheet,
    cash flow and income statement as knowable at `as_of`, plus the income
    statement as knowable a year earlier for the dilution comparison.

    Uses `load_latest_fundamentals_as_of`, the bulk form of Module 07's
    `get_latest_fundamental_as_of` that Module 08 added inside
    `core/data_validation/`. Same enforcement rule, same module, different
    arity — a per-security loop over ten thousand names would issue forty
    thousand queries per scan date. A bankruptcy verdict reached with
    non-PIT-correct fundamentals would itself be a leakage vector, which
    is why this never touches raw SQL.
    """
    if not security_ids:
        return {}

    balance = _indexed(
        load_latest_fundamentals_as_of(
            connection, security_ids, CanonicalStatementType.BALANCE_SHEET, as_of
        )
    )
    cash_flow = _indexed(
        load_latest_fundamentals_as_of(
            connection, security_ids, CanonicalStatementType.CASH_FLOW, as_of
        )
    )
    income = _indexed(
        load_latest_fundamentals_as_of(
            connection, security_ids, CanonicalStatementType.INCOME_STATEMENT, as_of
        )
    )
    income_a_year_ago = _indexed(
        load_latest_fundamentals_as_of(
            connection,
            security_ids,
            CanonicalStatementType.INCOME_STATEMENT,
            as_of - DILUTION_LOOKBACK,
        )
    )

    return {
        security_id: _evaluate(
            security_id,
            balance.get(security_id),
            cash_flow.get(security_id),
            income.get(security_id),
            income_a_year_ago.get(security_id),
        )
        for security_id in security_ids
    }


def evaluate_bankruptcy_gate(
    signals: DistressSignals,
    parameters: EligibilityParameters,
) -> GateResult:
    """Fail only when enough independent signals fire. See the docstring."""
    passed = signals.signal_count < parameters.min_distress_signals_to_exclude
    return GateResult(
        gate=EligibilityGate.BANKRUPTCY_RISK,
        passed=passed,
        detail={
            "evaluated": signals.evaluated,
            "signals_fired": list(signals.fired),
            "signal_count": signals.signal_count,
            "signals_required_to_exclude": parameters.min_distress_signals_to_exclude,
            "measured": signals.measured,
            # Named so a stored result stays interpretable if the proxy is
            # later revised — the row records which proxy judged it.
            "proxy": "fundamentals-distress-v1",
        },
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Statement:
    """A statement payload plus the period it covers.

    The period end is carried, not just the data, because the dilution
    comparison has to know whether the two sides are actually different
    filings — see `_evaluate`.
    """

    period_end: Any
    data: dict[str, Any]


def _indexed(frame: pd.DataFrame) -> dict[UUID, _Statement]:
    """Latest statement per security, keyed by security_id."""
    if frame.empty:
        return {}
    return {
        row.security_id: _Statement(
            period_end=row.fiscal_period_end,
            data=row.data if isinstance(row.data, dict) else {},
        )
        for row in frame.itertuples()
    }


def _field(statement: _Statement | None, concept: str) -> float | None:
    """First resolvable alias for `concept`, or None.

    None means "not present in the payload", which is reported as an
    unevaluated signal rather than coerced to zero — a missing cash
    balance is not a cash balance of zero, and treating it as one would
    manufacture distress out of a data gap.
    """
    if statement is None:
        return None
    for alias in FIELD_ALIASES[concept]:
        value = statement.data.get(alias)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _evaluate(
    security_id: UUID,
    balance: _Statement | None,
    cash_flow: _Statement | None,
    income: _Statement | None,
    income_a_year_ago: _Statement | None,
) -> DistressSignals:
    fired: list[str] = []
    measured: dict[str, Any] = {}

    if balance is None and cash_flow is None and income is None:
        return DistressSignals(security_id, (), {"reason": "no_fundamentals"}, evaluated=False)

    equity = _field(balance, "shareholders_equity")
    if equity is not None:
        measured["shareholders_equity"] = equity
        if equity < 0:
            fired.append("negative_shareholders_equity")

    cash = _field(balance, "cash")
    operating_cash_flow = _field(cash_flow, "operating_cash_flow")
    if cash is not None and operating_cash_flow is not None and operating_cash_flow < 0:
        quarters = cash / abs(operating_cash_flow)
        measured["runway_quarters"] = quarters
        if quarters < MIN_RUNWAY_QUARTERS:
            fired.append("short_cash_runway")

    debt = _field(balance, "total_debt")
    assets = _field(balance, "total_assets")
    if debt is not None and assets is not None and assets > 0:
        ratio = debt / assets
        measured["debt_to_assets"] = ratio
        if ratio > MAX_DEBT_TO_ASSETS:
            fired.append("extreme_leverage")

    # Only a comparison between two *different* filings says anything.
    # When nothing newer has been filed in a year, both PIT lookups resolve
    # to the same statement, and reporting a growth of 0.0 from that would
    # assert "no dilution" where the truth is "no new information" — the
    # kind of plausible-looking wrong number this project exists to avoid.
    distinct_filings = (
        income is not None
        and income_a_year_ago is not None
        and income.period_end != income_a_year_ago.period_end
    )
    shares_now = _field(income, "shares_outstanding") if distinct_filings else None
    shares_before = _field(income_a_year_ago, "shares_outstanding") if distinct_filings else None
    if shares_now is not None and shares_before is not None and shares_before > 0:
        growth = (shares_now - shares_before) / shares_before
        measured["annual_share_growth"] = growth
        if growth > MAX_ANNUAL_SHARE_GROWTH:
            fired.append("heavy_dilution")

    return DistressSignals(security_id, tuple(fired), measured, evaluated=True)
