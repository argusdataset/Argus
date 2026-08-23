"""The language-model seam, tested with a model that misbehaves on purpose.

No live API call is made here, and that is a deliberate choice rather than
a convenience. A real model would probably rephrase faithfully, and
"probably behaves" is exactly what the verifier exists not to rely on. The
stubs below fabricate numbers, invent domain terms, add claims and rewrite
citations — every failure mode the seam is supposed to survive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest

from core.explanation import (
    Claim,
    DeterministicRenderer,
    LanguageModelRenderer,
    Renderer,
    VerifiedRenderer,
    explain_signal,
    language_model_renderer,
    verify,
)
from core.explanation.narrative import Section
from core.explanation.renderers import MODEL, RESPONSE_SCHEMA
from tests.unit.explanation import factories as make


@pytest.fixture
def explanation():
    return explain_signal(**make.signal_bundle())


# --------------------------------------------------------------------------
# Stubs standing in for a model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fabricator:
    """A renderer that invents. Everything the seam must survive."""

    text: str
    name: str = "fabricator"

    def render(self, explanation):
        return replace(explanation, headline=replace(explanation.headline, text=self.text))


@dataclass(frozen=True, slots=True)
class ClaimInventor:
    """A renderer that adds a claim nobody asked for."""

    name: str = "inventor"

    def render(self, explanation):
        extra = Claim(text="And it looks great.", cites=explanation.headline.cites)
        return replace(
            explanation,
            sections=(*explanation.sections, Section(name="extra", claims=(extra,))),
        )


@dataclass(frozen=True, slots=True)
class Paraphraser:
    """A renderer that behaves: same facts, different words."""

    name: str = "paraphraser"

    def render(self, explanation):
        headline = explanation.headline
        rewritten = headline.text.replace("ARGUS scored this setup", "The composite score is")
        return replace(explanation, headline=replace(headline, text=rewritten))


class StubClient:
    """A minimal stand-in for `anthropic.Anthropic()`."""

    def __init__(self, responses: dict[int, str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        payload = {
            "claims": [{"index": index, "text": text} for index, text in self.responses.items()]
        }
        block = type("Block", (), {"type": "text", "text": json.dumps(payload)})()
        return type("Response", (), {"content": [block]})()


# --------------------------------------------------------------------------
# The default renderer
# --------------------------------------------------------------------------


def test_the_default_renderer_changes_nothing(explanation):
    """`narrators.py` already produced final prose. The deterministic
    renderer is the identity function, and it is what ARGUS runs unless
    someone deliberately opts into a model."""
    rendered = DeterministicRenderer().render(explanation)

    assert rendered.text() == explanation.text()
    assert verify(rendered) == ()


def test_both_renderers_satisfy_the_protocol():
    assert isinstance(DeterministicRenderer(), Renderer)
    assert isinstance(LanguageModelRenderer(client=object()), Renderer)
    assert isinstance(language_model_renderer(client=object()), Renderer)


# --------------------------------------------------------------------------
# The verifier is what makes the seam safe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sufficiency", "fabrication"),
    [
        ("adequate", "ARGUS found 84 historical analogues with a 71% positive outcome rate."),
        ("adequate", "ARGUS is fairly confident, scoring this 99.9."),
        # ADEQUATE is only a fabrication against evidence that is not
        # adequate — against an ADEQUATE input the word is licensed and
        # the verifier correctly permits it. Using the wrong fixture here
        # would have made this case pass for the wrong reason.
        ("sparse", "Historical evidence is ADEQUATE and the setup looks strong."),
    ],
    ids=["invented_numbers", "invented_score", "invented_term"],
)
def test_a_fabricating_renderer_is_reverted_not_shipped(sufficiency, fabrication):
    """The guarantee does not rest on the model following instructions.

    A rephrased claim is kept only if the explanation containing it
    verifies clean; otherwise the original wording is restored. The worst
    a misbehaving model can do is produce text that gets thrown away.
    """
    from tests.unit.scoring.factories import adequate, sparse

    source = explain_signal(
        **make.signal_bundle(cross=adequate() if sufficiency == "adequate" else sparse())
    )
    rendered = VerifiedRenderer(inner=Fabricator(text=fabrication)).render(source)

    assert rendered.headline.text == source.headline.text
    assert fabrication not in rendered.text()
    assert verify(rendered) == ()


def test_the_verifier_does_not_catch_unsupported_adjectives_and_says_so():
    """The honest limit of this check, asserted rather than left in prose.

    "The setup looks strong" cites a real fact and contains no illegal
    number or term, so set membership cannot reject it. That gap is closed
    by construction instead: the deterministic renderer has no vocabulary
    that is not bound to a field, and `test_uncertainty.py` asserts thin
    evidence produces visibly different language.

    A renderer that writes its own adjectives — a language model — is
    therefore always wrapped in `VerifiedRenderer`, which limits the
    damage to wording, never to a number. This test exists so that
    limitation is a recorded fact about the design rather than a surprise
    to whoever reads the verifier next.
    """
    source = explain_signal(**make.signal_bundle())
    editorialised = replace(
        source, headline=replace(source.headline, text="This setup looks strong.")
    )

    assert verify(editorialised) == ()


def test_a_faithful_rephrasing_is_kept(explanation):
    """The seam is not merely a no-op with extra steps — wording that
    introduces nothing does survive."""
    rendered = VerifiedRenderer(inner=Paraphraser()).render(explanation)

    assert rendered.headline.text != explanation.headline.text
    assert "The composite score is" in rendered.text()
    assert verify(rendered) == ()


def test_a_renderer_cannot_add_a_claim(explanation):
    """Structural, not verified: the output is matched to the input claim
    for claim, so an extra claim has no index to arrive at."""
    rendered = VerifiedRenderer(inner=ClaimInventor()).render(explanation)

    assert len(rendered.claims()) == len(explanation.claims())
    assert "looks great" not in rendered.text()


def test_a_renderer_cannot_change_a_citation(explanation):
    """Citations are copied from the original, never read from the
    renderer's output."""

    @dataclass(frozen=True, slots=True)
    class Recititer:
        name: str = "recititer"

        def render(self, inner):
            return replace(
                inner,
                headline=Claim(text=inner.headline.text, cites=("similarity.failure_rate",)),
            )

    rendered = VerifiedRenderer(inner=Recititer()).render(explanation)

    assert rendered.headline.cites == explanation.headline.cites


