# Target Model Matching

Contained sub-components of the Market State Engine (Module 10).

A target model judges *how well* a security matches a specific pattern
within the states that model covers. It does **not** assign states — that
is the general state engine's job. Keeping the asymmetry in the type
system is why `interface.py` defines a `Protocol` rather than the model
being inlined into the classifier.

| File | Contents |
|---|---|
| `interface.py` | `TargetModel` protocol, `TargetModelAssessment`, and the boundary discussion |
| `models/target_model_v1/` | The Long Decline → Expansion pattern |

The architecture anticipates a second target model someday. This module
does **not** build support for one — no registry, no dispatch — it only
ensures the first model's code is separable: the engine imports the
protocol and asks the model which features it needs, never the other way
round.

`interface.py` documents an unresolved ambiguity in the Module 10 brief
about whether a target model should *gate* transitions or only *score*
them. The second reading is implemented; the first is flagged rather than
settled.
