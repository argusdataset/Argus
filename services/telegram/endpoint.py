"""Where `public_stats` is, and where the public page is. Not secrets.

Both are addresses rather than credentials, so they are environment
configuration and not `SecretsProvider` material — the same distinction
`ARGUS_UNIVERSE_VERSION` sits on in `infra/deploy/scanner.py`: read from
the environment, defaulted in code, and refused nowhere because getting
it wrong produces a visible failure rather than a silent one.

## The default is Railway's internal DNS

Every service in a Railway project resolves its siblings at
`<service>.railway.internal`, over the project's private network. That
is one hop rather than out to the edge and back, it does not spend the
public service's edge bandwidth, and it does not depend on a public
domain existing at all.

The service name is the one thing this cannot know: `SERVICE_NAMES` in
`infra/deploy/railway.py` records that ARGUS's services carry names
chosen in a dashboard rather than derived from the code, and two of them
are already wrong (`"Argus"` serves the Terminal, `"Identity "` has a
trailing space). So the default here is the *process* name — which is
what a correctly-named service would be called — and
`ARGUS_PUBLIC_STATS_URL` is the override for when it is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["PUBLIC_PAGE_ENV_VAR", "PUBLIC_STATS_ENV_VAR", "StatsEndpoint"]

PUBLIC_STATS_ENV_VAR = "ARGUS_PUBLIC_STATS_URL"
PUBLIC_PAGE_ENV_VAR = "ARGUS_PUBLIC_PAGE_URL"

#: Railway's internal hostname for the `public_stats` process, on the
#: port every ARGUS web service binds. Plain `http`: TLS terminates at
#: Railway's edge and the private network carries no certificate, which
#: is exactly why `stats.py` sends `X-Forwarded-Proto`.
DEFAULT_BASE_URL = "http://public_stats.railway.internal:8080"

#: Where a reader is sent for the charts themselves.
DEFAULT_PUBLIC_PAGE = "https://publicstats-production.up.railway.app"

#: Seconds. Short on purpose: this call happens inside a Telegram webhook
#: handler, and Telegram retries a delivery that is not answered
#: promptly. A slow statistics service must produce "temporarily
#: unavailable", not a duplicate `/stats` press.
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class StatsEndpoint:
    """How the bot reaches the statistics it shows, and where to link."""

    base_url: str = DEFAULT_BASE_URL
    public_page: str = DEFAULT_PUBLIC_PAGE
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> StatsEndpoint:
        """Read the two overrides, if set.

        `env` is injectable for the same reason every `as_of` in this
        project is an argument: a test should be able to describe a
        deployment without mutating the process it runs in.
        """
        source = env if env is not None else dict(os.environ)
        return cls(
            base_url=source.get(PUBLIC_STATS_ENV_VAR) or DEFAULT_BASE_URL,
            public_page=source.get(PUBLIC_PAGE_ENV_VAR) or DEFAULT_PUBLIC_PAGE,
        )
