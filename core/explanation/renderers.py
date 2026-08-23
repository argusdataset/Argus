"""Where a language model is allowed to touch the text, and how narrowly.

## The model may rephrase. It may not claim.

Every renderer receives a finished `Explanation` — claims already written,
citations already attached, facts already extracted — and returns one with
the same structure. A renderer may change *wording*. It cannot:

* add a claim (the output is matched to the input claim-for-claim),
* change a citation (citations are copied across, never read from the
  model's output),
* introduce a number or a domain term that no fact licensed (every
  rephrased claim goes through `verification.verify`, and any claim that
  fails is **reverted to the deterministic wording** rather than dropped
  or repaired).

So the worst a misbehaving model can do is produce text that gets thrown
away. That is the point: the guarantee does not rest on the model
following instructions, and it does not rest on the prompt.

## Why the default renderer is deterministic

`DeterministicRenderer` is the identity function — `narrators.py` already
produced final prose. It ships as the default because ARGUS's whole
discipline is deterministic, tested, versioned code, and an explanation
layer that could not run without a network call would make every
downstream test non-reproducible. The language model is an *optional*
improvement to phrasing, behind the same kind of `Protocol` seam Module 09
used for `AnalogueCounter` and Module 10 for `TargetModel`.

**No live API call is made anywhere in the test suite.** The seam is
exercised with a stub renderer that deliberately fabricates, which is a
stronger test of the guarantee than a real call would be — a real model
would probably behave, and "probably behaves" is not what the verifier is
for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from core.explanation.narrative import Claim, Explanation, Section
from core.explanation.verification import verify

#: The model this module would use. Pinned here rather than at the call
#: site so a change is one edit and shows up in review.
MODEL = "claude-opus-5"

#: What the model is told it is doing. Deliberately short: the guarantee
#: is enforced by `verify`, not by asking nicely, and a long prompt would
#: suggest otherwise.
SYSTEM_PROMPT = """You rephrase pre-written financial analysis sentences for readability.

You will receive a list of sentences and the registry of facts they were built from.
For each sentence, return a rephrased version that:
- states exactly the same facts, no more and no fewer
- contains no number that does not already appear in the sentence you were given
- contains no upper-case domain term that does not already appear in it
- does not add interpretation, forecasting, reassurance, or hedging

If a sentence cannot be improved, return it unchanged. Never merge or split sentences."""

#: The response shape the model is constrained to. Rephrasing only — there
#: is no field through which a new claim or a changed citation could
#: arrive.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["index", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


@runtime_checkable
class Renderer(Protocol):
    """Turns an explanation into an explanation with better wording."""

    name: str

    def render(self, explanation: Explanation) -> Explanation:
        """Same claims, same citations, same facts — possibly new prose."""
        ...


@dataclass(frozen=True, slots=True)
class DeterministicRenderer:
    """The default. Returns what the narrators wrote, unchanged.

    Not a placeholder — this is the renderer ARGUS runs unless someone
    deliberately opts into the model. Its output is what every test in
    this module checks, and what the language-model renderer falls back to
    claim by claim.
    """

    name: str = "deterministic"

    def render(self, explanation: Explanation) -> Explanation:
        return explanation


@dataclass(frozen=True, slots=True)
class VerifiedRenderer:
    """Wraps any renderer and reverts whatever it cannot support.

    The safety net that makes the seam safe to open. It compares the
    rendered explanation against the original claim by claim: a rephrased
    claim is kept only if the explanation containing it verifies clean;
    otherwise the original wording is restored. Citations and facts are
    always taken from the original, never from the renderer's output.
    """

    inner: Renderer
    name: str = "verified"

    def render(self, explanation: Explanation) -> Explanation:
        candidate = self.inner.render(explanation)
        original = _claims(explanation)
        # Matched claim-for-claim by position against the original. A
        # renderer that added or dropped claims contributes nothing beyond
        # the positions the original had, which is what makes "cannot add
        # a claim" structural rather than checked.
        proposals = {index: claim.text for index, claim in enumerate(_claims(candidate))}

        kept: dict[int, str] = {}
        for index, claim in enumerate(original):
            proposed = proposals.get(index)
            if proposed is None or proposed == claim.text:
                continue
            trial = replace(claim, text=proposed)
            if not verify(_single(explanation, trial)):
                kept[index] = proposed

        return _rebuild(explanation, kept)


@dataclass(frozen=True, slots=True)
class LanguageModelRenderer:
    """Rephrases claims through Claude, constrained and then verified.

    Three constraints, in decreasing order of how much they are relied on:

    1. **Structural** — the response schema has no field for a citation or
       a new claim, so neither can arrive. This is the one that actually
       holds.
    2. **Verified** — wrap this renderer in `VerifiedRenderer` (or use
       `language_model_renderer()`, which does) and any rephrasing that
       introduces an unlicensed number or term is discarded.
    3. **Instructed** — the system prompt asks for faithful rephrasing.
       Listed last because it is the weakest and is not depended upon.

    The `anthropic` package is imported lazily and is an optional
    dependency: ARGUS runs, and its whole test suite passes, without it.
    """

    client: Any = None
    model: str = MODEL
    name: str = "claude"

    def render(self, explanation: Explanation) -> Explanation:
        claims = _claims(explanation)
        if not claims:
            return explanation

        response = self._client().messages.create(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": self._request(explanation, claims)}],
        )
        payload = _parse(response)
        rendered = {
            int(entry["index"]): str(entry["text"])
            for entry in payload.get("claims", [])
            if isinstance(entry, dict) and "index" in entry and "text" in entry
        }
        return _rebuild(explanation, rendered)

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "LanguageModelRenderer needs the optional `anthropic` package. "
                "ARGUS's default renderer is deterministic and needs nothing — see "
                "core/explanation/renderers.py."
            ) from error
        return anthropic.Anthropic()

    @staticmethod
    def _request(explanation: Explanation, claims: list[Claim]) -> str:
        return json.dumps(
            {
                "facts": explanation.facts.as_dict(),
                "claims": [
                    {"index": index, "text": claim.text} for index, claim in enumerate(claims)
                ],
            },
            indent=1,
            default=str,
        )


def language_model_renderer(client: Any = None, *, model: str = MODEL) -> Renderer:
    """A Claude renderer that cannot ship a fabrication.

    The only constructor this module exposes for the model path, because
    an unwrapped `LanguageModelRenderer` would be exactly the unverified
    text generator the project's architecture rules out.
    """
    return VerifiedRenderer(inner=LanguageModelRenderer(client=client, model=model))


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _claims(explanation: Explanation) -> list[Claim]:
    return list(explanation.claims())


def _single(explanation: Explanation, claim: Claim) -> Explanation:
    """One claim, in an explanation shell, for verification in isolation."""
    return replace(explanation, headline=claim, sections=())


def _rebuild(explanation: Explanation, rendered: dict[int, str]) -> Explanation:
    """The original explanation with accepted rephrasings substituted in.

    Citations and facts come from the original throughout. The renderer's
    output contributes text and nothing else.
    """
    if not rendered:
        return explanation

    index = 0

    def take(claim: Claim) -> Claim:
        nonlocal index
        text = rendered.get(index, claim.text)
        index += 1
        return replace(claim, text=text)

    headline = take(explanation.headline)
    sections = tuple(
        Section(name=section.name, claims=tuple(take(claim) for claim in section.claims))
        for section in explanation.sections
    )
    return replace(explanation, headline=headline, sections=sections)


def _parse(response: Any) -> dict[str, Any]:
    """The model's JSON, from whichever block carries it."""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                continue
    return {}
