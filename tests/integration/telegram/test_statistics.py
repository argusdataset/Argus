"""The Statistics menu against the real `public_stats` service.

Two things are under test and only one of them is the copy.

The first is the **three-state contract**. `public_stats` distinguishes a
real number, an honest "N of M required" absence, and nothing published
at all, and the bot adds a fourth — cannot reach the service. Collapsing
any pair would let a reader take a container restart for a claim about
ARGUS's track record, which is the opposite of what the review gate
exists to prevent.

The second is the **private-network hop**, and it is the one that would
have shipped broken. In production `public_stats` runs behind
`TlsPolicyMiddleware` with `require_https=True`; the internal call is
plaintext, because Railway terminates TLS at its edge. So the last test
here runs the client against a genuinely production-profile app and
asserts the call is answered rather than redirected — "works in
development, 308s in production" being a shape this project has already
shipped once.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from infra.deploy.config import profile_for
from infra.deploy.tls import TlsPolicyMiddleware
from packages.config.environment import Environment
from services.public_stats.app import create_app as create_public_stats
from services.telegram.messages import statistics_text
from services.telegram.stats import (
    FORWARDED_PROTO_HEADER,
    StatsClient,
    StatsState,
    read_statistics,
)

PUBLISHED_SUMMARY = {
    "published": True,
    "charts": ["win_rate", "cumulative_performance"],
    "approved_run_count": 1,
    "approved_window_count": 1,
    "explanation": "ARGUS publishes results a named human has approved.",
}

UNPUBLISHED_SUMMARY = {
    "published": False,
    "charts": ["win_rate"],
    "approved_run_count": 0,
    "approved_window_count": 0,
    "explanation": (
        "ARGUS has not published statistics yet: no validation run and no live "
        "release window has been approved. This is not a result of zero — it is "
        "the absence of anything ARGUS is permitted to show."
    ),
}


def _client(handler, **kwargs) -> StatsClient:
    return StatsClient("http://public-stats.test", transport=httpx.MockTransport(handler), **kwargs)


def _json(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _routed(routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return routes[request.url.path]
        except KeyError:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    return handler


def test_nothing_published_is_reported_as_that_and_not_as_zero():
    """The honest-absence state, quoting `public_stats`'s own explanation.

    Paraphrasing it here would be a second wording of ARGUS's most
    load-bearing sentence, free to drift from the page's.
    """
    with _client(
        _routed({"/public/stats": _json(UNPUBLISHED_SUMMARY), "/public/releases": _json([])})
    ) as client:
        snapshot = read_statistics(client)

    assert snapshot.state is StatsState.NOT_PUBLISHED
    assert snapshot.explanation == UNPUBLISHED_SUMMARY["explanation"]

    text = statistics_text(snapshot)
    assert "has not published statistics yet" in text
    assert "0%" not in text
    assert "unavailable" not in text.lower()


def test_a_thin_sample_reports_the_floor_rather_than_a_fabricated_rate():
    """The second state: the chart exists and says N of M required.

    `public_stats` carries an `unavailable` block precisely so a consumer
    cannot quote a rate computed from too little, and this asserts the
    bot does not undo that by printing the small number it can see.
    """
    thin = {
        "chart": "win_rate",
        "sample_size": 4,
        "series": [],
        "summary": {
            "sufficiency": "INSUFFICIENT",
            "hit_rate": None,
            "precision": None,
            "unavailable": {
                "available": False,
                "reason": "insufficient_sample",
                "observed": 4,
                "required": 30,
            },
        },
        "caption": "4 published outcome(s) — below the 30 ARGUS requires.",
        "freshness": {},
        "provenance": {},
    }
    routes = {
        "/public/stats": _json(PUBLISHED_SUMMARY),
        "/public/stats/win_rate": _json(thin),
        "/public/releases": _json([]),
    }

    with _client(_routed(routes)) as client:
        snapshot = read_statistics(client, charts=("win_rate",))

    chart = snapshot.charts[0]
    assert not chart.available
    assert (chart.observed, chart.required) == (4, 30)

    text = statistics_text(snapshot)
    assert "4 of 30 outcomes required" in text
    assert "%" not in text.split("Charts and full detail")[0].replace("100%", "")


def test_a_published_chart_reports_the_numbers_the_api_gave():
    published = {
        "chart": "win_rate",
        "sample_size": 42,
        "series": [],
        "summary": {
            "sufficiency": "REPORTABLE",
            "resolved": 40,
            "hit_rate": 0.55,
            "precision": 0.6,
            "unavailable": None,
        },
        "caption": "Every concluded setup ARGUS has published.",
        "freshness": {},
        "provenance": {},
    }
    routes = {
        "/public/stats": _json(PUBLISHED_SUMMARY),
        "/public/stats/win_rate": _json(published),
        "/public/releases": _json(
            [
                {
                    "period_start": "2026-01-01",
                    "period_end": "2026-03-01",
                    "sequence_number": 2,
                    "status": "APPROVED",
                }
            ]
        ),
    }

    with _client(_routed(routes)) as client:
        snapshot = read_statistics(client, charts=("win_rate",))

    text = statistics_text(snapshot)
    assert "55.0%" in text
    assert "Sample: 42" in text
    assert "APPROVED" in text
    assert "through 2026-03-01" in text


def test_an_unreachable_service_is_its_own_message():
    """Distinct from both absences, because it is not about ARGUS at all.

    Telling a reader "no track record yet" because a container was
    restarting would be a false statement about the thing this bot exists
    to report honestly.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with _client(handler) as client:
        snapshot = read_statistics(client)

    assert snapshot.state is StatsState.UNREACHABLE

    text = statistics_text(snapshot)
    assert "could not be reached" in text
    assert "has not published" not in text
    assert "says nothing about the track record" in text


