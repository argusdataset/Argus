"""ARGUS Public's static mount, composed onto public_stats without touching it.

No database: `mount_public_web` operates on any FastAPI app, so these
tests build a throwaway one rather than the real `create_app()` — the
question here is what the mount itself does, not what Module 20 does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from infra.deploy.public_web import FRONTEND_CSP, WEB_PUBLIC_DIR, mount_public_web


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<title>stub</title>hello")
    (tmp_path / "style.css").write_text("body { color: red; }")
    return tmp_path


def _app_with_api() -> FastAPI:
    """A stand-in for `create_app()`: an app with a route already registered."""
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict:
        return {"ok": True}

    return app


def test_the_real_web_public_directory_exists_and_has_an_index():
    """Guards against the frontend being deleted without updating this pointer."""
    assert WEB_PUBLIC_DIR.is_dir()
    assert (WEB_PUBLIC_DIR / "index.html").is_file()
    assert (WEB_PUBLIC_DIR / "app.js").is_file()
    assert (WEB_PUBLIC_DIR / "style.css").is_file()


def test_missing_directory_raises_rather_than_booting_silently(tmp_path: Path):
    """A public_stats container without its frontend should fail loudly."""
    app = _app_with_api()
    with pytest.raises(RuntimeError, match="web/public not found"):
        mount_public_web(app, directory=tmp_path / "does-not-exist")


def test_the_static_index_is_served_at_root(static_dir: Path):
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "stub" in response.text


def test_other_static_files_are_served_by_name(static_dir: Path):
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/style.css")
    assert response.status_code == 200
    assert "color: red" in response.text


def test_an_existing_api_route_is_not_shadowed_by_the_mount(static_dir: Path):
    """The whole point: routes registered before the mount still win.

    Starlette matches in registration order — `mount_public_web` is
    called *after* the API route exists, so the mount only ever catches
    what nothing else claimed.
    """
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/api/thing")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_an_unknown_path_is_a_plain_404_not_the_index(static_dir: Path):
    """`html=True` serves index.html for `/`, not for every miss —
    this page has no client-side router, so a 404 should stay a 404."""
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/nonexistent-path")
    assert response.status_code == 404


def test_mount_public_web_returns_the_same_app_instance(static_dir: Path):
    app = _app_with_api()
    assert mount_public_web(app, directory=static_dir) is app


# --------------------------------------------------------------------------
# The CSP override
# --------------------------------------------------------------------------


def test_the_frontends_own_responses_carry_the_permissive_csp(static_dir: Path):
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.headers["content-security-policy"] == FRONTEND_CSP


def test_the_frontend_csp_is_same_origin_only():
    """Never widened to allow a third-party host or inline execution.

    This page loads exactly two same-origin resources and nothing else —
    a policy permitting more than `'self'` would be granting something
    nothing here uses.
    """
    assert "unsafe-inline" not in FRONTEND_CSP
    assert "unsafe-eval" not in FRONTEND_CSP
    assert "*" not in FRONTEND_CSP
    assert "'self'" in FRONTEND_CSP


def test_an_api_routes_response_is_unaffected_by_the_frontend_csp(static_dir: Path):
    """The override applies to the static mount, not to the whole app.

    Without `SecurityHeadersMiddleware` in this throwaway app, an API
    route simply has no CSP header at all here — the guarantee this test
    checks is narrower and more important: the override must not leak
    onto responses it was never meant to touch.
    """
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/api/thing")
    assert "content-security-policy" not in response.headers


def test_the_override_replaces_rather_than_duplicates_an_existing_header(static_dir: Path):
    """Exactly one CSP header reaches the client, never two stacked values.

    A browser given two `Content-Security-Policy` headers enforces the
    *intersection* of both, which would silently reintroduce whatever
    Module 24's default forbids — so this proves there is one value, not
    that a permissive one happens to appear somewhere in the list.
    """
    app = mount_public_web(_app_with_api(), directory=static_dir)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.headers.get_list("content-security-policy") == [FRONTEND_CSP]
