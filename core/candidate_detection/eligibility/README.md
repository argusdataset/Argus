# Eligibility

The six gates that decide whether a candidate may be scored at all — built
in Module 09, alongside the detection stage in the parent package.

| File | Contents |
|---|---|
| `gates.py` | `GateResult`, `EligibilityOutcome`, `EligibilityReport` |
| `checks.py` | `DATA_HISTORY`, `DATA_QUALITY`, `LIQUIDITY`, `VALID_ASSET_IDENTITY` |
| `bankruptcy.py` | `BANKRUPTCY_RISK` — the distress proxy and its limitations |
| `analogues.py` | `MINIMUM_HISTORICAL_ANALOGUES` — and the Module 11 seam |
| `runner.py` | Batch evaluation of all six over a candidate pool |

The reasoning for each gate lives in its own module docstring rather than
here; `bankruptcy.py` and `analogues.py` in particular carry long
explanations of what they can and cannot claim, which is the point of
giving each its own file.

See `../README.md` for how the two stages fit together and why they are
deliberately separate.
