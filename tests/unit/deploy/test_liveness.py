"""The liveness probe added by composition. No database — the health check is faked.

Railway will not route traffic to a container whose health check does not
answer. Only Module 24's health service has `/health/live`; the four API
services would 404 the platform's poll and never receive traffic. Adding
the route to Modules 19-22 would be changing their application logic, so
it is wrapped on at the deployment layer instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from infra.deploy import liveness as liveness_module
from infra.deploy.liveness import LIVENESS_PATH, LivenessMiddleware, serves_liveness


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _Report:
    def __init__(self, value: str, http_status: int) -> None:
        self.status = _Status(value)
        self.http_status = http_status


@pytest.fixture
def health(monkeypatch):
    """Replace Module 23's check with one a test controls. Counts its calls."""
    state = {"calls": 0, "report": _Report("ok", 200)}

    def _check(engine: Any) -> _Report:
        state["calls"] += 1
        return state["report"]

    monkeypatch.setattr(liveness_module, "check_health", _check)
    return state


async def _thing(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _service(*, own_route: bool = False) -> Starlette:
    routes = [Route("/thing", _thing)]
    if own_route:
        routes.append(Route(LIVENESS_PATH, _thing))
    return Starlette(routes=routes)


def _client(app: Starlette, *, claim_path: bool = True) -> TestClient:
    return TestClient(
        LivenessMiddleware(app, engine=object(), claim_path=claim_path),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    )


def test_a_service_without_the_route_answers_the_probe(health):
    """This is the bug the middleware exists for: without it, Railway polls a 404."""
    assert _service().routes[0].path == "/thing"
    response = _client(_service()).get(LIVENESS_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "up"}


def test_the_service_without_the_middleware_really_would_404(health):
    """Stated as a test so the middleware is not protecting against nothing."""
    assert TestClient(_service()).get(LIVENESS_PATH).status_code == 404


def test_everything_else_is_delegated(health):
    assert _client(_service()).get("/thing").json() == {"ok": True}
    assert health["calls"] == 0


def test_a_down_verdict_takes_the_container_out_of_rotation(health):
    health["report"] = _Report("down", 503)
    response = _client(_service()).get(LIVENESS_PATH)
    assert response.status_code == 503
    assert response.json() == {"status": "down"}


def test_a_degraded_container_stays_in_rotation(health):
    """Module 24's decision, unchanged: a stale feed is not fixed by fewer servers."""
    health["report"] = _Report("degraded", 200)
    response = _client(_service()).get(LIVENESS_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "up"}


def test_the_verdict_is_cached_across_a_poll_burst(health):
    """Seven containers polled every few seconds is a steady background load
    that exists only to answer a question whose answer rarely changes."""
    client = _client(_service())
    for _ in range(5):
        client.get(LIVENESS_PATH)
    assert health["calls"] == 1


def test_a_service_that_has_its_own_route_is_detected():
    assert serves_liveness(_service(own_route=True))
    assert not serves_liveness(_service())
    assert not serves_liveness(object())


def test_the_middleware_delegates_rather_than_shadowing_an_existing_route(health):
    """Two definitions of liveness on one path, outer one winning, is invisible
    until the two disagree."""
    client = _client(_service(own_route=True), claim_path=False)
    assert client.get(LIVENESS_PATH).json() == {"ok": True}
    assert health["calls"] == 0


def test_the_probe_carries_the_security_headers(health):
    response = _client(_service()).get(LIVENESS_PATH)
    assert response.headers["x-content-type-options"] == "nosniff"
