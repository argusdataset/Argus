"""User watchlist CRUD, and the isolation that makes it safe.

Two things get most of the attention here. One is the full round trip,
because a CRUD surface is only correct end to end. The other is that two
users' lists never touch — which is the one bug in this module that would
be a security incident rather than an inconvenience.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.terminal.identity import USER_HEADER

# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_the_full_lifecycle_of_a_watchlist(client, register, make_user, as_user):
    user = make_user("alice")
    headers = as_user(user)
    register("MLSS")
    register("SLS")

    created = client.post("/terminal/watchlists", json={"name": "Long Term"}, headers=headers)
    assert created.status_code == 201
    watchlist_id = created.json()["id"]
    assert created.json()["items"] == []

    added = client.post(
        f"/terminal/watchlists/{watchlist_id}/items",
        json={"ticker": "MLSS"},
        headers=headers,
    ).json()
    assert [item["ticker"] for item in added["items"]] == ["MLSS"]

    client.post(
        f"/terminal/watchlists/{watchlist_id}/items",
        json={"ticker": "SLS"},
        headers=headers,
    )

    renamed = client.patch(
        f"/terminal/watchlists/{watchlist_id}",
        json={"name": "Core Holdings"},
        headers=headers,
    ).json()
    assert renamed["name"] == "Core Holdings"
    assert len(renamed["items"]) == 2

    listed = client.get("/terminal/watchlists", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Core Holdings"
    assert listed[0]["item_count"] == 2

    removed = client.delete(
        f"/terminal/watchlists/{watchlist_id}/items/MLSS", headers=headers
    ).json()
    assert [item["ticker"] for item in removed["items"]] == ["SLS"]

    assert client.delete(f"/terminal/watchlists/{watchlist_id}", headers=headers).status_code == 204
    assert client.get("/terminal/watchlists", headers=headers).json() == []


def test_a_watchlist_stores_identity_and_shows_the_current_ticker(
    client, register, make_user, as_user, connection
):
    """Module 03 made the item a foreign key to identity, not a ticker
    string, so a list survives a rename. This asserts the API preserves
    that rather than quietly storing a symbol."""
    from datetime import UTC, datetime

    from data.canonical_model.exchanges import CanonicalExchange
    from data.normalization.identity import SecurityIdentityResolver

    user = make_user("bob")
    headers = as_user(user)
    security_id = register("OLDTK")

    created = client.post("/terminal/watchlists", json={"name": "L"}, headers=headers).json()
    client.post(
        f"/terminal/watchlists/{created['id']}/items",
        json={"ticker": "OLDTK"},
        headers=headers,
    )

    SecurityIdentityResolver(connection).record_ticker_change(
        old_symbol="OLDTK",
        new_symbol="NEWTK",
        changed_at=datetime(2026, 3, 1, tzinfo=UTC),
        exchange=CanonicalExchange.NASDAQ,
    )

    detail = client.get(f"/terminal/watchlists/{created['id']}", headers=headers).json()

    assert detail["items"][0]["security_id"] == str(security_id)
    assert detail["items"][0]["ticker"] == "NEWTK"


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_two_users_watchlists_never_leak_into_each_other(client, register, make_user, as_user):
    alice, bob = make_user("alice"), make_user("bob")
    register("MLSS")
    register("HIVE")

    alice_list = client.post(
        "/terminal/watchlists", json={"name": "Mine"}, headers=as_user(alice)
    ).json()
    client.post(
        f"/terminal/watchlists/{alice_list['id']}/items",
        json={"ticker": "MLSS"},
        headers=as_user(alice),
    )
    bob_list = client.post(
        "/terminal/watchlists", json={"name": "Mine"}, headers=as_user(bob)
    ).json()
    client.post(
        f"/terminal/watchlists/{bob_list['id']}/items",
        json={"ticker": "HIVE"},
        headers=as_user(bob),
    )

    alice_view = client.get("/terminal/watchlists", headers=as_user(alice)).json()
    bob_view = client.get("/terminal/watchlists", headers=as_user(bob)).json()

    assert [row["id"] for row in alice_view] == [alice_list["id"]]
    assert [row["id"] for row in bob_view] == [bob_list["id"]]
    # Same name, different lists — the uniqueness constraint is per user.
    assert alice_list["id"] != bob_list["id"]


def test_reading_someone_elses_watchlist_is_not_found_not_forbidden(client, make_user, as_user):
    """A 403 would confirm the list exists, which leaks that some other
    user has one with that id."""
    alice, bob = make_user("alice"), make_user("bob")
    alice_list = client.post(
        "/terminal/watchlists", json={"name": "Private"}, headers=as_user(alice)
    ).json()

    response = client.get(f"/terminal/watchlists/{alice_list['id']}", headers=as_user(bob))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WATCHLIST_NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "", {"name": "hijacked"}),
        ("delete", "", None),
        ("post", "/items", {"ticker": "MLSS"}),
        ("delete", "/items/MLSS", None),
    ],
)
def test_no_write_path_reaches_another_users_watchlist(
    client, register, make_user, as_user, method, suffix, body
):
    """Every mutation, not just the read. Ownership is in the WHERE clause
    of each one rather than a check someone can forget to repeat."""
    alice, bob = make_user("alice"), make_user("bob")
    register("MLSS")
    target = client.post(
        "/terminal/watchlists", json={"name": "Alice's"}, headers=as_user(alice)
    ).json()

    client.post(
        f"/terminal/watchlists/{target['id']}/items",
        json={"ticker": "MLSS"},
        headers=as_user(alice),
    )
    before = client.get(f"/terminal/watchlists/{target['id']}", headers=as_user(alice)).json()

    call = getattr(client, method)
    kwargs = {"headers": as_user(bob)}
    if body is not None:
        kwargs["json"] = body
    response = call(f"/terminal/watchlists/{target['id']}{suffix}", **kwargs)

    assert response.status_code == 404

    # And Alice's list is byte-for-byte what it was. Asserting only the
    # 404 is not enough: an earlier version of this test did exactly that,
    # and a deliberately broken ownership check still passed it — the
    # write was undone by the transaction rolling back on the way out,
    # two layers below the check that was supposed to stop it. Correct
    # behaviour, arrived at accidentally, is not a guarantee.
    after = client.get(f"/terminal/watchlists/{target['id']}", headers=as_user(alice)).json()
    assert after == before


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_a_user_scoped_endpoint_refuses_a_request_with_no_identity(client):
    response = client.get("/terminal/watchlists")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "IDENTITY_REQUIRED"
    assert USER_HEADER in response.json()["error"]["message"]


def test_an_identity_naming_no_user_is_refused_rather_than_trusted(client):
    """The stub trusts a header, which is why it must at least insist the
    header names something real — otherwise it is a way to invent users."""
    response = client.get("/terminal/watchlists", headers={USER_HEADER: str(uuid4())})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "IDENTITY_REQUIRED"


def test_a_malformed_identity_is_refused(client):
    response = client.get("/terminal/watchlists", headers={USER_HEADER: "not-a-uuid"})

    assert response.status_code == 401


def test_disabling_the_stub_closes_every_user_scoped_endpoint(
    connection, register, make_user, as_user
):
    """Turning the stub off is a one-line deployment change, so a
    deployment that ships it has *left it on* rather than forgotten to
    remove it. A 501 naming Module 22, not a 401 — the caller did nothing
    wrong."""
    from fastapi.testclient import TestClient

    from services.terminal.app import create_app, get_connection
    from services.terminal.config import TerminalConfig

    user = make_user("carol")
    app = create_app(engine=None, config=TerminalConfig(stub_identity_enabled=False))  # type: ignore[arg-type]

    def _connection():
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as locked:
        response = locked.get("/terminal/watchlists", headers=as_user(user))

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "IDENTITY_UNAVAILABLE"
    assert "Module 22" in response.json()["error"]["message"]


def test_disabling_the_stub_leaves_public_endpoints_working(connection, register):
    """Company data is not user-scoped and must not be collateral damage."""
    from fastapi.testclient import TestClient

    from services.terminal.app import create_app, get_connection
    from services.terminal.config import TerminalConfig

    register("PUBLIC")
    app = create_app(engine=None, config=TerminalConfig(stub_identity_enabled=False))  # type: ignore[arg-type]

    def _connection():
        savepoint = connection.begin_nested()
        try:
            yield connection
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

    app.dependency_overrides[get_connection] = _connection
    with TestClient(app, raise_server_exceptions=False) as locked:
        assert locked.get("/terminal/companies/PUBLIC/fundamentals").status_code == 200


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_adding_the_same_security_twice_is_not_an_error(client, register, make_user, as_user):
    """A double-tapped "add" expressed one intention, and the second tap
    should leave the user where they wanted to be."""
    headers = as_user(make_user("dave"))
    register("MLSS")
    created = client.post("/terminal/watchlists", json={"name": "L"}, headers=headers).json()

    first = client.post(
        f"/terminal/watchlists/{created['id']}/items",
        json={"ticker": "MLSS"},
        headers=headers,
    )
    second = client.post(
        f"/terminal/watchlists/{created['id']}/items",
        json={"ticker": "MLSS"},
        headers=headers,
    )

    assert first.status_code == second.status_code == 200
    assert len(second.json()["items"]) == 1


def test_removing_a_security_that_is_not_on_the_list_is_not_an_error(
    client, register, make_user, as_user
):
    headers = as_user(make_user("erin"))
    register("MLSS")
    created = client.post("/terminal/watchlists", json={"name": "L"}, headers=headers).json()

    response = client.delete(f"/terminal/watchlists/{created['id']}/items/MLSS", headers=headers)

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_a_duplicate_name_for_one_user_is_refused_with_a_code(client, make_user, as_user):
    headers = as_user(make_user("frank"))
    client.post("/terminal/watchlists", json={"name": "Same"}, headers=headers)

    response = client.post("/terminal/watchlists", json={"name": "Same"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WATCHLIST_NAME_TAKEN"


def test_an_empty_name_is_refused(client, make_user, as_user):
    headers = as_user(make_user("gina"))

    response = client.post("/terminal/watchlists", json={"name": "   "}, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_adding_an_unknown_ticker_is_a_404(client, make_user, as_user):
    headers = as_user(make_user("hank"))
    created = client.post("/terminal/watchlists", json={"name": "L"}, headers=headers).json()

    response = client.post(
        f"/terminal/watchlists/{created['id']}/items",
        json={"ticker": "NOPE"},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SECURITY_NOT_FOUND"


def test_the_watchlist_count_limit_is_enforced(client, make_user, as_user):
    from fastapi.testclient import TestClient

    from services.terminal.app import create_app, get_connection
    from services.terminal.config import TerminalConfig, TerminalLimit, TerminalLimits

    headers = as_user(make_user("iris"))
    tight = TerminalConfig(
        limits=TerminalLimits(
            max_watchlists_per_user=TerminalLimit(value=1.0, kind="calibratable", rationale="test")
        )
    )
    app = create_app(engine=None, config=tight)  # type: ignore[arg-type]
    app.dependency_overrides[get_connection] = client.app.dependency_overrides[get_connection]

    with TestClient(app, raise_server_exceptions=False) as limited:
        assert (
            limited.post("/terminal/watchlists", json={"name": "One"}, headers=headers).status_code
            == 201
        )
        response = limited.post("/terminal/watchlists", json={"name": "Two"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WATCHLIST_LIMIT_REACHED"


def test_items_keep_the_order_the_user_put_them_in(client, register, make_user, as_user):
    headers = as_user(make_user("jane"))
    for ticker in ("AAA", "BBB", "CCC"):
        register(ticker)
    created = client.post("/terminal/watchlists", json={"name": "L"}, headers=headers).json()

    for ticker in ("CCC", "AAA", "BBB"):
        client.post(
            f"/terminal/watchlists/{created['id']}/items",
            json={"ticker": ticker},
            headers=headers,
        )

    detail = client.get(f"/terminal/watchlists/{created['id']}", headers=headers).json()

    assert [item["ticker"] for item in detail["items"]] == ["CCC", "AAA", "BBB"]


def test_the_ownership_check_itself_refuses_a_foreign_watchlist(connection, register, make_user):
    """The service layer, called directly, asserting the *effect*.

    Two earlier versions of this test passed against a deliberately broken
    ownership check, each for a different reason. The first asserted only
    the HTTP status: the write happened and was then undone by the
    transaction rolling back when the read-back failed. The second
    asserted the raised code — but with the check removed, `add_security`
    still raises `WATCHLIST_NOT_FOUND`, from the `read_watchlist` call at
    the end, *after* inserting the row.

    So this asserts what actually matters: Alice's list is unchanged.
    Called directly rather than over HTTP so no transaction rollback can
    quietly cover for a missing check.
    """
    from services.terminal.errors import TerminalError
    from services.terminal.watchlists import add_security, create_watchlist, read_watchlist

    alice, bob = make_user("alice"), make_user("bob")
    register("MLSS")
    watchlist = create_watchlist(connection, alice, "Alice's")

    with pytest.raises(TerminalError) as exc_info:
        add_security(connection, bob, watchlist.id, "MLSS")

    assert exc_info.value.code == "WATCHLIST_NOT_FOUND"
    assert read_watchlist(connection, alice, watchlist.id).items == []


def test_the_ownership_check_also_refuses_a_foreign_removal(connection, register, make_user):
    """Same, in the other direction: Bob must not be able to empty
    Alice's list."""
    from services.terminal.errors import TerminalError
    from services.terminal.watchlists import (
        add_security,
        create_watchlist,
        read_watchlist,
        remove_security,
    )

    alice, bob = make_user("alice"), make_user("bob")
    register("MLSS")
    watchlist = create_watchlist(connection, alice, "Alice's")
    add_security(connection, alice, watchlist.id, "MLSS")

    with pytest.raises(TerminalError):
        remove_security(connection, bob, watchlist.id, "MLSS")

    assert [item.ticker for item in read_watchlist(connection, alice, watchlist.id).items] == [
        "MLSS"
    ]
