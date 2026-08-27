"""Cross-user isolation under real authentication. The adversarial version.

Module 19 tested this under the stub, with two hardcoded UUIDs and a
header naming whichever one the test wanted. That proved the *queries*
were scoped. It could not prove anything about authentication, because
there was none: under the stub, "being Bob" was a matter of typing Bob's
id.

These tests use two real accounts with real passwords and real sessions,
and Mallory actively tries to reach Alice's data — with her user id, with
her session id, with a forged token, with her id in the stub header, and
with her session after it has been ended. Every route that Module 19 and
Module 21 expose to a user is swept, so a new user-scoped route added
later without ownership scoping fails here.
"""

from __future__ import annotations

import secrets
from uuid import uuid4

from sqlalchemy import select

from infra.db.schema.users import user_watchlists
from services.terminal.identity import USER_HEADER
from tests.integration.identity.conftest import GOOD_PASSWORD


def test_a_real_session_reaches_module_19s_watchlists(pair, terminal):
    """The premise: a Bearer token works against Module 19 with no change to it."""
    alice, _mallory = pair

    listed = terminal.get("/terminal/watchlists", headers=alice["headers"])

    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["Alice's bases"]


def test_mallory_sees_none_of_alices_watchlists(pair, terminal):
    alice, mallory = pair

    listed = terminal.get("/terminal/watchlists", headers=mallory["headers"])

    assert listed.status_code == 200
    assert listed.json() == []


