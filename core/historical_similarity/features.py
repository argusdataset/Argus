"""Which features the distance metric uses, and the duplicate-signal problem.

## The duplicate Module 10 found

Module 10 discovered that `volatility_compression` (Group B) and
`volatility_contraction_onset` (Group A) are **byte-identical
computations** — verified equal across all 1,875 cells of the shared
lifecycle fixture. Two of Module 08's fifty features are the same number
under different names.

That is harmless where features are read individually. It is not harmless
in a distance metric. Euclidean distance sums squared per-feature
differences, so including both makes that one signal contribute twice as
much as any other — silently, and in a way no test of the metric itself
would reveal.

**How this module handles it: one representative per duplicate group,
excluded by name and recorded in the result.** Not by halving weights,
which would work arithmetically but would leave two entries in every
explanation of *why* two setups are similar, inviting a reader to treat
them as independent corroboration when they are one measurement.

The exclusion is data, not a code branch: `DUPLICATE_FEATURE_GROUPS` lists
the groups, `metric_features()` drops the non-representatives, and every
similarity result carries `excluded_as_duplicate` so the decision is
visible wherever the number is.

**This module does not fix the duplication at source.** That was flagged
as a separate decision — changing Module 08's feature set would alter its
schema checksum and invalidate every stored feature vector.

**The tripwire.** `tests/unit/historical_similarity/test_duplicate_features.py`
asserts the two features are *still* identical. If Module 08 is ever
corrected, that test fails and says so — which is the point. A silent
exclusion that outlived the problem would quietly discard a real signal,
which is the same class of error in the opposite direction.

## Which features participate at all

Not all fifty. The metric compares *setup structure*, so it uses the
features that describe the shape of the base and its surroundings, and
excludes:

- **Liquidity and tradability** (`avg_dollar_volume`, `spread_proxy`) —
  a thin microcap and a mega-cap can form structurally identical bases,
  and ARGUS exists to find the former. Including dollar volume would push
  every microcap's nearest neighbours toward other microcaps for reasons
  that have nothing to do with the pattern.
- **Sector and industry relative strength** — no sector data exists in
  the schema (Module 08), so these are NaN for every security and would
  contribute nothing but missingness.
- **Absolute-scale features** (`atr`) — a $2 stock and a $200 stock with
  identical structure have ATRs two orders of magnitude apart.
  `atr_percentile` carries the same information scale-free.
"""

from __future__ import annotations

from core.feature_engine.spec import FEATURE_NAMES

#: Groups of features known to be the same underlying signal. The first
#: name in each group is the representative kept; the rest are excluded.
#:
#: Discovered by Module 10 and verified empirically rather than taken on
#: trust — see this module's docstring on the tripwire test.
DUPLICATE_FEATURE_GROUPS: tuple[tuple[str, ...], ...] = (
    # Module 08 computes both as
    # `rolling(medium).std() / rolling(medium).std().shift(medium)`.
    # `volatility_compression` is kept because Group B's consolidation
    # framing is closer to what this metric compares.
    ("volatility_compression", "volatility_contraction_onset"),
)

#: Features excluded because they describe tradability rather than
#: structure. See the module docstring.
LIQUIDITY_FEATURES: frozenset[str] = frozenset({"avg_dollar_volume", "spread_proxy"})

#: Excluded because no sector/industry data exists in the schema, so these
#: are NaN universally and carry no signal.
SECTOR_FEATURES: frozenset[str] = frozenset(
    name for name in FEATURE_NAMES if "sector" in name or "industry" in name
)

#: Excluded because their magnitude scales with the security's price.
#: `atr_percentile` carries the same information scale-free.
ABSOLUTE_SCALE_FEATURES: frozenset[str] = frozenset({"atr"})


def duplicate_representatives() -> frozenset[str]:
    """The one feature kept from each duplicate group."""
    return frozenset(group[0] for group in DUPLICATE_FEATURE_GROUPS)


def excluded_as_duplicate() -> frozenset[str]:
    """Every feature dropped for being a restatement of another."""
    return frozenset(name for group in DUPLICATE_FEATURE_GROUPS for name in group[1:])


def metric_features() -> tuple[str, ...]:
    """The features the distance metric actually compares, in a stable order.

    Stable order matters: per-feature contributions are reported back to a
    user as the explanation of a match, and a set iteration order that
    varied between processes would make two identical runs produce
    differently-ordered explanations.
    """
    dropped = (
        excluded_as_duplicate() | LIQUIDITY_FEATURES | SECTOR_FEATURES | ABSOLUTE_SCALE_FEATURES
    )
    return tuple(name for name in FEATURE_NAMES if name not in dropped)


def exclusion_report() -> dict[str, list[str]]:
    """Why each excluded feature was excluded.

    Carried into every stored similarity result. A distance that cannot
    say which features it did and did not look at is not explainable, and
    'explainable, not black-box' is a binding constraint on this module
    rather than a preference.
    """
    return {
        "duplicate_signal": sorted(excluded_as_duplicate()),
        "liquidity_not_structure": sorted(LIQUIDITY_FEATURES),
        "no_sector_data_in_schema": sorted(SECTOR_FEATURES),
        "absolute_scale": sorted(ABSOLUTE_SCALE_FEATURES),
    }
