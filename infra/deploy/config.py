"""Deployment configuration: the settings that differ per environment.

Module 24 built the security mechanisms and left every one of them
switched off by default — trusted proxies empty, CORS origins empty, no
HSTS. That was correct: it had no deployment target to configure them
for, and a default that guesses is worse than a default that declines.
This module has a target, so this file is where the guessing stops.

## Why these are read from the environment rather than hardcoded

`ARGUS_ENV` already selects development/staging/production (Module 02),
and Railway injects environment variables (the deployment audit's
`EnvironmentSecretsProvider` work). So the shape here is: a profile per
environment, holding the values that are *policy*, and environment
variables for the values that are *deployment facts* — the frontend's
origin, the number of workers — which nobody can know at authoring time.

The split matters. `hsts_enabled` is policy: production always wants it,
and a deployment that turns it off has made a mistake rather than a
configuration choice. `cors_allowed_origins` is a fact: it is whatever
the frontend's URL turns out to be, and hardcoding a guess would mean
the first real deployment ships with a wrong value that looks
deliberate.

## Production refuses to start misconfigured

`ProductionMisconfigured` is raised at startup, not logged. Three
conditions:

- **A wildcard CORS origin.** The CORS specification refuses to combine
  `*` with credentialed requests, and this API's clients send
  `Authorization`, so a wildcard here is both a security mistake and a
  configuration that cannot work.
- **More than one worker with no shared rate-limit store.** Module 24's
  limiter keeps its counters in process memory. Two workers means two
  independent ceilings and an effective limit of double the configured
  one; four means quadruple. That is not a subtle degradation, and it is
  invisible from inside either process — so it is checked at the one
  moment it can still be acted on.
- **No trusted proxy behind a platform that has one.** Without it, every
  request appears to originate from the platform's edge, and every
  per-source protection Module 22 and Module 24 built — the login
  lockout, the registration limit, the request ceiling — collapses into
  a single global bucket that one attacker can exhaust for everybody.

Refusing to boot is a deliberate choice over warning and continuing. A
warning at startup is read once, by whoever is watching the deploy, and
then never again; the misconfiguration it described outlives the person
who saw it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from infra.security.config import SecurityConfig, SecuritySettings
from packages.config.environment import Environment

__all__ = [
    "DeploymentProfile",
    "ProductionMisconfigured",
    "PROFILES",
    "profile_for",
    "security_config_for",
]


class ProductionMisconfigured(RuntimeError):
    """A production deployment was configured in a way that cannot be safe."""


@dataclass(frozen=True, slots=True)
class DeploymentProfile:
    """Everything about how ARGUS is exposed that varies by environment."""

    environment: Environment

    #: Send `Strict-Transport-Security`. Module 24 deliberately did not,
    #: because HSTS is a promise that this origin is reachable over HTTPS
    #: and it had no way to know whether that was true. Behind Railway's
    #: edge it is true — TLS is terminated there for every custom domain
    #: and every `*.up.railway.app` hostname — so the promise is keepable
    #: and is made. Locally it is not, so it is not.
    hsts_enabled: bool = False
    #: One year, the value browsers require for preload eligibility.
    #: Preload itself is not requested: it is effectively irreversible
    #: (removal takes months to propagate) and is not a commitment worth
    #: making before the first real domain exists.
    hsts_max_age_seconds: int = 31_536_000
    hsts_include_subdomains: bool = True

    #: Refuse a plaintext request rather than redirecting it. See
    #: `tls.py` — the answer differs for a browser navigation and for an
    #: API call carrying a bearer token, and this switch is the second.
    require_https: bool = False

    #: What Module 24's `client_ip.py` will trust an `X-Forwarded-For`
    #: from. Empty means never, which is what development wants.
    trusted_proxies: tuple[str, ...] = ()
    trusted_proxy_depth: int = 1

    #: Whether this environment must have a real CORS origin configured.
    #: A staging or production API with no frontend origin is not broken
    #: — it simply has no browser client yet — so this is about refusing
    #: a *wildcard*, not about requiring a value.
    forbid_wildcard_cors: bool = False

    #: Whether more than one worker is permitted without a shared
    #: rate-limit store. See the module docstring.
    require_shared_rate_store_for_multiprocess: bool = False

    #: Read from the environment, because nobody can know them at
    #: authoring time.
    cors_allowed_origins: tuple[str, ...] = ()
    workers: int = 1
    shared_rate_limit_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment.value,
            "hsts_enabled": self.hsts_enabled,
            "hsts_max_age_seconds": self.hsts_max_age_seconds,
            "require_https": self.require_https,
            "trusted_proxies": list(self.trusted_proxies),
            "trusted_proxy_depth": self.trusted_proxy_depth,
            "cors_allowed_origins": list(self.cors_allowed_origins),
            "workers": self.workers,
            "shared_rate_limit_configured": self.shared_rate_limit_url is not None,
        }

    def validate(self) -> None:
        """Refuse a configuration that cannot be safe. Raises, never warns."""
        if "*" in self.cors_allowed_origins and self.forbid_wildcard_cors:
            raise ProductionMisconfigured(
                "CORS is configured with a wildcard origin. This API's clients send "
                "Authorization headers, and the CORS specification refuses to combine "
                "a wildcard origin with credentialed requests — so this is both unsafe "
                "and non-functional. Set ARGUS_CORS_ORIGINS to the frontend's actual "
                "origin."
            )

        if (
            self.require_shared_rate_store_for_multiprocess
            and self.workers > 1
            and self.shared_rate_limit_url is None
        ):
            raise ProductionMisconfigured(
                f"{self.workers} workers are configured and no shared rate-limit store "
                "is set. Module 24's request ceiling keeps its counters in process "
                "memory, so N workers means N independent ceilings and an effective "
                f"limit {self.workers}x the configured one. Either run one worker "
                "(WEB_CONCURRENCY=1, the deployed default) or provide a shared store."
            )

        if self.forbid_wildcard_cors and not self.trusted_proxies:
            raise ProductionMisconfigured(
                "No trusted proxy is configured. Behind a platform edge every request "
                "arrives from the edge's address, so every per-source protection — the "
                "login lockout, the registration limit, the request ceiling — becomes "
                "one global bucket a single attacker can exhaust for every user. Set "
                "ARGUS_TRUSTED_PROXIES."
            )


#: Railway routes public traffic to a container through its own edge, over
#: the platform's private network; the container's port is not reachable
#: from the public internet. The TCP peer is therefore always Railway's
#: proxy, which is why trusting the private range is sound here and would
#: not be on a host with direct public ingress.
#:
#: `100.64.0.0/10` (RFC 6598, carrier-grade NAT) is the range Railway's
#: edge/healthcheck prober actually connects from — confirmed from a live
#: deploy's own access log (`100.64.0.2:44971 - "GET /health/live ..."`),
#: not from documentation. Without it, `request_scheme` in `tls.py` never
#: trusts the peer, `X-Forwarded-Proto` is never read, the ASGI scope's
#: own scheme (`http`, since TLS is terminated before the container) is
#: used instead, and a production deployment with `require_https=True`
#: redirects every plaintext-looking request to `https://` — which the
#: browser already used, so the container sees the same "insecure"
#: request again and redirects again: `ERR_TOO_MANY_REDIRECTS`, forever.
#: `10.0.0.0/8` and `172.16.0.0/12` are kept for private networking
#: between services; `100.64.0.0/10` is what the edge itself uses.
#:
#: Stated as a precondition rather than assumed: if a deployment exposes
#: the container port directly, this value is wrong and must be narrowed.
RAILWAY_PRIVATE_NETWORK: tuple[str, ...] = (
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "fd00::/8",
)

PROFILES: dict[Environment, DeploymentProfile] = {
    Environment.DEVELOPMENT: DeploymentProfile(
        environment=Environment.DEVELOPMENT,
        # No HSTS locally: `http://localhost` is how development works,
        # and a browser that receives HSTS for localhost keeps refusing
        # plaintext there for a year afterwards — across every project
        # that shares the hostname.
        hsts_enabled=False,
        require_https=False,
        trusted_proxies=(),
        forbid_wildcard_cors=False,
        require_shared_rate_store_for_multiprocess=False,
    ),
    Environment.STAGING: DeploymentProfile(
        environment=Environment.STAGING,
        hsts_enabled=True,
        # Shorter than production's year. Staging is where a TLS
        # misconfiguration should be discoverable and reversible; a
        # year-long HSTS header on a staging hostname turns a mistake
        # into a year of inaccessibility.
        hsts_max_age_seconds=86_400,
        hsts_include_subdomains=False,
        require_https=True,
        trusted_proxies=RAILWAY_PRIVATE_NETWORK,
        forbid_wildcard_cors=True,
        require_shared_rate_store_for_multiprocess=True,
    ),
    Environment.PRODUCTION: DeploymentProfile(
        environment=Environment.PRODUCTION,
        hsts_enabled=True,
        require_https=True,
        trusted_proxies=RAILWAY_PRIVATE_NETWORK,
        forbid_wildcard_cors=True,
        require_shared_rate_store_for_multiprocess=True,
    ),
}


def profile_for(
    environment: Environment | None = None, env: dict[str, str] | None = None
) -> DeploymentProfile:
    """The profile for an environment, with deployment facts read in.

    `env` is injectable so a test can describe a deployment without
    mutating the process environment — the same reason every `as_of` in
    this project is an argument rather than a call to `now()`.
    """
    source = env if env is not None else dict(os.environ)
    resolved = environment or _environment_from(source)

    profile = PROFILES[resolved]
    return replace(
        profile,
        cors_allowed_origins=_origins(source.get("ARGUS_CORS_ORIGINS")),
        workers=_workers(source.get("WEB_CONCURRENCY")),
        shared_rate_limit_url=source.get("ARGUS_RATE_LIMIT_STORE_URL") or None,
        trusted_proxies=_origins(source.get("ARGUS_TRUSTED_PROXIES")) or profile.trusted_proxies,
    )


def security_config_for(profile: DeploymentProfile) -> SecurityConfig:
    """Module 24's `SecurityConfig`, filled in from a deployment profile.

    Module 24's settings object is the one this feeds — no second
    mechanism, and no reimplementation of what a trusted proxy or an
    allowed origin means. This function is only the wiring between "which
    environment is this" and "what did Module 24 already build".
    """
    base = SecuritySettings()
    return SecurityConfig(
        settings=replace(
            base,
            trusted_proxies=profile.trusted_proxies,
            trusted_proxy_depth=profile.trusted_proxy_depth,
            cors_allowed_origins=profile.cors_allowed_origins,
        )
    )


def _environment_from(source: dict[str, str]) -> Environment:
    raw = source.get("ARGUS_ENV", Environment.DEVELOPMENT.value).strip().lower()
    try:
        return Environment(raw)
    except ValueError as error:
        raise ProductionMisconfigured(
            f"ARGUS_ENV is {raw!r}, which is not one of "
            f"{', '.join(item.value for item in Environment)}. Failing rather than "
            "defaulting: an unrecognised environment silently treated as development "
            "would run production without HSTS, without proxy trust, and without any "
            "of the checks in this file."
        ) from error


def _origins(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _workers(raw: str | None) -> int:
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1
