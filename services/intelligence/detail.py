"""Everything ARGUS knows about one security, assembled from stored artefacts.

Assembled. Not computed, not summarised, not interpreted. Every block
comes from a row an earlier module wrote:

| Block | Source | Module |
|---|---|---|
| `state` | `market_state` projection | 10 |
| `score` | latest original `signals` row | 13 |
| `similarity` | `historical_similarity_results`, per scope | 11 |
| `risk` | signal's stored risk flags + `pending_material_events` | 12 |
| `explanation` | `explain_signal` over the above | 16 |
| `news_signal` | `news_volume_signals`, read directly, never joined into the above | 28 |

The explanation is the one place something is *produced* rather than
read, and it is produced by Module 16's own narrator from the blocks
already assembled — not written here. Module 16 built a fabrication guard
around that text; generating prose in this module would step outside it
while looking harmless.

## `INSUFFICIENT_EVIDENCE` gets the same path, not a lesser one

Module 16's `explain_signal` dispatches to the refusal narrator when the
signal is not SCORED, "because a caller holding a signal does not know
which it has and should not have to branch". This module takes that at
face value: one call, one response shape, and the refusal case carries a
complete explanation rather than an empty block.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from core.explanation.narrators import explain_signal
from infra.db.schema.identity import security_identity, security_ticker_history
from services.intelligence.blocks import (
    build_explanation,
    build_freshness,
    build_news_signal,
    build_risk,
    build_score,
    build_similarity,
    build_state,
)
from services.intelligence.reads import (
    latest_news_signal,
    latest_signal,
    latest_similarity,
    pending_events,
    state_row,
)
from services.intelligence.schemas import SecurityDetail

__all__ = ["read_detail", "resolve_identity"]


def read_detail(
    connection: Connection,
    security_id: UUID,
    *,
    as_of: datetime,
    stale_after_seconds: float,
) -> SecurityDetail:
    """One security's full picture, from what is stored.

    Every block is independently absent-able. A security with a state but
    no signal, a signal but no similarity evidence, or nothing at all
    produces the same response shape with different `Unavailable` blocks —
    which is what lets a client render one layout rather than four.
    """
    ticker, name = resolve_identity(connection, security_id, as_of=as_of)

    signal = latest_signal(connection, security_id)
    scoped = latest_similarity(connection, security_id)
    state = state_row(connection, security_id)
    events = pending_events(connection, security_id, as_of=as_of)
    news_signal = latest_news_signal(connection, security_id)

    risk_flags = _risk_flags(signal)
    explanation = _narrate(signal, scoped=scoped, risk=risk_flags, state=state)

    return SecurityDetail(
        security_id=security_id,
        ticker=ticker,
        name=name,
        state=build_state(state),
        score=build_score(signal),
        similarity=build_similarity(scoped),
        risk=build_risk(flags=risk_flags, pending=events),
        explanation=build_explanation(explanation),
        news_signal=build_news_signal(news_signal),
        freshness=build_freshness(
            computed_at=signal.get("event_time") if signal else None,
            as_of=as_of,
            stale_after_seconds=stale_after_seconds,
        ),
    )


def resolve_identity(
    connection: Connection, security_id: UUID, *, as_of: datetime
) -> tuple[str | None, str | None]:
    """The ticker valid at `as_of`, and the display name."""
    row = connection.execute(
        select(
            security_identity.c.name,
            security_ticker_history.c.ticker,
        )
        .select_from(
            security_identity.outerjoin(
                security_ticker_history,
                (security_ticker_history.c.security_id == security_identity.c.id)
                & (security_ticker_history.c.valid_from <= as_of)
                & (
                    security_ticker_history.c.valid_to.is_(None)
                    | (security_ticker_history.c.valid_to > as_of)
                ),
            )
        )
        .where(security_identity.c.id == security_id)
        .order_by(desc(security_ticker_history.c.valid_from))
        .limit(1)
    ).one_or_none()
    return (row.ticker, row.name) if row is not None else (None, None)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _risk_flags(signal: dict[str, Any] | None) -> dict[str, Any]:
    """Module 12's flags as the signal recorded them.

    Read from the stored signal rather than re-assessed. Re-running
    Module 12 now would produce today's risk against a score computed
    days ago, and the two would silently disagree — the explanation would
    cite a flag the score never saw.
    """
    if signal is None:
        return {}
    risk_component = (signal.get("components") or {}).get("risk_reward") or {}
    verdict = signal.get("verdict") or {}
    flags: dict[str, Any] = {}

    for name, value in (risk_component.get("inputs") or {}).items():
        flags[name] = {
            "value": value,
            "normalized": (risk_component.get("normalized") or {}).get(name),
        }
    for name in risk_component.get("unavailable") or ():
        flags.setdefault(name, {"value": None, "normalized": None})

    if verdict:
        flags["gate_verdict"] = {"value": verdict.get("reason"), "normalized": None}
    return flags


def _narrate(
    signal: dict[str, Any] | None,
    *,
    scoped: dict[str, dict[str, Any]],
    risk: dict[str, Any],
    state: Any | None,
) -> Any | None:
    """Module 16's narrator over the assembled blocks. No prose written here."""
    if signal is None:
        return None

    similarity = None
    if scoped:
        # Module 16's `similarity_facts` reads the shape Module 11 stores,
        # scope by scope. Passed through as-is.
        similarity = {
            "cross_asset": scoped.get("CROSS_ASSET"),
            "same_asset": scoped.get("SAME_ASSET"),
        }

    state_evidence = None
    if state is not None:
        state_evidence = {
            "state": str(state.state),
            "confidence": float(state.confidence) if state.confidence is not None else None,
        }

    return explain_signal(
        signal,
        similarity=similarity,
        risk={"flags": risk} if risk else None,
        state_evidence=state_evidence,
    )
