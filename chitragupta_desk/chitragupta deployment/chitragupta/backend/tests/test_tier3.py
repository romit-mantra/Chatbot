"""Tier 3 backend pieces: chart data + feedback endpoint."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from planner import Planner, ScriptedClient


def test_chart_data_from_groupby(tmp_path):
    erp = FakeERP(); erp.store["Sales Order"] = {}
    for i in range(9):
        erp.store["Sales Order"][f"SO-{i}"] = {
            "name": f"SO-{i}", "customer": f"C{i%3}",
            "grand_total": (i+1)*100.0, "docstatus": 1}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Sales Order"}), "answer"])))
    r = eng.handle_command("sales by customer", "ravi")
    ch = r.get("chart")
    assert ch and len(ch["labels"]) == 3 and sum(ch["values"]) == 4500.0


def test_chart_omitted_for_single_group(tmp_path):
    erp = FakeERP(); erp.store["Sales Order"] = {
        "SO-1": {"name": "SO-1", "customer": "OnlyOne", "grand_total": 10.0,
                 "docstatus": 1}}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Sales Order"}), "answer"])))
    r = eng.handle_command("sales", "ravi")
    assert r.get("chart") is None      # nothing worth charting


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHITRAGUPTA_DB",
                       f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ERP_URL", raising=False)
    import governance
    for u in [u for u, i in governance.USERS.items() if i.get("provisioned")]:
        governance.USERS.pop(u, None)
    sys.modules.pop("app", None)
    import app as appmod
    from fastapi.testclient import TestClient
    return TestClient(appmod.app), appmod


def test_feedback_is_audited(client):
    c, appmod = client
    tok = c.post("/api/login", json={"user": "ravi", "password": "demo"}
                 ).json()["token"]
    r = c.post("/api/feedback", headers={"Authorization": f"Bearer {tok}"},
               json={"reply_excerpt": "wrong total", "rating": "down",
                     "reason": "sum was off by 100"})
    assert r.status_code == 200
    s = appmod.SessionMaker()
    fb = [a for a in s.query(AuditLog).all() if a.event == "feedback"]
    assert fb and "sum was off" in fb[0].detail_json


def test_erp_users_endpoint(client):
    c, appmod = client
    appmod.default_erp.store["User"] = {
        "a@x.com": {"name": "a@x.com"}, "b@x.com": {"name": "b@x.com"}}
    tok = c.post("/api/login", json={"user": "ravi", "password": "demo"}
                 ).json()["token"]
    r = c.get("/api/erp-users?q=a@", headers={"Authorization": f"Bearer {tok}"})
    assert r.json()["users"] == ["a@x.com"]