def test_every_section_keeps_its_shape_through_a_renderer(explanation):
    rendered = VerifiedRenderer(inner=Paraphraser()).render(explanation)

    assert [section.name for section in rendered.sections] == [
        section.name for section in explanation.sections
    ]
    assert rendered.facts is explanation.facts


# --------------------------------------------------------------------------
# The Claude call itself, without making one
# --------------------------------------------------------------------------


def test_the_request_is_constrained_and_carries_no_citation_field(explanation):
    """The first line of defence is structural: the response schema has no
    field through which a new claim or a changed citation could arrive."""
    client = StubClient(responses={})
    LanguageModelRenderer(client=client).render(explanation)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == MODEL
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["schema"] == RESPONSE_SCHEMA

    properties = RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]
    assert set(properties) == {"index", "text"}
    assert RESPONSE_SCHEMA["properties"]["claims"]["items"]["additionalProperties"] is False


def test_the_request_carries_the_fact_registry_so_the_model_can_be_faithful(explanation):
    client = StubClient(responses={})
    LanguageModelRenderer(client=client).render(explanation)

    payload = json.loads(client.calls[0]["messages"][0]["content"])

    assert set(payload) == {"facts", "claims"}
    assert payload["facts"]
    assert len(payload["claims"]) == len(explanation.claims())


def test_a_model_response_that_fabricates_is_discarded_end_to_end(explanation):
    """The whole path: a client returning an invented statistic, through
    the constructor callers are meant to use."""
    client = StubClient(responses={0: "ARGUS found 84 analogues, 71% of them positive."})

    rendered = language_model_renderer(client=client).render(explanation)

    assert rendered.headline.text == explanation.headline.text
    assert verify(rendered) == ()


def test_a_model_response_that_rephrases_faithfully_is_used(explanation):
    faithful = explanation.headline.text.replace("ARGUS scored", "ARGUS rated")
    client = StubClient(responses={0: faithful})

    rendered = language_model_renderer(client=client).render(explanation)

    assert rendered.headline.text == faithful
    assert verify(rendered) == ()


def test_an_unparseable_model_response_leaves_the_text_alone(explanation):
    class Broken(StubClient):
        def create(self, **kwargs):
            block = type("Block", (), {"type": "text", "text": "not json"})()
            return type("Response", (), {"content": [block]})()

    rendered = LanguageModelRenderer(client=Broken({})).render(explanation)

    assert rendered.text() == explanation.text()


def test_the_optional_dependency_is_only_needed_for_the_model_path():
    """Constructing the renderer must not require `anthropic`; only
    calling it without a client does."""
    renderer = LanguageModelRenderer()

    assert renderer.model == MODEL
    assert renderer.client is None
