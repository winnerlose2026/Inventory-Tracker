"""Session key fallback, password-version sessions, named tokens, admin gate."""
import importlib

import pytest

import app as app_mod
from core import auth

MASTER = "master-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", MASTER)
    monkeypatch.setenv("INVENTORY_USERNAMES", "jd,jay")
    monkeypatch.setenv("INVENTORY_PASSWORD", "pw1")
    monkeypatch.delenv("INVENTORY_API_TOKENS", raising=False)
    monkeypatch.delenv("INVENTORY_ADMINS", raising=False)
    monkeypatch.delenv("SESSION_REQUIRE_PWV", raising=False)
    app_mod.app.config["TESTING"] = True
    app_mod._LOGIN_FAILS.clear()
    return app_mod.app.test_client()


def _login(c, user="jd", pw="pw1"):
    return c.post("/login", data={"username": user, "password": pw})


# --- secret key -----------------------------------------------------------

def test_secret_key_prefers_the_env_var(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "abc")
    assert auth.derive_secret_key() == ("abc", "FLASK_SECRET_KEY")


def test_secret_key_falls_back_to_a_stable_hash_of_the_api_token(monkeypatch):
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    k1, src = auth.derive_secret_key()
    k2, _ = auth.derive_secret_key()
    assert k1 == k2 and len(k1) == 64 and "tok" not in k1
    assert src == "derived:INVENTORY_API_TOKEN"
    monkeypatch.setenv("INVENTORY_API_TOKEN", "other")
    assert auth.derive_secret_key()[0] != k1


def test_secret_key_never_raises_with_nothing_set(monkeypatch):
    for n in ("FLASK_SECRET_KEY", "SECRET_KEY", "INVENTORY_API_TOKEN",
              "INVENTORY_PASSWORD"):
        monkeypatch.delenv(n, raising=False)
    key, src = auth.derive_secret_key()
    assert key and src == "random"


def test_request_body_cap_is_set():
    assert app_mod.app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


# --- sessions -------------------------------------------------------------

def test_login_stamps_pwv_and_password_change_ends_the_session(client, monkeypatch):
    assert _login(client).status_code == 302
    with client.session_transaction() as s:
        assert s["pwv"] == auth.password_version()
    assert client.get("/api/warehouses").status_code == 200
    monkeypatch.setenv("INVENTORY_PASSWORD", "pw2")
    assert client.get("/api/warehouses").status_code == 401


def test_user_removed_from_allowlist_is_signed_out(client, monkeypatch):
    _login(client, "jay")
    assert client.get("/api/warehouses").status_code == 200
    monkeypatch.setenv("INVENTORY_USERNAMES", "jd")
    assert client.get("/api/warehouses").status_code == 401


def test_legacy_session_without_pwv_keeps_working_until_flag_is_on(client, monkeypatch):
    with client.session_transaction() as s:
        s["user"] = "JD"                       # issued before this release
    assert client.get("/api/warehouses").status_code == 200
    monkeypatch.setenv("SESSION_REQUIRE_PWV", "on")
    assert client.get("/api/warehouses").status_code == 401


def test_get_logout_does_not_end_the_session_but_post_does(client):
    _login(client)
    client.get("/logout")
    assert client.get("/api/warehouses").status_code == 200
    client.post("/logout")
    assert client.get("/api/warehouses").status_code == 401


# --- named tokens ---------------------------------------------------------

def _h(tok):
    return {"X-Inventory-Token": tok}


def test_master_token_still_has_full_access(client):
    assert client.get("/api/warehouses", headers=_h(MASTER)).status_code == 200
    r = client.get("/api/auth/check", headers=_h(MASTER)).get_json()
    assert r["authorized"] and r["auth_type"] == "token"


def test_named_tokens_are_limited_to_their_scope(client, monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKENS",
                       "viewer:rrr:read, scanner:iii:ingest,chaser:sss:send,"
                       "ops:fff:full,broken,bad:x:nonsense")
    assert client.get("/api/warehouses", headers=_h("rrr")).status_code == 200
    r = client.post("/api/use", json={"name": "x", "amount": 1}, headers=_h("rrr"))
    assert r.status_code == 403 and r.get_json()["error"] == "forbidden"
    assert client.get("/api/admin/backups", headers=_h("rrr")).status_code == 403
    assert client.get("/api/warehouses", headers=_h("iii")).status_code == 403
    assert client.get("/api/scan/health", headers=_h("iii")).status_code == 200
    assert client.get("/api/warehouses", headers=_h("sss")).status_code == 403
    assert client.get("/api/warehouses", headers=_h("fff")).status_code == 200
    assert client.get("/api/warehouses", headers=_h("x")).status_code == 401
    assert client.get("/api/warehouses", headers=_h("nope")).status_code == 401


def test_scope_rules():
    assert auth.scope_allows({"ingest"}, "POST", "/api/email/ingest-events")
    assert auth.scope_allows({"ingest"}, "POST", "/api/ingest/cheney-inventory-csv")
    assert not auth.scope_allows({"ingest"}, "POST", "/api/email/send")
    assert auth.scope_allows({"send"}, "POST", "/api/email/send")
    assert not auth.scope_allows({"send"}, "POST", "/api/email/sendx")
    assert auth.scope_allows({"backup"}, "GET", "/api/admin/backups/latest")
    assert not auth.scope_allows({"backup"}, "POST", "/api/admin/backups/x/restore")
    assert not auth.scope_allows({"read"}, "GET", "/api/admin/backups/latest")
    assert auth.scope_allows({"read", "send"}, "HEAD", "/api/inventory")
    assert auth.scope_allows({"full"}, "DELETE", "/anything")


def test_activity_log_names_the_token(client, monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKENS", "nightly-scan:iii:ingest")
    app_mod.app.config["ACTIVITY_LOG"] = True
    try:
        client.post("/api/email/ingest-events", json={"events": []}, headers=_h("iii"))
    finally:
        app_mod.app.config["ACTIVITY_LOG"] = False
    from core import activity
    assert activity.load()[0]["actor"] == "token:nightly-scan"


# --- admin gate -----------------------------------------------------------

def test_everyone_is_admin_until_INVENTORY_ADMINS_is_set(client, monkeypatch):
    _login(client, "jay")
    r = client.post("/api/seed", json={"reset": True})
    assert r.status_code == 400                    # reached the confirm check
    monkeypatch.setenv("INVENTORY_ADMINS", "jd")
    r = client.post("/api/seed", json={"reset": True})
    assert r.status_code == 403 and r.get_json()["error"] == "admin only"
    r = client.post("/api/admin/restore-inventory", json={"items": []})
    assert r.status_code == 403
    # the master token is always admin
    r = client.post("/api/admin/restore-inventory", json={"items": []},
                    headers=_h(MASTER))
    assert r.status_code == 400
