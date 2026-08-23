"""The fabrication test, as code the module runs on its own output.

Every prior module in ARGUS has one load-bearing check that would catch
its worst failure: Module 07's leakage query, Module 13's score/probability
separation, Module 15's PIT excursion test. This is that check here, and
the failure it exists to catch is linguistic rather than arithmetic — a
fabricated number reads exactly like a real one.

## Four rules, all set-membership

1. **Every claim cites at least one fact.** Enforced at construction by
   `Claim`, re-checked here because an explanation may arrive from
   somewhere else — an LLM renderer, a deserialized payload.
2. **Every cited key exists in the registry.** A citation to a fact that
   was never extracted is a claim about nothing.
3. **Every number in the prose is licensed.** Extract every numeric token
   from the finished text; each must appear in some fact's `rendered`
   string. This is the rule that catches "84 historical analogues" when
   the input said six.
4. **Every domain term in the prose is licensed.** ARGUS's vocabulary is
   upper case — `INSUFFICIENT`, `ADEQUATE`, `BREAKOUT_READY`, `FAILED`.
   Each such token must come from a fact, which is what stops an
   `INSUFFICIENT` similarity result being narrated as `ADEQUATE`.

Rules 3 and 4 are what make this more than a citation audit. A renderer
can cite the right facts and still write the wrong sentence; it cannot
write a number or a state name that no fact produced.

## What this deliberately does not check

Whether the prose is *true to* the facts in a semantic sense — "the
evidence is strong" over six cases cites a real fact and contains no
illegal token. That gap is closed by construction rather than by
verification: the deterministic renderer's wording is bound to the
sufficiency state, and `tests/unit/explanation/test_uncertainty.py`
asserts that thin evidence produces visibly different language. A renderer
that wrote its own adjectives would need its own test; the LLM seam in
`renderers.py` runs this verifier and falls back to the deterministic
phrasing for any claim that fails, which is why a fabrication cannot ship
even if a model produces one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.explanation.narrative import Explanation

UNCITED_CLAIM = "uncited_claim"
UNKNOWN_FACT = "unknown_fact"
UNLICENSED_NUMBER = "unlicensed_number"
UNLICENSED_TERM = "unlicensed_term"

VIOLATION_KINDS: tuple[str, ...] = (
    UNCITED_CLAIM,
    UNKNOWN_FACT,
    UNLICENSED_NUMBER,
    UNLICENSED_TERM,
)

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
_TERM = re.compile(r"\b[A-Z][A-Z_]{2,}\b")

#: The system's own name is not a claim about the data, so it does not
#: need a fact behind it. Deliberately the only entry — every other upper
#: case token in ARGUS is domain vocabulary and must be licensed.
SYSTEM_TERMS: frozenset[str] = frozenset({"ARGUS"})


class Fabrication(AssertionError):
    """An explanation contained something its input did not."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One thing in the text that the input does not support."""

    kind: str
    offender: str
    claim: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.offender!r} in {self.claim!r}"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "offender": self.offender, "claim": self.claim}


def verify(explanation: Explanation) -> tuple[Violation, ...]:
    """Every unsupported element of this explanation. Empty means clean."""
    facts = explanation.facts
    licensed = facts.licensed_tokens() | SYSTEM_TERMS
    violations: list[Violation] = []

    for claim in explanation.claims():
        if not claim.cites:
            violations.append(Violation(UNCITED_CLAIM, "", claim.text))
        for key in claim.cites:
            if key not in facts:
                violations.append(Violation(UNKNOWN_FACT, key, claim.text))

        for number in _NUMBER.findall(claim.text):
            if number not in licensed:
                violations.append(Violation(UNLICENSED_NUMBER, number, claim.text))
        for term in _TERM.findall(claim.text):
            if term not in licensed:
                violations.append(Violation(UNLICENSED_TERM, term, claim.text))

    return tuple(violations)


def assert_faithful(explanation: Explanation) -> Explanation:
    """Return the explanation, or raise if anything in it is unsupported.

    Used by the LLM renderer on its own output and by the test suite. A
    caller that wants to inspect rather than fail uses `verify`.
    """
    violations = verify(explanation)
    if violations:
        raise Fabrication(
            "Explanation contains claims its input does not support:\n"
            + "\n".join(f"  - {violation}" for violation in violations)
        )
    return explanation


def unused_facts(explanation: Explanation) -> tuple[str, ...]:
    """Facts the explanation never used.

    Not a violation — an explanation is a summary and is allowed to leave
    things out. It is reported because a *systematically* unused fact is
    usually a narrator that forgot a dimension, and that is invisible
    otherwise.
    """
    return tuple(sorted(set(explanation.facts.names()) - explanation.cited_keys()))