def test_mallory_cannot_read_alices_watchlist_by_id(pair, terminal):
    """A 404, not a 403 — Module 19 chose that so membership cannot be probed."""
    alice, mallory = pair

    response = terminal.get(
        f"/terminal/watchlists/{alice['watchlist_id']}", headers=mallory["headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WATCHLIST_NOT_FOUND"


def test_mallory_cannot_write_to_alices_watchlist(pair, terminal, connection, register):
    """And the refusal is checked against the stored rows, not just the status.

    Module 19's report records a near-miss here: an ownership test passed
    against a deliberately broken check, because the write was rolled back
    by the transaction and the error came from the read-back. So this
    asserts what Alice's list actually contains afterwards.
    """
    alice, mallory = pair
    security_id = register("TGT")

    before = _items(connection, alice["watchlist_id"])
    response = terminal.post(
        f"/terminal/watchlists/{alice['watchlist_id']}/securities",
        headers=mallory["headers"],
        json={"ticker": "TGT"},
    )

    assert response.status_code == 404
    assert _items(connection, alice["watchlist_id"]) == before
    assert str(security_id) not in str(before)


def test_mallory_cannot_rename_or_delete_alices_watchlist(pair, terminal, connection):
    alice, mallory = pair

    renamed = terminal.patch(
        f"/terminal/watchlists/{alice['watchlist_id']}",
        headers=mallory["headers"],
        json={"name": "Mallory was here"},
    )
    removed = terminal.delete(
        f"/terminal/watchlists/{alice['watchlist_id']}", headers=mallory["headers"]
    )

    assert renamed.status_code == 404
    assert removed.status_code == 404

    row = connection.execute(
        select(user_watchlists).where(user_watchlists.c.id == alice["watchlist_id"])
    ).one_or_none()
    assert row is not None, "Alice's list still exists"
    assert row.name == "Alice's bases", "and still has her name"


def test_knowing_alices_user_id_buys_mallory_nothing(pair, terminal):
    """The stub's whole security model was "do not know the other id". Gone.

    Under real auth the user id is not a credential. Mallory holds Alice's
    id — it is in every response about her — and sending it as the stub
    header alongside her own valid session must not change who she is.
    """
    alice, mallory = pair

    with_alices_id = terminal.get(
        "/terminal/watchlists",
        headers={**mallory["headers"], USER_HEADER: str(alice["user_id"])},
    )

    assert with_alices_id.status_code == 200
    assert with_alices_id.json() == [], "still Mallory, not Alice"


def test_alices_session_id_is_not_a_credential(pair, terminal):
    """Session ids appear in `/identity/sessions`. They are identifiers, not tokens."""
    alice, mallory = pair

    response = terminal.get(
        "/terminal/watchlists",
        headers={"Authorization": f"Bearer {alice['session_id']}"},
    )

    assert response.status_code == 401


def test_a_forged_token_is_refused_by_module_19_too(pair, terminal):
    """The seam verifies; it does not merely parse."""
    _alice, _mallory = pair

    response = terminal.get(
        "/terminal/watchlists",
        headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
    )

    assert response.status_code == 401


def test_alices_session_stops_reaching_module_19_after_she_logs_out(pair, terminal, client):
    """Logout in Module 22 takes effect in Module 19, because there is one session store."""
    alice, _mallory = pair
    assert terminal.get("/terminal/watchlists", headers=alice["headers"]).status_code == 200

    assert client.post("/identity/logout", headers=alice["headers"]).status_code == 204

    after = terminal.get("/terminal/watchlists", headers=alice["headers"])
    assert after.status_code == 401


def test_changing_alices_password_locks_a_stolen_session_out_of_module_19(pair, terminal, client):
    """The response to a compromise actually works across service boundaries."""
    alice, _mallory = pair
    stolen = dict(alice["headers"])

    changed = client.post(
        "/identity/password",
        headers=alice["headers"],
        json={"current_password": GOOD_PASSWORD, "new_password": "a-fresh-long-passphrase"},
    )

    assert changed.status_code == 204
    assert terminal.get("/terminal/watchlists", headers=stolen).status_code == 401


def test_a_deactivated_account_loses_module_19_immediately(pair, terminal, connection):
    """Not "at the next expiry". `sessions.verify` joins `users` for this."""
    alice, _mallory = pair
    from services.identity import accounts

    accounts.deactivate(connection, alice["user_id"])

    assert terminal.get("/terminal/watchlists", headers=alice["headers"]).status_code == 401


def test_every_user_scoped_terminal_route_refuses_mallory(pair, terminal, connection):
    """A sweep, so a route added later without scoping fails here.

    Enumerated from the app's own OpenAPI paths rather than listed by
    hand: a hand-written list is a list that goes stale the first time
    somebody adds a route.
    """
    alice, mallory = pair
    watchlist_routes = [
        path for path in terminal.app.openapi()["paths"] if path.startswith("/terminal/watchlists")
    ]
    assert len(watchlist_routes) >= 3, "the sweep found the routes it is meant to sweep"

    for path in watchlist_routes:
        concrete = path.replace("{watchlist_id}", str(alice["watchlist_id"])).replace(
            "{security_id}", str(uuid4())
        )
        if "{" in concrete:
            continue
        for method in ("get", "patch", "delete"):
            call = getattr(terminal, method)
            response = (
                call(concrete, headers=mallory["headers"], json={"name": "taken"})
                if method == "patch"
                else call(concrete, headers=mallory["headers"])
            )
            if response.status_code == 405:
                continue
            assert response.status_code in (200, 404), (
                f"{method.upper()} {concrete} answered {response.status_code}"
            )
            if response.status_code == 200 and concrete == "/terminal/watchlists":
                assert response.json() == [], "Mallory's own list, and it is empty"


def test_alice_still_has_everything_she_started_with(pair, terminal, connection):
    """After the whole adversarial sweep, nothing of Alice's moved."""
    alice, _mallory = pair

    listed = terminal.get("/terminal/watchlists", headers=alice["headers"])

    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["Alice's bases"]


def _items(connection, watchlist_id) -> list:
    from infra.db.schema.users import user_watchlist_items

    return connection.execute(
        select(user_watchlist_items.c.security_id).where(
            user_watchlist_items.c.watchlist_id == watchlist_id
        )
    ).all()
