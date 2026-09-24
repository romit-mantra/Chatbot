"""Auth layer tests — through the real HTTP app (TestClient).
Proves: login works; wrong password refused; no/bad token -> 401; identity
comes from the token (a body cannot spoof actor); approvals use the token
identity for RBAC; logout invalidates; expired sessions are rejected."""

import os, sys, uuid, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHITRAGUPTA_DB",
                       f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # fresh app instance per test (module-level state)
    for m in ("app",):
        sys.modules.pop(m, None)
    import app as appmod
    return TestClient(appmod.app), appmod


def _login(c, user, pw="demo"):
    r = c.post("/api/login", json={"user": user, "password": pw})
    return r


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_login_and_wrong_password(client):
    c, _ = client
    ok = _login(c, "ravi")
    assert ok.status_code == 200 and ok.json()["roles"] == ["Purchase User"]
    bad = _login(c, "ravi", "wrong")
    assert bad.status_code == 401
    unknown = _login(c, "mallory")
    assert unknown.status_code == 401


def test_endpoints_require_token(client):
    c, _ = client
    assert c.post("/api/command", json={"text": "hi"}).status_code == 401
    assert c.get("/api/brief").status_code == 401
    assert c.get("/api/memory").status_code == 401
    bad = c.post("/api/command", json={"text": "hi"},
                 headers=_auth("garbage-token"))
    assert bad.status_code == 401


def test_identity_comes_from_token_not_body(client):
    c, _ = client
    tok = _login(c, "meera").json()["token"]          # Sales user
    # even if a client tries to smuggle an actor field, it is ignored:
    r = c.post("/api/command",
               json={"text": "raise a PO for the screw", "actor": "ravi"},
               headers=_auth(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["proposals"] == []                     # RBAC refused Meera
    assert "role" in body["reply"].lower()


def test_approve_uses_token_identity(client):
    c, _ = client
    ravi = _login(c, "ravi").json()["token"]
    meera = _login(c, "meera").json()["token"]
    r = c.post("/api/command", json={"text": "raise a PO for the screw"},
               headers=_auth(ravi)).json()
    pid = r["proposals"][0]["id"]
    denied = c.post(f"/api/proposals/{pid}/approve", json={},
                    headers=_auth(meera)).json()
    assert "error" in denied                           # meera can't approve POs
    ok = c.post(f"/api/proposals/{pid}/approve", json={},
                headers=_auth(ravi)).json()
    assert ok["readback_ok"] is True


def test_memory_is_bound_to_session_user(client):
    c, _ = client
    ravi = _login(c, "ravi").json()["token"]
    priya = _login(c, "priya").json()["token"]
    c.post("/api/memory", json={"key": "default_warehouse", "value": "Unit-2"},
           headers=_auth(ravi))
    assert c.get("/api/memory", headers=_auth(ravi)).json()["memories"]
    assert c.get("/api/memory", headers=_auth(priya)).json()["memories"] == []


def test_logout_invalidates(client):
    c, _ = client
    tok = _login(c, "ravi").json()["token"]
    assert c.get("/api/brief", headers=_auth(tok)).status_code == 200
    assert c.post("/api/logout", headers=_auth(tok)).json()["ok"] is True
    assert c.get("/api/brief", headers=_auth(tok)).status_code == 401


def test_expired_session_rejected(client):
    c, appmod = client
    tok = _login(c, "ravi").json()["token"]
    # force-expire the session in the DB
    from auth import Session, _hash_token
    s = appmod.SessionMaker()
    row = s.query(Session).filter_by(token_hash=_hash_token(tok)).first()
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    s.commit()
    assert c.get("/api/brief", headers=_auth(tok)).status_code == 401


def test_login_events_audited(client):
    c, _ = client
    _login(c, "ravi")
    _login(c, "ravi", "wrong")
    tok = _login(c, "priya").json()["token"]
    events = [a["event"] for a in
              c.get("/api/audit", headers=_auth(tok)).json()["audit"]]
    assert "login" in events and "login_failed" in events
