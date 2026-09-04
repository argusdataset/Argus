"""What each deployable process actually runs. One definition, several consumers.

Railway's configuration, the Dockerfile's default command, this module's
README and the tests that check them all describe the same ten
processes. Written four times they drift; written once and read four
times they cannot. `PROCESSES` is that once.

## Why the API services run one worker

`WEB_CONCURRENCY` defaults to 1, and Module 24's rate limiter is the
reason. Its counters live in process memory, so N workers means N
independent ceilings — an effective limit N times what was configured,
invisible from inside any of them. `infra/deploy/config.py` refuses to
start a production process with more than one worker and no shared
store, so this default is not merely a suggestion.

The alternative — adding Redis so the limiter can be shared — was
considered and declined for now. It is a real dependency with its own
availability, credentials and failure modes, added to serve a system
with no users, to raise a ceiling nothing is currently approaching.
Scaling horizontally on Railway is done by adding *replicas* rather than
in-process workers, and the same argument applies to replicas: the
ceiling multiplies. So the honest statement is that ARGUS is
single-process-per-service until it has enough traffic to need
otherwise, and the day it does, the limiter needs a shared store first.
That is recorded in the README's "before going live" list rather than
left as a surprise.

## Why the cron processes have no health check and no port

Neither serves. Railway health-checks HTTP services by polling a path; a
cron service has no path to poll and no process running between firings.
The scanner's health is Module 18's `live_scan_runs` table and Module
23's `scan_health`, which the health service reports; the retention
job's is its own exit code and the growth records it logs; the ingestion
job's is its exit code plus the readiness report it logs, which is the
scanner's own coverage question asked an hour early. A process-level
probe on any of them would answer a question nobody is asking.

## Ordering between the weekday crons

`ingestion` (21:00 UTC), `scanner` (22:30) and `telegram_dispatch`
(23:00) are a chain, not three independent jobs. The scanner refuses to
scan a session whose OHLCV coverage is short and `ingestion` is what
delivers it; the dispatch reads the state transitions the scan records.
So the gaps between them are deadlines. Railway has no notion of one cron
depending on another, so the dependency lives in the schedules and is
stated in each file rather than being inferable from none of them.

The two deadlines are not equally hard. A scan that runs before ingestion
finished loses the day. A dispatch that runs before the scan finished
sends nothing and costs one evening's alerts — and does not catch up
later, deliberately: an alert about a two-day-old state change is worse
than no alert. See `services/telegram/dispatch.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["PROCESSES", "ProcessDefinition", "api_processes", "start_command"]


@dataclass(frozen=True, slots=True)
class ProcessDefinition:
    """One deployable process: what it runs, how it is checked, when it runs."""

    name: str
    #: "web" serves HTTP and gets a health check and a port. "cron" runs
    #: to completion on a schedule and gets neither.
    kind: str
    #: The `infra.deploy.asgi` factory, for a web process.
    asgi_factory: str | None = None
    #: The module executed with `python -m`, for a cron process.
    module: str | None = None
    #: Railway polls this and will not route traffic to a container that
    #: does not answer it. Module 24's public liveness probe, which is
    #: exempt from the rate limiter for exactly this reason.
    health_path: str | None = None
    #: Cron expression, for a cron process. UTC.
    schedule: str | None = None
    description: str = ""

    @property
    def is_web(self) -> bool:
        return self.kind == "web"

    def command(self) -> str:
        """The container's start command."""
        if self.is_web:
            return (
                f"uvicorn infra.deploy.asgi:{self.asgi_factory} --factory "
                "--host 0.0.0.0 --port ${PORT:-8000} "
                "--workers ${WEB_CONCURRENCY:-1} --no-access-log"
            )
        return f"python -m {self.module}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "command": self.command(),
            "health_path": self.health_path,
            "schedule": self.schedule,
            "description": self.description,
        }


#: The pre-deploy command every web service runs before Railway routes
#: traffic to the new container. Non-zero abandons the deploy and the old
#: containers keep serving — which is the entire safety property, and the
#: reason this is a pre-deploy command rather than something the app does
#: at startup.
PRE_DEPLOY_COMMAND = "python -m infra.deploy.migrate"

#: `--no-access-log` on the web processes is deliberate. Uvicorn's access
#: log writes one unstructured line per request in its own format,
#: which would sit alongside Module 23's JSON records and break the "one
#: JSON object per line" convention every log consumer downstream is
#: entitled to rely on. Request-level observability belongs to Module 23,
#: in its own shape.

