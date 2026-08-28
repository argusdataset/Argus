"""TLS policy: HSTS, and what to do about a request that arrived in plaintext.

Module 24 built every other security header and deliberately left this
one out, saying why:

> "HSTS is a promise that this origin is reachable over HTTPS, kept for
> as long as a browser remembers the header — sending it from a service
> that might be reached over plain HTTP during development, or before TLS
> termination is wired up in front of it, would be issuing a promise this
> layer cannot keep... TLS is deferred to Module 25; HSTS belongs beside
> that decision, set at the point that actually terminates TLS, not here."

This module knows the point that terminates TLS. It is Railway's edge,
not ARGUS — so this file does not terminate anything. What it does is
make the promise now that the promise is keepable, and decide what
happens to a request that reached the application over plaintext anyway.

## ARGUS does not terminate TLS, and should not

Railway terminates TLS at its edge for every `*.up.railway.app` hostname
and every custom domain attached to a service, and forwards to the
container over the platform's private network. Building TLS termination
into the application would mean a second certificate to obtain, renew and
get wrong, in front of one that is already correct — the "actively
harmful redundancy" the deployment-readiness audit warned about, in a
different costume.

So this middleware sets a header and enforces a policy. It does not hold
a private key.

## Why a 426 and not a redirect

The usual answer to a plaintext request is `301 → https://`. That is
right for a browser navigating to a page, and wrong here, for a specific
reason: a redirect happens *after* the request has already been sent, and
this API's requests carry a bearer token in an `Authorization` header. By
the time ARGUS could redirect, the credential has already crossed the
network in the clear. Redirecting tells the client to try again with a
token that should now be considered compromised.

So `require_https` refuses with `426 Upgrade Required` and names the
problem, rather than politely redirecting a leaked credential to safety.
The exception is a request carrying no credential at all — nothing has
leaked, a redirect costs nothing, and refusing a plain `GET /health/live`
because someone typed `http://` would be pedantry.

This only ever fires as defence in depth. Railway's edge already
redirects plaintext to HTTPS before a request reaches the container, so
in the deployment this is written for, the condition should be
unreachable. It exists for the deployment that is *not* the one this was
written for.

## How the scheme is determined

`X-Forwarded-Proto`, which Railway's edge sets — and which is trusted for
exactly the same reason and under exactly the same condition as
`X-Forwarded-For` in Module 24's `client_ip.py`: only when the request
arrived from a peer this deployment has been configured to trust. An
untrusted peer's `X-Forwarded-Proto: https` is a claim anyone can make,
and believing it would turn this middleware into a way to bypass itself.

With no trusted proxy configured, the ASGI scope's own `scheme` is used,
which is the truth about the actual connection.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Any

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from infra.deploy.config import DeploymentProfile
from infra.security.headers import SECURITY_HEADERS
from services.shared.errors import error_payload

__all__ = ["HTTPS_REQUIRED", "TlsPolicyMiddleware", "hsts_value", "request_scheme"]

HTTPS_REQUIRED = "HTTPS_REQUIRED"

#: Never refused over plaintext even in production. A liveness probe that
#: fails because of a scheme policy reports the application as down when
#: the application is fine, which is the one thing a liveness probe must
#: not do.
_ALWAYS_ALLOWED: frozenset[str] = frozenset({"/health/live"})


def hsts_value(profile: DeploymentProfile) -> str | None:
    """The `Strict-Transport-Security` header value, or None to omit it."""
    if not profile.hsts_enabled:
        return None
    value = f"max-age={profile.hsts_max_age_seconds}"
    if profile.hsts_include_subdomains:
        value += "; includeSubDomains"
    # `preload` is deliberately absent — see `config.py`. Getting onto the
    # preload list is easy and getting off it takes months, which is not a
    # commitment to make before the first real domain exists.
    return value


def request_scheme(scope: Scope, profile: DeploymentProfile) -> str:
    """`http` or `https`, believing `X-Forwarded-Proto` only from a trusted peer.

    The same rule Module 24's `client_ip.py` applies to
    `X-Forwarded-For`, for the same reason: a forwarded header is a claim
    by whoever sent it, and the only thing about a request that cannot be
    forged is which peer opened the connection.
    """
    networks = profile_networks(profile)
    peer = scope.get("client")
    peer_host = peer[0] if peer else None

    if networks and peer_host and _peer_is_trusted(peer_host, networks):
        forwarded = _header(scope, "x-forwarded-proto")
        if forwarded:
            # A proxy chain appends, so the first entry is the scheme the
            # original client used.
            return forwarded.split(",")[0].strip().lower()

    return str(scope.get("scheme", "http")).lower()


def profile_networks(profile: DeploymentProfile) -> tuple[IPv4Network | IPv6Network, ...]:
    """`trusted_proxies` parsed to networks. Empty when nothing is trusted.

    A malformed entry is skipped rather than raising. The alternative —
    one typo in a comma-separated environment variable taking the whole
    service down — trades a narrowed trust boundary for a total outage,
    and the narrowing fails safe: an unparsed entry trusts nothing, which
    is this module's default anyway.
    """
    parsed: list[IPv4Network | IPv6Network] = []
    for entry in profile.trusted_proxies:
        try:
            parsed.append(ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(parsed)


def _peer_is_trusted(peer: str, networks: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    """Whether the TCP peer falls in a configured trusted range.

    Deliberately a local three lines rather than an import of Module 24's
    equivalent private helper: the two answer the same question for the
    same reason, but reaching into another module's underscore-prefixed
    name would couple this file to an implementation detail its author
    never published. `SecuritySettings.trusted_networks()` is the public
    surface, and a `DeploymentProfile` is not a `SecuritySettings`.
    """
    try:
        address = ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in networks)


class TlsPolicyMiddleware:
    """Adds HSTS, and refuses or redirects a plaintext request.

    Pure ASGI, and mounted *outside* Module 24's `harden()` stack by
    `asgi.py`, so a refusal here costs nothing further down. Module 24's
    own files are untouched: this is a deployment concern composed on top
    of the service, not a change to what the service is.
    """

    def __init__(self, app: ASGIApp, *, profile: DeploymentProfile) -> None:
        self._app = app
        self._profile = profile
        self._hsts = hsts_value(profile)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        insecure = request_scheme(scope, self._profile) != "https"

        if insecure and self._profile.require_https and scope["path"] not in _ALWAYS_ALLOWED:
            response = self._refuse(scope)
            if response is not None:
                await response(scope, receive, send)
                return

        # HSTS goes on secure responses only. RFC 6797 §7.2: an HSTS host
        # MUST NOT send the header over non-secure transport, and a
        # browser ignores it there anyway — a plaintext response is
        # exactly the one an attacker in the path can rewrite, so a
        # policy asserted on it would be worth nothing even if it were
        # permitted. This is also what keeps the refusal path above and
        # this one consistent: neither stamps a header on a plaintext
        # response, rather than one of them doing it by accident.
        if self._hsts is None or insecure:
            await self._app(scope, receive, send)
            return

        await self._app(scope, receive, _with_hsts(send, self._hsts))

    def _refuse(self, scope: Scope) -> Any:
        """A 426 for a credentialed request, a redirect for an anonymous one."""
        if _header(scope, "authorization") or _header(scope, "cookie"):
            return JSONResponse(
                status_code=426,
                content=error_payload(
                    HTTPS_REQUIRED,
                    "This request carried a credential over an unencrypted connection. "
                    "It has not been processed, and the credential should be treated as "
                    "exposed: sign in again over HTTPS rather than retrying this one.",
                ),
                headers={"Upgrade": "TLS/1.2, HTTP/1.1", **SECURITY_HEADERS},
            )

        host = _header(scope, "host")
        if not host:
            return None
        query = scope.get("query_string", b"").decode("latin-1")
        target = f"https://{host}{scope['path']}" + (f"?{query}" if query else "")
        return RedirectResponse(url=target, status_code=308)


def _with_hsts(send: Send, value: str) -> Send:
    """Wrap `send` so the response start carries HSTS.

    Applied to every response on a secure request, error responses
    included, for the same reason Module 24 stamps its headers on both
    paths: a security header present only on the happy path is one an
    error response defeats. Plaintext responses are excluded by the
    caller — see the RFC 6797 note there.
    """

    async def _send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            if not any(key.lower() == b"strict-transport-security" for key, _ in headers):
                headers.append((b"strict-transport-security", value.encode("latin-1")))
            message = {**message, "headers": headers}
        await send(message)

    return _send


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key.lower() == target:
            return value.decode("latin-1")
    return None
