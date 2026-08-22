"""Can Module 11 replace the analogue check without touching this module?

The forward dependency this module was built around: counting historical
analogues properly is Module 11's job, and Module 11 does not exist. The
agreed resolution was a lightweight check behind an interface, swappable
later by substitution rather than rewrite.

"Swappable" is easy to claim and easy to get wrong — an interface that
secretly depends on the shipped implementation's internals passes every
test until the day someone tries to replace it. So the stub here shares
**no code** with `PeerProfileAnalogueCounter`: it does not import it, does
not subclass it, and does not reuse its bucketing. If the seam were
leaky, this file would not run.

The second thing tested is subtler and matters more once Module 11 lands:
results produced by the provisional counter must stay *identifiable* in
storage. A row recorded today and a row recorded after Module 11 answer
different questions, and a stored gate verdict that could not distinguish
them would be unauditable exactly when the audit becomes interesting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest

from core.candidate_detection.config import DetectionConfig, EligibilityParameters
from core.candidate_detection.eligibility.analogues import (
    AnalogueCount,
    AnalogueCounter,
    PeerProfileAnalogueCounter,
)
from core.candidate_detection.eligibility.runner import _analogue_gate
from core.feature_engine.vector import BatchFeatureResult
from infra.db.enums import EligibilityGate
from tests.unit.candidate_detection.synthetic import BASING_ID, build_universe


@dataclass
class StubHistoricalCounter:
    """What Module 11 might look like from this module's point of view.

    Deliberately unrelated to the shipped implementation — a fixed answer,
    a different method name, and `provisional=False`. It satisfies the
    protocol and nothing else, which is precisely the contract Module 11
    should have to meet.
    """

    fixed_count: int
    method: str = "historical_similarity_v1"

    def count_analogues(
        self,
        securities: list[UUID],
        as_of: datetime,
        features: BatchFeatureResult,
    ) -> dict[UUID, AnalogueCount]:
        return {
            security_id: AnalogueCount(
                count=self.fixed_count,
                method=self.method,
                provisional=False,
                detail={"window": "2010-2024"},
            )
            for security_id in securities
        }


@pytest.fixture(scope="module")
def universe():
    return build_universe()


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


def test_the_stub_satisfies_the_protocol_without_inheriting_anything():
    """Structural typing, so Module 11 owes this module no import.

    A base class would make Module 11 depend on Module 09 to implement a
    Module 11 concern — backwards, and the kind of coupling that turns a
    clean substitution into a refactor.
    """
    stub = StubHistoricalCounter(fixed_count=42)

    assert isinstance(stub, AnalogueCounter)
    assert not isinstance(stub, PeerProfileAnalogueCounter)
    assert PeerProfileAnalogueCounter not in type(stub).__mro__


def test_swapping_the_counter_changes_the_gate_verdict(universe):
    """The substitution has to actually reach the gate, not just be accepted.

    The same security, the same features, the same configuration — and
    opposite verdicts, decided entirely by which counter was supplied.
    """
    securities = [BASING_ID]
    config = DetectionConfig()

    plentiful = StubHistoricalCounter(fixed_count=500).count_analogues(
        securities, universe.as_of, universe
    )
    unprecedented = StubHistoricalCounter(fixed_count=0).count_analogues(
        securities, universe.as_of, universe
    )

    assert _analogue_gate(plentiful[BASING_ID], config).passed
    assert not _analogue_gate(unprecedented[BASING_ID], config).passed


def test_the_gate_reads_only_the_protocol_surface(universe):
    """`count` is the whole contract. Everything else is explanation.

    Two counters agreeing on the number must agree on the verdict, however
    differently they arrived at it — otherwise the interface is not the
    interface.
    """
    config = DetectionConfig()
    stub = StubHistoricalCounter(fixed_count=7).count_analogues(
        [BASING_ID], universe.as_of, universe
    )
    shipped = AnalogueCount(count=7, method="peer_profile", provisional=True)

    assert _analogue_gate(stub[BASING_ID], config).passed
    assert _analogue_gate(shipped, config).passed


def test_a_counter_that_omits_a_security_fails_that_security(universe):
    """An absent count is not evidence that enough analogues exist.

    The failure mode this guards against is a Module 11 implementation
    that silently skips securities it cannot handle — which would read
    downstream as "gate not evaluated" and pass.
    """
    result = _analogue_gate(None, DetectionConfig())
    assert not result.passed
    assert result.detail["reason"] == "counter_returned_no_entry"


# --------------------------------------------------------------------------
# Provisional results must stay identifiable
# --------------------------------------------------------------------------


def test_the_provisional_method_is_recorded_in_the_gate_detail(universe):
    """So today's rows are distinguishable from Module 11's, later."""
    counts = PeerProfileAnalogueCounter().count_analogues([BASING_ID], universe.as_of, universe)
    detail = _analogue_gate(counts[BASING_ID], DetectionConfig()).detail

    assert detail["method"] == "peer_profile"
    assert detail["provisional"] is True


def test_a_real_implementation_records_that_it_is_not_provisional(universe):
    counts = StubHistoricalCounter(fixed_count=10).count_analogues(
        [BASING_ID], universe.as_of, universe
    )
    detail = _analogue_gate(counts[BASING_ID], DetectionConfig()).detail

    assert detail["provisional"] is False
    assert detail["method"] == "historical_similarity_v1"


# --------------------------------------------------------------------------
# The shipped provisional counter
# --------------------------------------------------------------------------


def test_the_peer_counter_returns_an_entry_for_every_security(universe):
    """No omissions — see `test_a_counter_that_omits_a_security_fails_that_security`."""
    securities = list(universe.vectors)[:50]
    counts = PeerProfileAnalogueCounter().count_analogues(securities, universe.as_of, universe)
    assert set(counts) == set(securities)


def test_the_peer_counter_finds_peers_for_an_ordinary_security(universe):
    """A basing security in a universe of 400 is not unprecedented."""
    counts = PeerProfileAnalogueCounter().count_analogues([BASING_ID], universe.as_of, universe)
    assert counts[BASING_ID].count > 0


def test_the_peer_counter_reports_zero_for_an_incomplete_profile(universe):
    """A security that cannot be placed in a bucket is not grouped with everything else."""
    from tests.unit.candidate_detection.synthetic import SPARSE_ID

    counts = PeerProfileAnalogueCounter().count_analogues([SPARSE_ID], universe.as_of, universe)
    assert counts[SPARSE_ID].count == 0
    assert counts[SPARSE_ID].detail["reason"] == "incomplete_profile"


def test_the_peer_counter_never_counts_the_security_itself(universe):
    """A sample of one is not a sample. Self-inclusion would inflate every count by one."""
    counter = PeerProfileAnalogueCounter(buckets=1)
    securities = list(universe.vectors)
    counts = counter.count_analogues(securities, universe.as_of, universe)

    # With a single bucket every complete profile shares it, so the count
    # is the population minus the security itself.
    populations = {c.detail["population"] for c in counts.values() if c.detail.get("population")}
    assert len(populations) == 1
    population = populations.pop()
    for count in counts.values():
        if count.detail and "population" in count.detail:
            assert count.count == population - 1


def test_the_gate_threshold_is_configurable(universe):
    """Provisional counter, provisional threshold — both must be movable."""
    count = AnalogueCount(count=3, method="peer_profile", provisional=True)
    lenient = DetectionConfig(eligibility=EligibilityParameters(min_historical_analogues=1))
    strict = DetectionConfig(eligibility=EligibilityParameters(min_historical_analogues=10))

    assert _analogue_gate(count, lenient).passed
    assert not _analogue_gate(count, strict).passed


def test_the_gate_result_is_tagged_with_the_right_enum(universe):
    counts = PeerProfileAnalogueCounter().count_analogues([BASING_ID], universe.as_of, universe)
    result = _analogue_gate(counts[BASING_ID], DetectionConfig())
    assert result.gate is EligibilityGate.MINIMUM_HISTORICAL_ANALOGUES