PROCESSES: dict[str, ProcessDefinition] = {
    "terminal": ProcessDefinition(
        name="terminal",
        kind="web",
        asgi_factory="terminal_app",
        health_path="/health/live",
        description="Module 19. Company fundamentals, news, chart data, user watchlists.",
    ),
    "public_stats": ProcessDefinition(
        name="public_stats",
        kind="web",
        asgi_factory="public_stats_app",
        health_path="/health/live",
        description="Module 20. Public, unauthenticated track record. Expects real traffic.",
    ),
    "intelligence": ProcessDefinition(
        name="intelligence",
        kind="web",
        asgi_factory="intelligence_app",
        health_path="/health/live",
        description="Module 21. Derived watchlists, per-security detail, case explanations.",
    ),
    "identity": ProcessDefinition(
        name="identity",
        kind="web",
        asgi_factory="identity_app",
        health_path="/health/live",
        description="Module 22. The only service that issues credentials.",
    ),
    "telegram": ProcessDefinition(
        name="telegram",
        kind="web",
        asgi_factory="telegram_app",
        health_path="/health/live",
        description=(
            "Module 27. The bot's webhook: /start and /stop, nothing else. "
            "Holds no bot token — see services/telegram/app.py."
        ),
    ),
    "health": ProcessDefinition(
        name="health",
        kind="web",
        asgi_factory="health_app",
        health_path="/health/live",
        description=(
            "Modules 23/24. Public liveness plus the admin-gated detailed view. "
            "Its own liveness path is the one Railway polls."
        ),
    ),
    "ingestion": ProcessDefinition(
        name="ingestion",
        kind="cron",
        module="infra.deploy.ingestion",
        # 21:00 UTC on weekdays, ninety minutes before the scanner. The
        # margin is sized for FMP's slowest paid plan: ~33 minutes for a
        # ten-thousand-symbol universe at 300 requests a minute. The
        # tiered fundamentals/news half runs after the prices and has no
        # deadline, since nothing checks how fresh a fundamental is.
        schedule="0 21 * * 1-5",
        description=(
            "Module 26. Full-universe daily OHLCV, then the tiered deep refresh. "
            "What the scanner's readiness check has always assumed exists."
        ),
    ),
    "scanner": ProcessDefinition(
        name="scanner",
        kind="cron",
        module="infra.deploy.scanner",
        # 22:30 UTC on weekdays. After the US close (21:00 UTC, 20:00
        # during daylight saving) with enough margin for a provider to
        # publish the day's bars, and before the next session opens.
        # Module 18 catches up missed dates oldest-first, so a firing
        # that finds nothing ready is harmless and the next one retries.
        schedule="30 22 * * 1-5",
        description="Module 18. One catch-up scan per weekday evening.",
    ),
    "telegram_dispatch": ProcessDefinition(
        name="telegram_dispatch",
        kind="cron",
        module="infra.deploy.telegram_dispatch",
        # 23:00 UTC on weekdays, thirty minutes after the scanner. The
        # scanner's worst case is four attempts with exponential backoff
        # plus the scan itself, so thirty minutes covers a bad evening.
        # Not longer, because an alert's value is that it is fresh —
        # 23:00 UTC is early evening in the Americas.
        schedule="0 23 * * 1-5",
        description=("Module 27. One message per subscriber per BREAKOUT_READY transition."),
    ),
    "retention": ProcessDefinition(
        name="retention",
        kind="cron",
        module="infra.deploy.retention",
        # 03:00 UTC daily. Deliberately not weekday-only: sessions expire
        # on weekends too, and a job whose whole purpose is to stop
        # something accumulating should not take two days off a week.
        schedule="0 3 * * *",
        description="Module 25. Prunes expired sessions; measures append-only growth.",
    ),
    "news_signals": ProcessDefinition(
        name="news_signals",
        kind="cron",
        module="infra.deploy.news_signals",
        # 23:30 UTC on weekdays, thirty minutes after telegram_dispatch —
        # last in the evening chain, since it reads canonical_news filled
        # by ingestion's own tiered deep refresh, which runs after that
        # cron's price pull with no fixed deadline. Unlike the other three
        # crons this one has no hard ordering dependency on the scanner —
        # it never imports core.market_state — so running last only
        # gives the day's news the most time to have landed.
        schedule="30 23 * * 1-5",
        description=(
            "Module 28. Reactive news-volume signal: unusually more canonical_news "
            "today than this security's own trailing baseline. Display-only annotation "
            "in services/intelligence; never read by scoring or market_state."
        ),
    ),
}


def api_processes() -> list[ProcessDefinition]:
    """The web services, in a stable order."""
    return [process for process in PROCESSES.values() if process.is_web]


def start_command(name: str) -> str:
    return PROCESSES[name].command()
