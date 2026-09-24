"""Real-world conversation failures found in live testing:
  1. "hey there hows its gng" -> robotic menu, three times running
  2. "purchase orders this month or till now" -> "none in August" and nothing
     else, despite 50 POs existing
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def test_greeting_gets_a_human_reply(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(FakeERP(), make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "chitchat",
                    "doc": {"reply": "Doing well, thanks! Want me to pull up "
                                     "today's pending approvals?"}})])))
    r = eng.handle_command("hey there hows its gng", "ravi")
    assert "thanks" in r["reply"].lower()
    assert "count or list records" not in r["reply"]      # NOT the old menu
    assert r["suggestions"]


def test_empty_date_filter_still_reports_the_total(tmp_path):
    """THE BUG: 'none in August 2026' while 50 POs exist overall."""
    erp = FakeERP(); erp.store["Purchase Order"] = {}
    for i in range(50):
        erp.store["Purchase Order"][f"PO-{i}"] = {
            "name": f"PO-{i}", "supplier": "ACME Fasteners",
            "transaction_date": "2026-05-10", "grand_total": 1000.0,
            "docstatus": 1}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "date_range": {"field": "transaction_date",
                                   "from": "2026-08-01", "to": "2026-08-31"}}),
        "None in August 2026 — but 50 POs in total worth INR 50,000."])))
    r = eng.handle_command("purchase orders this month or till now", "ravi")
    sent = eng.planner.client.calls[-1][1]
    assert '"unfiltered"' in sent          # model TOLD the overall picture
    assert '"row_count": 50' in sent
    assert "50" in r["reply"]


def test_normal_filtered_query_has_no_unfiltered_noise(tmp_path):
    erp = FakeERP(); erp.store["Purchase Order"] = {
        "PO-1": {"name": "PO-1", "supplier": "ACME Fasteners",
                 "transaction_date": "2026-08-10", "grand_total": 500.0,
                 "docstatus": 1}}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "date_range": {"field": "transaction_date",
                                   "from": "2026-08-01", "to": "2026-08-31"}}),
        "One PO in August, INR 500."])))
    eng.handle_command("POs this month", "ravi")
    assert '"unfiltered"' not in eng.planner.client.calls[-1][1]


def test_role_question_is_answered_not_done(tmp_path):
    """Live bug: 'what can my role do?' returned bare 'Done.' via lazy agent."""
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    # even if the planner mis-routes to agent AND the model finishes lazily:
    eng = Engine(FakeERP(), make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "agent"}),
        json.dumps({"tool": "finish", "args": {"reply": "Done."}})])))
    r = eng.handle_command("what can my role do?", "ravi")
    assert r["reply"] != "Done." and len(r["reply"]) > 20


def test_personas_get_substantive_role_answers(tmp_path):
    """Every persona asking about their own capabilities gets a real answer."""
    for user in ("ravi", "meera", "admin"):
        db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
        eng = Engine(FakeERP(), make_session(db),
                     planner=Planner(ScriptedClient([
            json.dumps({"op": "explain"}),
            "As your roles allow, you can raise POs and view suppliers."])))
        r = eng.handle_command("what can i do?", user)
        assert len(r["reply"]) > 30 and "Done" not in r["reply"]