def test_a_five_hundred_from_the_stats_service_is_also_unreachable_not_empty():
    """A 503 from the gate is still not evidence about the track record."""
    with _client(lambda r: httpx.Response(503, json={"error": {"code": "X"}})) as client:
        snapshot = read_statistics(client)

    assert snapshot.state is StatsState.UNREACHABLE


def test_one_unreadable_chart_does_not_hide_the_ones_that_worked():
    """A partial answer beats no answer, and the absence is visible.

    The reader sees the win rate and simply does not see a cumulative
    line, which the sample-size copy has already taught them to expect.
    """
    routes = {
        "/public/stats": _json(PUBLISHED_SUMMARY),
        "/public/stats/win_rate": _json(
            {
                "chart": "win_rate",
                "sample_size": 42,
                "summary": {"hit_rate": 0.5, "unavailable": None},
                "caption": "",
                "freshness": {},
                "provenance": {},
            }
        ),
        "/public/releases": _json([]),
    }

    with _client(_routed(routes)) as client:
        snapshot = read_statistics(client)

    assert [chart.chart for chart in snapshot.charts] == ["win_rate"]
    assert "50.0%" in statistics_text(snapshot)


def test_the_message_carries_no_promotional_language():
    """Same restraint as the alert copy, asserted the same way."""
    routes = {
        "/public/stats": _json(PUBLISHED_SUMMARY),
        "/public/stats/win_rate": _json(
            {
                "chart": "win_rate",
                "sample_size": 42,
                "summary": {"hit_rate": 0.9, "unavailable": None},
                "caption": "",
                "freshness": {},
                "provenance": {},
            }
        ),
        "/public/releases": _json([]),
    }

    with _client(_routed(routes)) as client:
        text = statistics_text(read_statistics(client))

    for forbidden in ("buy", "profit", "opportunity", "guarantee", "beat the market", "returns of"):
        assert forbidden not in text.lower(), forbidden
    assert "not a recommendation" in text


def test_the_stats_client_sends_the_forwarded_proto_header_on_every_request():
    """Half of the private-network arrangement: the client's side.

    The other half — that `public_stats` in production accepts it — is
    the test below. Split in two because they fail for different reasons
    and a combined test would not say which.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(UNPUBLISHED_SUMMARY)

    with _client(handler) as client:
        client.get("/public/stats")
        client.get("/public/releases")

    assert seen
    for request in seen:
        assert request.headers[FORWARDED_PROTO_HEADER] == "https"


def test_a_production_profile_public_stats_answers_the_internal_call(
    committing_engine: Engine, security
):
    """The hop that would otherwise 308, proven against the real middleware.

    `public_stats` in production refuses plaintext, and the private
    network *is* plaintext — Railway terminates TLS at its edge. The
    header `StatsClient` sends is believed by `tls.py` only from a peer
    inside `RAILWAY_PRIVATE_NETWORK`, so this is the arrangement the
    header exists for rather than a bypass. Asserted end to end because
    the failure mode is completely invisible in development, where
    `require_https` is off.
    """
    app = TlsPolicyMiddleware(
        create_public_stats(committing_engine, security=security),
        profile=profile_for(Environment.PRODUCTION),
    )
    # `client=` sets the ASGI peer, which is what `request_scheme` trusts.
    internal = TestClient(app, client=("100.64.0.2", 40000))

    accepted = internal.get(
        "/public/stats",
        headers={FORWARDED_PROTO_HEADER: "https"},
        follow_redirects=False,
    )

    assert accepted.status_code == 200, "the internal hop was refused"
    assert "published" in accepted.json()


def test_without_the_forwarded_header_the_same_call_is_refused(committing_engine: Engine, security):
    """The control: proves the header is what makes the test above pass.

    Without it the production profile redirects to an `https://` URL
    nothing inside the private network is listening on — which is the
    bug this design avoids rather than a hypothetical one.
    """
    app = TlsPolicyMiddleware(
        create_public_stats(committing_engine, security=security),
        profile=profile_for(Environment.PRODUCTION),
    )
    internal = TestClient(app, client=("100.64.0.2", 40000))

    refused = internal.get("/public/stats", follow_redirects=False)

    assert refused.status_code in (307, 308, 426)


def test_an_untrusted_peer_cannot_use_the_header_to_bypass_the_policy(
    committing_engine: Engine, security
):
    """Which is what keeps the arrangement from being a hole.

    The same header from a public address is not believed, because
    `request_scheme` checks the peer before it reads it. If this ever
    stops being true, the internal convenience above has become a way for
    anyone to opt out of the TLS policy.
    """
    app = TlsPolicyMiddleware(
        create_public_stats(committing_engine, security=security),
        profile=profile_for(Environment.PRODUCTION),
    )
    outsider = TestClient(app, client=("203.0.113.9", 40000))

    response = outsider.get(
        "/public/stats",
        headers={FORWARDED_PROTO_HEADER: "https"},
        follow_redirects=False,
    )

    assert response.status_code in (307, 308, 426)
