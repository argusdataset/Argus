"""General request-rate ceiling, CORS, and standard security headers.

Every test drives a real request through a real app with the real
`RateLimitMiddleware`/`CORSMiddleware`/`SecurityHeadersMiddleware` stack
`harden()` installs — not a mocked ASGI call.
"""

from __future__ import annotations

from infra.security.headers import SECURITY_HEADERS
from infra.security.rate_limit import RATE_LIMITED

# --------------------------------------------------------------------------
# General rate limiting
# --------------------------------------------------------------------------


def test_a_burst_over_the_ceiling_is_refused_with_429(app_client, tight_rate_limit):
    """Simulated burst, per the brief: a handful of requests, one ceiling, one refusal."""
    limited = app_client("identity", security=tight_rate_limit)
    limit = int(tight_rate_limit.settings.request_rate_limit)

    statuses = [limited.get("/identity/me").status_code for _ in range(limit)]
    over = limited.get("/identity/me")

    assert 401 in statuses, "requests under the ceiling reach the route (and are refused there)"
    assert 429 not in statuses
    assert over.status_code == 429
    assert over.json()["error"]["code"] == RATE_LIMITED


def test_the_429_names_a_retry_after(app_client, tight_rate_limit):
    limited = app_client("identity", security=tight_rate_limit)
    for _ in range(int(tight_rate_limit.settings.request_rate_limit)):
        limited.get("/identity/me")

    over = limited.get("/identity/me")

    assert over.status_code == 429
    assert "Retry-After" in over.headers
    assert int(over.headers["Retry-After"]) >= 1
    assert over.json()["error"]["detail"]["retry_after_seconds"] > 0


def test_the_ceiling_fires_before_routing_is_ever_reached(app_client, tight_rate_limit):
    """Even a path naming no real route is refused once the ceiling trips.

    Proves the middleware sits in front of routing rather than behind
    it: a `Depends`-based limiter would let an unmatched path fall
    through to a 404 without ever counting against the ceiling.
    """
    limited = app_client("identity", security=tight_rate_limit)
    for _ in range(int(tight_rate_limit.settings.request_rate_limit)):
        limited.get("/identity/me")

    over = limited.get("/this/route/does/not/exist")

    assert over.status_code == 429


def test_the_liveness_probe_is_exempt_from_the_general_ceiling(app_client, tight_rate_limit):
    """A monitor that can be rate-limited into reporting "down" lies exactly
    when it matters most.
    """
    from infra.security.health_app import create_health_app

    app = create_health_app(engine=None, security=tight_rate_limit)  # type: ignore[arg-type]
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as limited:
        limit = int(tight_rate_limit.settings.request_rate_limit)
        statuses = [limited.get("/health/live").status_code for _ in range(limit + 5)]

    assert 429 not in statuses


def test_a_normal_request_volume_is_never_refused(client):
    """The default ceiling must not interfere with ordinary use — sanity check."""
    for _ in range(10):
        response = client.get("/identity/me")
        assert response.status_code != 429


def test_two_source_addresses_have_independent_ceilings(app_client, tight_rate_limit):
    """`TestClient` gives every call the same peer, so this is checked at the
    unit level for the resolver (`tests/unit/security/test_client_ip.py`)
    and here only for the property that the counters are keyed at all —
    a second, freshly constructed app (a fresh middleware instance, a
    fresh counter dict) starts its own budget rather than inheriting the
    first one's.
    """
    limit = int(tight_rate_limit.settings.request_rate_limit)
    first = app_client("identity", security=tight_rate_limit)
    for _ in range(limit):
        first.get("/identity/me")
    assert first.get("/identity/me").status_code == 429

    second = app_client("identity", security=tight_rate_limit)
    assert second.get("/identity/me").status_code != 429


# --------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------


def test_every_response_carries_the_standard_security_headers(client):
    """On the happy path — a header only on errors is a header errors defeat."""
    response = client.get("/identity/me")

    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_error_responses_carry_the_headers_too(client):
    response = client.get("/identity/me")  # 401, no credential
    assert response.status_code == 401

    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_the_rate_limited_response_itself_carries_the_headers(app_client, tight_rate_limit):
    """Even the middleware that runs *before* the header middleware ends up
    stamped — `harden()`'s ordering must not leave its own refusals bare.
    """
    limited = app_client("identity", security=tight_rate_limit)
    for _ in range(int(tight_rate_limit.settings.request_rate_limit)):
        limited.get("/identity/me")

    over = limited.get("/identity/me")

    assert over.status_code == 429
    for name, value in SECURITY_HEADERS.items():
        assert over.headers.get(name) == value, name


def test_the_content_security_policy_forbids_everything_by_default(client):
    """The correct policy for an API that serves no HTML, script or style."""
    response = client.get("/identity/me")

    csp = response.headers.get("Content-Security-Policy")
    assert "default-src 'none'" in csp


def test_no_response_sets_strict_transport_security(client):
    """HSTS is a promise about TLS this layer cannot keep — see headers.py.
    Deferred to whatever actually terminates TLS (Module 25).
    """
    response = client.get("/identity/me")

    assert "Strict-Transport-Security" not in response.headers


def test_headers_are_present_across_every_hardened_service(app_client):
    """Not just identity — `harden()` is one function, used by all four."""
    for service in ("identity", "terminal", "intelligence", "public_stats"):
        response = app_client(service).get("/does-not-exist")
        assert response.headers.get("X-Content-Type-Options") == "nosniff", service


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------


def test_no_origin_is_allowed_by_default(client):
    """No browser UI exists yet — nothing is allowed until a deployment configures it."""
    response = client.options(
        "/identity/me",
        headers={
            "Origin": "https://example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_a_configured_origin_is_allowed(app_client):
    from infra.security.config import SecurityConfig, SecuritySettings

    security = SecurityConfig(
        settings=SecuritySettings(cors_allowed_origins=("https://app.argus.test",))
    )
    configured = app_client("identity", security=security)

    response = configured.options(
        "/identity/me",
        headers={
            "Origin": "https://app.argus.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") == "https://app.argus.test"


def test_an_unconfigured_origin_is_refused_even_with_others_allowed(app_client):
    from infra.security.config import SecurityConfig, SecuritySettings

    security = SecurityConfig(
        settings=SecuritySettings(cors_allowed_origins=("https://app.argus.test",))
    )
    configured = app_client("identity", security=security)

    response = configured.options(
        "/identity/me",
        headers={
            "Origin": "https://evil.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-origin") != "https://evil.test"


def test_credentials_are_allowed_for_configured_origins(app_client):
    """`Authorization` is exactly the credential a browser client would send."""
    from infra.security.config import SecurityConfig, SecuritySettings

    security = SecurityConfig(
        settings=SecuritySettings(cors_allowed_origins=("https://app.argus.test",))
    )
    configured = app_client("identity", security=security)

    response = configured.options(
        "/identity/me",
        headers={
            "Origin": "https://app.argus.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("access-control-allow-credentials") == "true"
