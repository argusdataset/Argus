"""Fixtures for normalization unit tests.

Re-exports the FMP adapter's test fixtures so the envelope tests can run
provider records through Module 04's real fetch path (against
MockTransport — still no live calls) before translating them. Proving the
canonical output is identical across envelope shapes is only meaningful
if the real unwrapping code runs.
"""

from __future__ import annotations

from tests.unit.fmp.conftest import (  # noqa: F401
    config,
    make_client,
    make_fetcher,
)
