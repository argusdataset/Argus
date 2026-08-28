"""Resolving the client address, without trusting a header a client controls.

## The problem this exists to fix, stated by Module 22

`services/identity/app.py`'s `client_address` read `request.client.host`
and deliberately not `X-Forwarded-For`, because behind no configured proxy
that header is attacker-controlled:

> "A deployment behind a real proxy has to configure trusted forwarding
> at the ASGI layer, which is Module 23's concern."

Which cuts both ways and Module 22's own report said so. With no proxy,
trusting the header lets an attacker send a fresh value on every request
and evade the per-source lockout entirely. Behind a real proxy, *not*
trusting it means every request arrives from the proxy's one address —
the per-source lockout becomes a global one, and a single attacker can
lock out every legitimate user behind the same load balancer.

## The fix is configuration, not smarter guessing

There is no way to distinguish "this came through our load balancer" from
"this came from an attacker pretending to be our load balancer" by
inspecting the header alone — both look identical on the wire. The only
sound signal is *who the TCP connection actually came from*: if that peer
is one ARGUS has been told to trust, its `X-Forwarded-For` value is
believed; otherwise it is not, unconditionally.

## Walking the chain from the right, by trusted hops

`X-Forwarded-For` is comma-separated, oldest hop first, each proxy
appending the address it received the request from. The **client** is
the address that started the chain — the leftmost entry — but only up to
the point where trust runs out: a chain entry appended by an untrusted
party is exactly as fake as the header itself would be with no proxy at
all, since anyone can put arbitrary text before a real proxy's own
appended hop.

So the algorithm walks from the **right** — the hop closest to this
server, which is the one whose sender ARGUS can actually verify against
`request.client.host` — consuming one trusted hop at a time up to
`trusted_proxy_depth`, and returns the address immediately past the last
trusted one. Depth exists because a real deployment can have more than
one hop of infrastructure in front of the application (a CDN in front of
a load balancer, say), and each configured hop is one more layer of
"we vouch for whoever is allowed to have appended this entry."

## What this deliberately does not attempt

No IPv6-vs-IPv4 normalisation beyond what `ipaddress` does natively, no
attempt to defend against a proxy that itself forwards an attacker's
`X-Forwarded-For` value uncritically (that proxy's operator has to be the
one who trusts it correctly — this code trusts the proxy's *identity*,
which is all TCP gives it), and no support for the `Forwarded` header
(RFC 7239) — `X-Forwarded-For` is what every proxy in common use actually
sends, and adding a second header format multiplies the ways this can be
gotten wrong for a case nothing here needs yet.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Any

from infra.security.config import SecuritySettings

__all__ = ["resolve_client_ip"]


def resolve_client_ip(request: Any, settings: SecuritySettings | None = None) -> str | None:
    """The address a request should be rate-limited and logged under.

    Falls back to `request.client.host` — Module 22's original behaviour
    — whenever proxy trust is unconfigured, the peer is not a trusted
    proxy, or the header is absent or unparseable. A caller never sees a
    different failure mode for a misconfiguration than for no
    configuration at all; both mean "use the TCP peer", which is always
    safe even when it is not maximally informative.
    """
    settings = settings or SecuritySettings()
    peer = request.client.host if request.client else None

    networks = settings.trusted_networks()
    if not networks or peer is None or not _trusted(peer, networks):
        return peer

    header = request.headers.get("X-Forwarded-For")
    if not header:
        return peer

    hops = [entry.strip() for entry in header.split(",") if entry.strip()]
    if not hops:
        return peer

    # Walk from the right: the hop nearest this server is the one whose
    # sender was just verified as trusted. Each further trusted hop lets
    # the walk continue one step further left.
    depth = max(1, int(settings.trusted_proxy_depth))
    index = len(hops) - 1
    trusted_so_far = 1  # the TCP peer itself, already verified above

    while index > 0 and trusted_so_far < depth:
        candidate = hops[index]
        if not _looks_like_ip(candidate):
            return peer
        index -= 1
        trusted_so_far += 1

    candidate = hops[index]
    return candidate if _looks_like_ip(candidate) else peer


def _trusted(peer: str, networks: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    try:
        parsed = ip_address(peer)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _looks_like_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True
