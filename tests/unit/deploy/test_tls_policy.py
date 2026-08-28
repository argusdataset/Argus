"""TLS policy at the edge of the application. Pure ASGI, no database.

ARGUS does not terminate TLS — Railway's edge does, and the container
port is not publicly reachable. So this middleware is not "add HTTPS"; it
is the two things that still have to be decided once somebody else's
proxy has done the encrypting: what to tell a browser (HSTS), and what to
do with a request that arrived in the clear anyway.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from infra.deploy.config import PROFILES, DeploymentProfile
from infra.deploy.tls import TlsPolicyMiddleware, hsts_value, request_scheme
from packages.config.environment import Environment

PRIVATE_PEER = "10.0.0.7"


async def _echo(request: Request) -> JSONResponse:
    return JSONResponse({"path": request.url.path})


def _client(profile: DeploymentProfile, *, peer: str = PRIVATE_PEER) -> TestClient:
    app = Starlette(routes=[Route("/thing", _echo), Route("/health/live", _echo)])
    wrapped = TlsPolicyMiddleware(app, profile=profile)
    return TestClient(wrapped, base_url="http://api.argus.test", client=(peer, 51000))


# --------------------------------------------------------------------------
# HSTS
# --------------------------------------------------------------------------


def test_no_hsts_header_in_development():
    assert hsts_value(PROFILES[Environment.DEVELOPMENT]) is None


def test_production_hsts_claims_a_year_and_subdomains():
    assert hsts_value(PROFILES[Environment.PRODUCTION]) == ("max-age=31536000; includeSubDomains")


def test_staging_hsts_is_a_day_and_does_not_claim_subdomains():
    assert hsts_value(PROFILES[Environment.STAGING]) == "max-age=86400"


def test_hsts_never_asks_for_preload():
    """Getting onto the preload list is easy; getting off takes months."""
    for profile in PROFILES.values():
        assert "preload" not in (hsts_value(profile) or "")


def test_hsts_is_stamped_on_a_real_response():
    client = _client(PROFILES[Environment.PRODUCTION])
    response = client.get("/thing", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_hsts_is_stamped_on_an_error_response_too():
    """A security header present only on the happy path is one an error defeats."""
    client = _client(PROFILES[Environment.PRODUCTION])
    response = client.get("/missing", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 404
    assert "strict-transport-security" in response.headers


def test_hsts_is_never_sent_over_plaintext():
    """RFC 6797 §7.2 forbids it, and a browser ignores it there anyway.

    A plaintext response is precisely the one an attacker in the path can
    rewrite, so a policy asserted on it would be worth nothing even if it
    were permitted.
    """
    client = _client(PROFILES[Environment.PRODUCTION])

    refused = client.get("/thing", headers={"Authorization": "Bearer x"}, follow_redirects=False)
    assert refused.status_code == 426
    assert "strict-transport-security" not in refused.headers

    redirected = client.get("/thing", follow_redirects=False)
    assert redirected.status_code == 308
    assert "strict-transport-security" not in redirected.headers

    probe = client.get("/health/live", follow_redirects=False)
    assert probe.status_code == 200
    assert "strict-transport-security" not in probe.headers


# --------------------------------------------------------------------------
# Which scheme the request actually used
# --------------------------------------------------------------------------


def _scope(peer: str | None, forwarded: str | None = None, scheme: str = "http") -> dict:
    headers = [(b"x-forwarded-proto", forwarded.encode())] if forwarded else []
    return {
        "type": "http",
        "scheme": scheme,
        "headers": headers,
        "client": (peer, 4000) if peer else None,
    }


def test_a_forwarded_scheme_from_an_untrusted_peer_is_ignored():
    """Otherwise the header that says 'this was HTTPS' is the bypass.

    Anyone who can reach the port can send it, and believing it would
    turn this middleware into a way around itself.
    """
    profile = PROFILES[Environment.PRODUCTION]
    assert request_scheme(_scope("203.0.113.9", "https"), profile) == "http"


def test_a_forwarded_scheme_from_a_trusted_peer_is_believed():
    profile = PROFILES[Environment.PRODUCTION]
    assert request_scheme(_scope(PRIVATE_PEER, "https"), profile) == "https"


def test_the_first_entry_of_a_proxy_chain_is_the_clients_scheme():
    profile = PROFILES[Environment.PRODUCTION]
    assert request_scheme(_scope(PRIVATE_PEER, "https, http"), profile) == "https"


def test_with_nothing_trusted_the_connections_own_scheme_is_used():
    profile = PROFILES[Environment.DEVELOPMENT]
    assert request_scheme(_scope(PRIVATE_PEER, "https"), profile) == "http"


def test_a_malformed_trusted_proxy_entry_trusts_nothing_rather_than_crashing():
    """One typo in a comma-separated variable should not be a total outage."""
    profile = DeploymentProfile(
        environment=Environment.PRODUCTION, trusted_proxies=("not-a-network",)
    )
    assert request_scheme(_scope(PRIVATE_PEER, "https"), profile) == "http"


# --------------------------------------------------------------------------
# What happens to a plaintext request
# --------------------------------------------------------------------------


def test_an_anonymous_plaintext_request_is_redirected():
    client = _client(PROFILES[Environment.PRODUCTION])
    response = client.get("/thing", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "https://api.argus.test/thing"


def test_the_redirect_preserves_the_query_string():
    client = _client(PROFILES[Environment.PRODUCTION])
    response = client.get("/thing?a=1&b=2", follow_redirects=False)
    assert response.headers["location"] == "https://api.argus.test/thing?a=1&b=2"


@pytest.mark.parametrize("header", [{"Authorization": "Bearer token"}, {"Cookie": "session=abc"}])
def test_a_credentialed_plaintext_request_is_refused_not_redirected(header: dict):
    """A redirect happens *after* the request crossed the network.

    The token is already exposed by the time a 308 could be sent, so
    politely redirecting it teaches the client to retry a credential that
    should now be treated as leaked. 426 says that instead.
    """
    client = _client(PROFILES[Environment.PRODUCTION])
    response = client.get("/thing", headers=header, follow_redirects=False)
    assert response.status_code == 426
    assert response.json()["error"]["code"] == "HTTPS_REQUIRED"
    assert "Upgrade" in response.headers


def test_the_liveness_probe_is_never_refused_over_plaintext():
    """A probe that fails on a scheme policy reports a healthy app as down."""
    client = _client(PROFILES[Environment.PRODUCTION])
    assert client.get("/health/live", follow_redirects=False).status_code == 200


def test_development_does_not_redirect():
    client = _client(PROFILES[Environment.DEVELOPMENT])
    assert client.get("/thing", follow_redirects=False).status_code == 200
