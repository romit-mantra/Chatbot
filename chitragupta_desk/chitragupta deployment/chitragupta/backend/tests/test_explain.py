"""How-to / knowledge questions must be answered, not fall to the fallback.
Reproduces the real failures: "how to raise PO?", "workflow of PO and my role?"
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_how_to_raise_po(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "explain", "question": "how to raise PO?"}),
        "Go to **Buying > Purchase Order > Add Purchase Order**. Or just tell "
        "me: 'raise a PO for 100 SKU001 from Summit Traders'.",
    ])
    r = eng.handle_command("how to raise PO?", "ravi")
    assert "Buying" in r["reply"] and "Purchase Order" in r["reply"]
    assert r["proposals"] == []


def test_role_question_gets_the_users_real_roles(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "explain", "question": "what is my role?"}),
        "You hold the **Purchase User** role, so you can create and submit POs.",
    ])
    r = eng.handle_command("workflow of PO? and what is my role?", "ravi")
    assert "Purchase User" in r["reply"]
    # the planner must have been TOLD the user's real roles
    sent = eng.planner.client.calls[-1][1]
    assert "Purchase User" in sent


def test_explain_reads_no_data(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "explain", "question": "where do I raise a PO?"}),
        "Buying > Purchase Order.",
    ])
    r = eng.handle_command("where to raise PO?", "ravi")
    tags = [a["tag"] for a in r["activity"]]
    assert "go" in tags
    assert "Buying" in r["reply"]
    # no ERP rows were fetched for a knowledge question
    assert not any("row(s)" in a["text"] for a in r["activity"])
