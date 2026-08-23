# Module 16 — Explanation Layer

Narrates already-computed evidence. Computes nothing, reads no database,
and cannot state a number its input did not contain.

## The architecture, in one paragraph

Text is never written directly. Every value that may appear in prose is
first extracted into a **`Fact`** — a key, the input path it came from,
the raw value, and *the exact string it is allowed to contribute*. A
**`Claim`** is one sentence plus the fact keys it rests on; a claim with
no citations cannot be constructed. The **verifier** then checks finished
text against the registry: every number and every upper-case domain term
in the prose must be one some fact rendered.

That turns "did this make something up" from a judgement into a set
membership test.

```
input dicts → facts.py → narrators.py → verification.py → renderers.py
 (5 sources)  (registry)   (claims)       (set check)      (optional LLM)
```

## The input surface, enforced

| Source | Shape |
|---|---|
| Module 13 signal | `ScoredSignal.as_dict()` or a `signals` row + `detail` |
| Module 15 case record | `CaseRecord.as_dict()` |
| Module 12 risk context | `RiskContext.as_dict()` |
| Module 11 similarity | `similarity_as_dict(evidence)` |
| Module 10 state evidence | the assignment `evidence` JSONB |

Everything arrives as a plain dictionary. `core/explanation` imports
**nothing from `core.*` beyond itself**, and imports no `sqlalchemy`, no
`psycopg`, no `infra`, no `httpx`. There is no object graph to walk back
to a connection and no table to name, so "cannot read outside the input
surface" is a property of the code rather than a promise. Tests scan every
file's imports — including nested ones, so a lazily-imported database
library is caught too.

## The fabrication test

This module's equivalent of Module 07's leakage query and Module 15's PIT
excursion test. Four rules, all set membership:

1. Every claim cites at least one fact — enforced at construction, and
   re-checked by the verifier because an explanation can arrive from a
   renderer or a deserialized payload.
2. Every cited key exists in the registry.
3. Every number in the prose is licensed by some fact's rendered string.
4. Every upper-case domain term is licensed. This is what stops an
   `INSUFFICIENT` similarity result being narrated as `ADEQUATE`.

There is no uncited-connective category. Connective wording lives inside a
claim's template, attached to the facts licensing the rest of it — because
an "it's just connective text" exemption is exactly where an unsupported
sentence would hide.

**The verifier caught two fabrications in this module's own prose during
development**: "scored 76.0 out of 100" and "a 95% interval of …". Both
numbers are true of ARGUS and neither appears in the input, so both were
removed. The 95% is a real property of Module 11's Wilson interval that
its evidence dict does not carry — flagged below.

### What it deliberately does not catch

Whether the prose is *semantically* faithful. "This setup looks strong"
cites a real fact and contains no illegal token, so set membership cannot
reject it. That gap is closed by construction instead: the deterministic
renderer has no vocabulary that is not bound to a field, and the
uncertainty tests assert that thin evidence produces visibly different
language. There is a test that records this limitation explicitly rather
than leaving it to a docstring.

## Three explanation types, three narrators

| Type | Answers |
|---|---|
| `explain_signal` | why ARGUS scored this the way it did |
| `explain_case` | what happened to a completed setup |
| `explain_insufficient` | why ARGUS declined to score |

Fact extraction and verification are shared — that is where correctness
lives. The narration is three functions with three section sets, because
one generator with a mode flag produces exactly the degraded output the
brief rules out: the same section list with "score: unavailable" where a
number should be. `explain_signal` dispatches to the refusal narrator on
`evidence_status`, since a caller holding a signal does not know which it
has.

A refusal is a complete answer. It names the specific blocking component
from Module 13's verdict, says what ARGUS *could* measure, and states that
the coverage floor which caused the refusal is itself an unvalidated
placeholder.

## Uncertainty is bound to the sentence

Sufficiency changes which sentence is written, not just the numbers in it:

> Historical evidence is **ADEQUATE**. That rests on 200 comparable
> historical cases, enough for the statistics below to be worth reading.

> Historical evidence is **SPARSE**. That rests on only 6 comparable
> historical cases — few enough that the statistics below are indicative
> at best, and the interval around them is wide.

`INSUFFICIENT` quotes no statistics at all, because Module 11 reports
none below its floor. A failure rate is never stated without its interval.
No probability figure ever appears, and every scored explanation says in
words that the score is not one.

## The language-model seam

The default renderer is `DeterministicRenderer` — the identity function,
because `narrators.py` already produced final prose. The model is
optional, behind the same kind of `Protocol` seam Module 09 used for
`AnalogueCounter`.

A renderer may change **wording**. It cannot:

- add a claim — output is matched to input claim-for-claim by position;
- change a citation — citations are copied from the original, never read
  from the renderer's output;
- introduce an unlicensed number or term — every rephrased claim is
  verified, and any that fails is **reverted to the deterministic
  wording**.

So the worst a misbehaving model can do is produce text that gets thrown
away. `language_model_renderer()` is the only constructor exposed for the
model path, and it wraps the renderer in `VerifiedRenderer` — an
unwrapped one would be exactly the unverified text generator the
project's architecture rules out.

**No live API call is made anywhere in the test suite**, deliberately. The
seam is exercised with stubs that fabricate numbers, invent terms, add
claims and rewrite citations. A real model would probably behave, and
"probably behaves" is what the verifier exists not to rely on. `anthropic`
is an optional dependency (`pip install -e ".[llm]"`); it is imported
inside a method so the package — and the whole suite — works without it.

## Known gaps, flagged not fixed

- **Module 11's evidence has no `as_dict()`.** It is the only one of the
  five inputs without one, so this module carries the single adapter in
  the project for it. The method belongs on `SimilarityEvidence`.
- **The Wilson interval's confidence level is not in the data.** Module 11
  computes a 95% interval; its evidence carries only the bounds. A
  consumer therefore cannot state the level without asserting something
  the input does not contain, so this module does not.
- **`signals.detail` and `ScoredSignal.as_dict()` have different shapes.**
  Both work — `signal_facts` reads the union — but a stored row nests the
  breakdown under `detail` while the in-memory object has it at the top
  level, so a caller must know which it holds.
