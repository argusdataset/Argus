"""Every number this module has, and the one non-number: trusted proxies.

Follows Module 22's convention exactly, including reusing its `SECURITY`
kind rather than inventing a second one — a setting whose value is a
defence, not a computation input. Every setting here is `security`: this
module has no `operational` numbers, because it produces no answer for a
caller to consume, only refusals and headers.

## `trusted_proxies` is the one field that is not a number

It decides whether `X-Forwarded-For` is trusted at all, and how far into
the chain. The default is the empty tuple — **trust nothing** — which
makes every client-IP read fall back to the TCP peer address,
`request.client.host`. That default is deliberately the same behaviour
Module 22 shipped, so a deployment that has not configured this section
gets exactly what it had before: unspoofable, and blind behind a real
proxy. Turning proxy trust on is an explicit configuration act, never an
inferred one — see `client_ip.py` for why guessing would be worse than
declining to guess.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from ipaddress import IPv4Network, IPv6Network
from typing import Any

#: The same string Module 22 uses for the identical concept — "a setting
#: whose value is a defence" — but defined here rather than imported from
#: `services.identity.config`. `services/identity/app.py` imports from
#: `infra.security` (for the trusted-proxy client-IP resolver), so
#: importing the other way would make the two packages import each other:
#: Python would start initialising `infra.security.config`, hit the
#: import of `services.identity.config`, which pulls in the whole
#: `services.identity` package including `app.py`, which reaches back for
#: `infra.security.client_ip` before this module has finished defining
#: `SecuritySettings`. A four-character string is cheaper to duplicate
#: than that cycle is to carry.
SECURITY = "security"

__all__ = ["SECURITY", "SecurityConfig", "SecuritySetting", "SecuritySettings"]


@dataclass(frozen=True, slots=True)
class SecuritySetting:
    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, rationale: str) -> SecuritySetting:
    return SecuritySetting(value=value, kind=SECURITY, rationale=rationale)


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    """Rate ceilings, CORS/header defaults, and the trusted-proxy list."""

    # ---- trusted proxies ----------------------------------------------
    #: TCP peer addresses ARGUS will read `X-Forwarded-For` from. Empty
    #: means never — every read falls back to `request.client.host`. A
    #: value here names your own load balancer or reverse proxy, never a
    #: client; trusting a client-reachable address defeats the point.
    trusted_proxies: tuple[str, ...] = ()
    #: How many hops of `X-Forwarded-For` to walk past trusted proxies
    #: before taking an address as the client's. One is correct for a
    #: single load balancer in front of ARGUS; each additional trusted
    #: hop (a CDN in front of that balancer, say) needs one more.
    trusted_proxy_depth: int = 1

    # ---- CORS -----------------------------------------------------------
    #: Origins a browser client may call this API from. Empty by default
    #: — no browser UI exists yet, so nothing is allowed. Set explicitly
    #: per deployment once one does; never `"*"`, which CORS itself
    #: refuses to combine with the credentialed requests this API takes.
    cors_allowed_origins: tuple[str, ...] = ()

    # ---- general API rate limiting -------------------------------------
    request_rate_limit: SecuritySetting = field(
        default_factory=lambda: _s(
            120.0,
            "Requests per source address per window before a 429. A ceiling meant "
            "to catch a burst or a runaway client, not to be a precise budget — set "
            "well above any legitimate single-user request pattern against Modules "
            "19-22's endpoints.",
        )
    )
    request_rate_window_seconds: SecuritySetting = field(
        default_factory=lambda: _s(
            60.0,
            "The window `request_rate_limit` is counted over.",
        )
    )
    rate_limit_block_seconds: SecuritySetting = field(
        default_factory=lambda: _s(
            30.0,
            "How long a source that tripped the general ceiling is refused before "
            "the window is given a chance to clear on its own. Short — this guards "
            "against a burst, not a determined attacker, which is what Module 22's "
            "identity-specific lockouts are for.",
        )
    )

    def as_dict(self) -> dict[str, Any]:
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        return {
            key: (float(value) if isinstance(value, SecuritySetting) else value)
            for key, value in payload.items()
        }

    def describe(self) -> dict[str, Any]:
        payload = {}
        for f in fields(self):
            value = getattr(self, f.name)
            payload[f.name] = asdict(value) if isinstance(value, SecuritySetting) else value
        return payload

    def security(self) -> dict[str, float]:
        return {
            f.name: float(getattr(self, f.name))
            for f in fields(self)
            if isinstance(getattr(self, f.name), SecuritySetting)
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    def trusted_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """`trusted_proxies` parsed once, as networks (a bare IP is a /32 or /128)."""
        from ipaddress import ip_network

        return tuple(ip_network(entry, strict=False) for entry in self.trusted_proxies)


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    name: str = "argus-security"
    settings: SecuritySettings = field(default_factory=SecuritySettings)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "settings": self.settings.as_dict()}

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
