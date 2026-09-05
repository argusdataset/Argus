"""Module 29 — insider-trading clusters and institutional-ownership trend.

Two signals from the FMP Ultimate plan's ownership data, sharing a
package because they share a subject — *who owns this, and are they
buying* — and nothing else. They are deliberately different shapes,
because the questions are:

- **Insider clusters** (`insider.py`) are an event: several distinct
  people made open-market purchases inside a trailing window. Tri-state
  `raised`, because a security whose Form 4 history has never been
  fetched has not been measured and must not read as "insiders are not
  buying".
- **Institutional ownership** (`institutional.py`) is a *trend*, not an
  event, so it carries no `raised` at all. The stored row is this
  quarter's figures, last quarter's, and the change between them; what
  that is worth is the trader's call, not ARGUS's.

## The boundary this module shares with Module 28

Neither signal ever enters `core/scoring/`. `core/market_state/`,
`core/candidate_detection/eligibility/` and `core/live_scanner/` must
never import this package or read its tables — not as a filter, a gate,
or a weight. A security with no insider or institutional data at all
appears on the BREAKOUT_READY watchlist exactly as it would if this
module did not exist, and a structural test asserts the import graph
stays that way.

The reasoning is the one `EVENT_PROXIMITY` taught: turning "an event is
near" into an automatic score reduction encodes one trading style as if
it were a risk measurement, and can work directly against a strategy
that is *looking* for catalysts. These are context for a person to read,
never a number that quietly moves a rank.

## PIT is not decoration here

Both data types have a disclosure lag that is easy to leak through.
Form 4 is due two business days after an insider trade; 13F holdings are
due 45 days after quarter end. Filtering on the transaction date or the
quarter end would hand a backtest weeks of hindsight on exactly the
kind of accumulation this module exists to surface — so availability is
derived from those deadlines, and every read filters on it. See
`config.py` for both constants and why they are tagged `structural`.
"""

from core.ownership_signals.config import (
    OwnershipSignalConfig,
    OwnershipThreshold,
    OwnershipThresholds,
)
from core.ownership_signals.insider import (
    InsiderCluster,
    InsiderClusterSignal,
    assess_insider_batch,
    evaluate_insider_cluster,
    store_insider_signals,
)
from core.ownership_signals.institutional import (
    InstitutionalTrend,
    OwnershipQuarter,
    assess_institutional_batch,
    evaluate_institutional_trend,
    store_institutional_signals,
)
from core.ownership_signals.orchestrator import (
    OwnershipSignalRunReport,
    run_daily_ownership_signals,
)

__all__ = [
    "InsiderCluster",
    "InsiderClusterSignal",
    "InstitutionalTrend",
    "OwnershipQuarter",
    "OwnershipSignalConfig",
    "OwnershipSignalRunReport",
    "OwnershipThreshold",
    "OwnershipThresholds",
    "assess_insider_batch",
    "assess_institutional_batch",
    "evaluate_insider_cluster",
    "evaluate_institutional_trend",
    "run_daily_ownership_signals",
    "store_insider_signals",
    "store_institutional_signals",
]
