# Intelligence (Module 21)

What ARGUS currently thinks about a security, and why. This service
computes nothing: every figure it serves was produced by Modules 08–16
and stored, and every absence it reports was an earlier module's refusal.

## Files

| File | What it owns |
|---|---|
| `watchlists.py` | The three derived lists. A live query over `market_state`, never a table |
| `detail.py` | Per-security assembly: state, score, similarity, risk, explanation |
| `overlays.py` | The chart layer Module 19 left out. Serves no price data |
| `cases.py` | "Why did this fail" — Module 15's record, Module 16's words |
| `reads.py` | Loading stored rows into the shapes Modules 13/11/12/16 expect |
| `blocks.py` | Turning those into response blocks, including the `Unavailable` ones |
| `schemas.py` | This service's own shapes. Explicitly not Module 19's |
| `config.py` | Two operational bounds: page size, staleness horizon |
| `errors.py` | Four codes. Almost nothing here is an error |
| `app.py` | Routing. Three lines per endpoint |

## Endpoints

```
GET /intelligence/watchlists                        the three names
GET /intelligence/watchlists/{name}                 one derived list, live
GET /intelligence/securities/{ticker}               everything ARGUS holds
GET /intelligence/securities/{ticker}/overlays      marks and zones. No bars
GET /intelligence/setups/{setup_id}/outcome         why it ended that way
```

## The four boundaries, and how each is held

Each is enforced by an AST test over this package's source rather than by
a convention — a text or source-position test can pass against code that
was broken in a way that preserved the text, which is what Module 20's
near-miss demonstrated.

1. **No computation.** No import here reaches a scoring, classification,
   similarity or risk function. If a value is needed that is not in a
   column, the work belongs upstream.
2. **No Module 20.** The mirror image of Module 20's own boundary. The
   two services answer different questions for different audiences and
   share only `services/shared/`.
3. **No Module 19 watchlist schemas.** A User Watchlist is a list a person
   made. An Intelligence Watchlist is a query. One shape for both would
   mean a client that could not tell them apart.
4. **No fundamentals.** Checked against the actual Pydantic `model_fields`,
   not against source text. Revenue and P/E belong to the Terminal.

## `INSUFFICIENT_EVIDENCE` is the common answer, not the error path

Most candidates today produce no score, because Module 13 refuses to score
one whose evidence component is unmeasurable. This service treats that as
a complete response: 200, every block present, the decision named, the
components that were and were not measurable listed, and Module 16's
account of the refusal attached. A client renders one layout, not two.

## What this service does not have, and why

- **No stored watchlist.** A second place membership could be recorded is
  a second thing that can disagree with `market_state`.
- **No price bars.** Module 19 serves those. A second OHLCV path would be
  a second price series that could drift from the first.
- **No prose of its own.** Module 16 built a fabrication guard around
  explanation text — every sentence cites a fact, a verifier rejects one
  that does not. Rewriting it here would step outside the guard while
  looking harmless.
- **No consolidation zone price levels.** No module stores them. Module 08
  measures a base's normalized range width and how often support and
  resistance were tested; none of those is a price. The response says so
  rather than deriving levels an API layer has no business deriving.

## Two things a deployment must know

- Module 19's datafeed `/config` still advertises `supports_marks: false`.
  A deployment that wants marks on the chart serves both services behind
  one datafeed prefix and flips that flag — which means editing Module 19,
  outside this module's boundary.
- This service is ungated, unlike Module 20. Showing a person current
  evidence about one security is not a track-record claim. The module
  docstring in `app.py` states where the line would sit if a future
  endpoint crossed it.
