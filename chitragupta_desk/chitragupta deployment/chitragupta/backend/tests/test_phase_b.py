"""Act-as-user Phase B: per-user ERP execution via verified session binding.

Properties proven:
- a sid is only bound after the ERP confirms it belongs to the asserted user
- a mismatched sid (stolen/wrong session) is NOT bound -> stays in bot mode
- once bound, erp_for(user) returns a per-user adapter; other users still get
  the bot adapter (thread-isolated at the engine)
- cookie-mode adapter sends Cookie+CSRF headers, no bot token
- adapter factory failure degrades to the bot, never breaks
"""
import os, sys, uuid, time, hmac, hashlib, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from auth import CredentialStore

SECRET = "test-sso-secret"


def sign(user, ts=None, secret=SECRET, **extra):
    ts = ts if ts is not None else str(time.time())
    sig = hmac.new(secret.encode(), f"{user}|{ts}".encode(),
                   hashlib.sha256).hexdigest()
    return {"user": user, "ts": ts, "sig": sig, **extra}


# ---------- CredentialStore behaviour ----------

def test_erp_for_returns_user_adapter_after_binding():
    made = {}
    def factory(user, cred):
        made[user] = cred
        return f"ADAPTER-{user}"
    cs = CredentialStore("BOT", adapter_factory=factory)
    assert cs.erp_for("priya") == "BOT"                  # before binding
    cs.register_session("priya", "sid123", "csrf456")
    assert cs.erp_for("priya") == "ADAPTER-priya"        # after
    assert made["priya"] == {"sid": "sid123", "csrf": "csrf456"}
    assert cs.erp_for("ravi") == "BOT"                   # others unaffected
    cs.clear("priya")
    assert cs.erp_for("priya") == "BOT"


def test_factory_failure_degrades_to_bot():
    def bad_factory(user, cred):
        raise RuntimeError("boom")
    cs = CredentialStore("BOT", adapter_factory=bad_factory)
    cs.register_session("priya", "sid", None)
    assert cs.erp_for("priya") == "BOT"


# ---------- cookie-mode adapter headers ----------

def test_cookie_adapter_uses_session_not_token(monkeypatch):
    import erp_frappe
    captured = {}
    class FakeResp:
        status = 200
        def read(self): return b'{"message": "ok"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["method"] = req.get_method()
        return FakeResp()
    monkeypatch.setattr(erp_frappe.urllib.request, "urlopen", fake_urlopen)
    a = erp_frappe.FrappeERP(base_url="http://erp", session_sid="SID9",
                             csrf_token="CSRF7", acting_user="priya@m.com")
    a._call("POST", "/api/method/x", body={"k": 1})
    h = captured["headers"]
    assert h.get("Cookie") == "sid=SID9"
    assert h.get("X-frappe-csrf-token") == "CSRF7"
    assert "Authorization" not in h                      # no bot token leaks


def test_comment_attributed_to_acting_user(monkeypatch):
    import erp_frappe
    sent = {}
    class FakeResp:
        status = 200
        def read(self): return b'{"message": {}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data)
        return FakeResp()
    monkeypatch.setattr(erp_frappe.urllib.request, "urlopen", fake_urlopen)
    a = erp_frappe.FrappeERP(base_url="http://erp", session_sid="S",
                             acting_user="priya@m.com")
    a.add_comment("Purchase Order", "PO-1", "looks good")
    assert sent["body"]["comment_by"] == "priya@m.com"


# ---------- the /api/sso exchange ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHITRAGUPTA_DB",
                       f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db")
    monkeypatch.setenv("CHITRAGUPTA_SSO_SECRET", SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ERP_URL", raising=False)
    # governance.USERS is module-global: purge users provisioned by other
    # tests, or a stale (empty-role) entry blocks re-provisioning here.
    import governance
    for u in [u for u, i in governance.USERS.items() if i.get("provisioned")]:
        governance.USERS.pop(u, None)
    sys.modules.pop("app", None)
    import app as appmod
    from fastapi.testclient import TestClient
    return TestClient(appmod.app), appmod


def test_sso_without_sid_is_phase_a(client):
    c, appmod = client
    r = c.post("/api/sso", json=sign("ravi@mantratec.com"))
    assert r.status_code == 200
    assert r.json()["acting_mode"] == "bot"


def test_sid_only_bound_when_erp_confirms_owner(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("ERP_URL", "http://erp")
    import erp_frappe
    monkeypatch.setattr(
        erp_frappe.FrappeERP, "_call",
        lambda self, m, p, **k: "priya@mantratec.com")   # ERP says: this sid IS priya
    r = c.post("/api/sso", json=sign("priya@mantratec.com",
                                     sid="GOODSID", csrf="C"))
    assert r.json()["acting_mode"] == "user"
    assert appmod.creds._creds["priya@mantratec.com"]["sid"] == "GOODSID"


def test_mismatched_sid_not_bound(client, monkeypatch):
    """A stolen/mismatched session must NOT be bound to the asserted user."""
    c, appmod = client
    monkeypatch.setenv("ERP_URL", "http://erp")
    import erp_frappe
    monkeypatch.setattr(
        erp_frappe.FrappeERP, "_call",
        lambda self, m, p, **k: "someone-else@mantratec.com")
    r = c.post("/api/sso", json=sign("priya@mantratec.com",
                                     sid="STOLEN", csrf="C"))
    assert r.status_code == 200                     # login ok (Phase A)
    assert r.json()["acting_mode"] == "bot"         # but NOT bound
    assert "priya@mantratec.com" not in appmod.creds._creds


def test_sso_reports_acting_reason(client):
    c, appmod = client
    r = c.post("/api/sso", json=sign("ravi@mantratec.com"))
    d = r.json()
    assert d["acting_mode"] == "bot"
    assert "sid" in d["acting_reason"]        # explains WHY it's bot mode


def test_strict_mode_refuses_unbound_sso_user(monkeypatch):
    """The user's own question: 'what's the purpose of act-as-user if
    fallback sees everything?' Strict mode = unbound SSO user gets NOTHING."""
    monkeypatch.setenv("CHITRAGUPTA_STRICT_SSO", "1")
    from auth import CredentialStore, UnboundSession
    from erp import FakeERP
    store = CredentialStore(FakeERP(), adapter_factory=None)
    store.mark_sso("pramod@m.com")
    try:
        store.erp_for("pramod@m.com")
        assert False, "should have refused"
    except UnboundSession as e:
        assert e.user == "pramod@m.com"
    # non-SSO (demo/pilot) users still get the default adapter
    assert store.erp_for("ravi") is store.default_erp
