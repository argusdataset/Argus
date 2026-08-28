"""Trusted-proxy client-IP resolution. No database — this is pure logic
over a request-shaped object and a config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from infra.security.client_ip import resolve_client_ip
from infra.security.config import SecuritySettings


@dataclass
class _Client:
    host: str


@dataclass
class _Request:
    """The narrow slice of a real `Request` `resolve_client_ip` reads."""

    client: _Client | None
    headers: dict[str, str] = field(default_factory=dict)


def _request(peer: str | None, forwarded: str | None = None) -> _Request:
    request = _Request(client=_Client(peer) if peer else None)
    if forwarded is not None:
        request.headers = {"X-Forwarded-For": forwarded}
    return request


def _settings(*, trusted: tuple[str, ...] = (), depth: int = 1) -> SecuritySettings:
    return SecuritySettings(trusted_proxies=trusted, trusted_proxy_depth=depth)


# --------------------------------------------------------------------------
# The default: unconfigured means never trust the header
# --------------------------------------------------------------------------


def test_with_no_trusted_proxies_configured_the_header_is_ignored():
    """Module 22's original behaviour, preserved as the default.

    An attacker sending a fresh `X-Forwarded-For` on every request must
    not be able to evade a per-source lockout just because this section
    of config was never touched.
    """
    request = _request("203.0.113.9", forwarded="198.51.100.1")

    resolved = resolve_client_ip(request, _settings())

    assert resolved == "203.0.113.9"


def test_with_no_client_and_no_config_the_result_is_none():
    request = _request(None)

    assert resolve_client_ip(request, _settings()) is None


def test_an_untrusted_peer_is_never_believed_even_with_proxies_configured():
    """The header is trusted by *who sent it*, not by whether the section is on."""
    request = _request("198.51.100.50", forwarded="1.2.3.4")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.0/8",)))

    assert resolved == "198.51.100.50"


# --------------------------------------------------------------------------
# A trusted single hop
# --------------------------------------------------------------------------


def test_a_trusted_proxy_forwards_the_real_client_address():
    request = _request("10.0.0.5", forwarded="1.2.3.4")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",)))

    assert resolved == "1.2.3.4"


def test_a_trusted_proxy_by_cidr_range_is_also_believed():
    request = _request("10.4.9.200", forwarded="1.2.3.4")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.0/8",)))

    assert resolved == "1.2.3.4"


def test_a_trusted_peer_with_no_forwarded_header_falls_back_to_the_peer():
    request = _request("10.0.0.5", forwarded=None)

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",)))

    assert resolved == "10.0.0.5"


def test_a_trusted_peer_with_an_empty_forwarded_header_falls_back():
    request = _request("10.0.0.5", forwarded="   ")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",)))

    assert resolved == "10.0.0.5"


def test_a_malformed_hop_falls_back_to_the_peer_rather_than_erroring():
    """A garbage header must never crash the request it is attached to."""
    request = _request("10.0.0.5", forwarded="not-an-ip-address")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",)))

    assert resolved == "10.0.0.5"


# --------------------------------------------------------------------------
# Depth: more than one hop of trusted infrastructure
# --------------------------------------------------------------------------


def test_depth_one_stops_at_the_hop_nearest_the_server():
    """Two hops present, only one trusted (depth=1): the CDN's own address
    is taken as the client, because ARGUS was not told to trust past it.
    """
    request = _request("10.0.0.5", forwarded="1.2.3.4, 203.0.113.7")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",), depth=1))

    assert resolved == "203.0.113.7"


def test_depth_two_walks_past_a_second_trusted_hop():
    """The chain `client, cdn` arriving via a trusted load balancer, with
    depth=2 telling ARGUS to trust the CDN's hop too.
    """
    request = _request("10.0.0.5", forwarded="1.2.3.4, 203.0.113.7")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",), depth=2))

    assert resolved == "1.2.3.4"


def test_depth_beyond_the_chains_length_stops_at_the_leftmost_entry():
    request = _request("10.0.0.5", forwarded="1.2.3.4")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",), depth=5))

    assert resolved == "1.2.3.4"


def test_a_non_ip_entry_partway_through_the_chain_stops_the_walk_safely():
    """A chain an attacker tampered with partway through is not trusted past that point."""
    request = _request("10.0.0.5", forwarded="1.2.3.4, garbage")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",), depth=2))

    assert resolved == "10.0.0.5"


@pytest.mark.parametrize("depth", [0, -1])
def test_a_non_positive_depth_is_clamped_to_at_least_one(depth):
    request = _request("10.0.0.5", forwarded="1.2.3.4")

    resolved = resolve_client_ip(request, _settings(trusted=("10.0.0.5",), depth=depth))

    assert resolved == "1.2.3.4"


def test_the_default_settings_object_trusts_nothing():
    """The behaviour a deployment gets by doing nothing at all."""
    settings = SecuritySettings()

    assert settings.trusted_proxies == ()
    assert settings.trusted_networks() == ()
