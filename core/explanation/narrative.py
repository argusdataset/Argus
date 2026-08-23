"""What an explanation is made of.

A `Claim` is one sentence and the facts it rests on. **Every claim cites
at least one fact** — there is no uncited-claim category, and no
"connective text" escape hatch, because that hatch is exactly where an
unsupported sentence would live. Connective wording belongs inside a
claim's own template, attached to the facts that license the rest of it.

Sections group claims by the question they answer, so a consumer can
render "why this score" without the risk section, or show the outcome
without the pattern history. Module 21 will want that; this module does
not do any rendering itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.explanation.facts import FactSet

#: The three kinds of explanation. Distinct rather than one shape with a
#: mode flag, because they answer different questions — see `narrators.py`.
SIGNAL = "signal"
CASE = "case"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

EXPLANATION_KINDS: tuple[str, ...] = (SIGNAL, CASE, INSUFFICIENT_EVIDENCE)


class UncitedClaim(ValueError):
    """A claim was constructed with no facts behind it."""


@dataclass(frozen=True, slots=True)
class Claim:
    """One sentence, and the fact keys it rests on."""

    text: str
    cites: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.cites:
            raise UncitedClaim(
                f"Claim {self.text!r} cites no facts. Every sentence in an explanation "
                "must rest on something in the registry — see core/explanation/facts.py."
            )

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "cites": list(self.cites)}


@dataclass(frozen=True, slots=True)
class Section:
    """Claims answering one question."""

    name: str
    claims: tuple[Claim, ...] = ()

    def text(self) -> str:
        return " ".join(claim.text for claim in self.claims)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "claims": [claim.as_dict() for claim in self.claims]}


@dataclass(frozen=True, slots=True)
class Explanation:
    """One finished explanation, with the registry it was built from.

    The registry travels with the text deliberately: a consumer that wants
    to check a sentence, or link a number back to the field it came from,
    should not have to re-derive anything. It is also what
    `verification.py` checks against.
    """

    kind: str
    subject: str
    headline: Claim
    sections: tuple[Section, ...] = ()
    facts: FactSet = field(default_factory=FactSet)
    #: Dimensions deliberately absent, and why. An explanation that simply
    #: omitted a missing input would be indistinguishable from one where
    #: everything was fine.
    omissions: tuple[str, ...] = ()

    def claims(self) -> tuple[Claim, ...]:
        return (self.headline, *[claim for section in self.sections for claim in section.claims])

    def text(self) -> str:
        parts = [self.headline.text]
        parts.extend(section.text() for section in self.sections if section.claims)
        return " ".join(part for part in parts if part)

    def section(self, name: str) -> Section | None:
        return next((section for section in self.sections if section.name == name), None)

    def cited_keys(self) -> set[str]:
        return {key for claim in self.claims() for key in claim.cites}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "headline": self.headline.as_dict(),
            "sections": [section.as_dict() for section in self.sections],
            "text": self.text(),
            "omissions": list(self.omissions),
            "facts": self.facts.as_dict(),
        }
