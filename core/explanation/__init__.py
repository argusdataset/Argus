"""Module 16 — the explanation layer.

Narrates already-computed evidence. Computes nothing, reads no database,
and cannot state a number its input did not contain. See
`core/explanation/README.md`.
"""

from core.explanation.facts import (
    INPUT_SURFACE,
    STRUCTURAL_NUMBERS,
    Fact,
    FactSet,
    case_facts,
    merge,
    risk_facts,
    signal_facts,
    similarity_as_dict,
    similarity_facts,
    state_facts,
)
from core.explanation.narrative import (
    CASE,
    EXPLANATION_KINDS,
    INSUFFICIENT_EVIDENCE,
    SIGNAL,
    Claim,
    Explanation,
    Section,
    UncitedClaim,
)
from core.explanation.narrators import explain_case, explain_insufficient, explain_signal
from core.explanation.renderers import (
    MODEL,
    DeterministicRenderer,
    LanguageModelRenderer,
    Renderer,
    VerifiedRenderer,
    language_model_renderer,
)
from core.explanation.verification import (
    UNCITED_CLAIM,
    UNKNOWN_FACT,
    UNLICENSED_NUMBER,
    UNLICENSED_TERM,
    VIOLATION_KINDS,
    Fabrication,
    Violation,
    assert_faithful,
    unused_facts,
    verify,
)

__all__ = [
    "CASE",
    "EXPLANATION_KINDS",
    "INPUT_SURFACE",
    "INSUFFICIENT_EVIDENCE",
    "MODEL",
    "SIGNAL",
    "STRUCTURAL_NUMBERS",
    "UNCITED_CLAIM",
    "UNKNOWN_FACT",
    "UNLICENSED_NUMBER",
    "UNLICENSED_TERM",
    "VIOLATION_KINDS",
    "Claim",
    "DeterministicRenderer",
    "Explanation",
    "Fabrication",
    "Fact",
    "FactSet",
    "LanguageModelRenderer",
    "Renderer",
    "Section",
    "UncitedClaim",
    "VerifiedRenderer",
    "Violation",
    "assert_faithful",
    "case_facts",
    "explain_case",
    "explain_insufficient",
    "explain_signal",
    "language_model_renderer",
    "merge",
    "risk_facts",
    "signal_facts",
    "similarity_as_dict",
    "similarity_facts",
    "state_facts",
    "unused_facts",
    "verify",
]
