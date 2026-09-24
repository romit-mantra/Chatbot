"""Learning loop: what's learned must CHANGE BEHAVIOUR, and be measurable."""
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


def test_edit_is_learned_then_applied_to_the_next_draft(tmp_path):
    """THE loop: human edits a draft -> next draft uses that value itself."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 10, "rate": 12}]}}),
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 5, "rate": 12}]}}),
    ])
    r1 = eng.handle_command("draft a PO", "ravi")
    pid = r1["proposals"][0]["id"]
    # human edits a field before approving
    eng.approve(pid, "ravi", edits={"currency": "USD"})
    assert eng.memory.org_norms("Purchase Order").get("preferred_currency") == "USD"

    # the NEXT draft should carry the learned value without being told
    r2 = eng.handle_command("draft another PO", "ravi")
    assert r2["proposals"][0]["fields"].get("currency") == "USD"
    notes = " ".join(a["text"] for a in r2["activity"])
    assert "applied learned default currency=USD" in notes


def test_learning_stats_are_measurable(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw", "qty": 1,
                                       "rate": 5}]}})])
    r = eng.handle_command("draft a PO", "ravi")
    eng.approve(r["proposals"][0]["id"], "ravi")
    stats = eng.memory.learning_stats()
    assert stats["approvals_total"] == 1
    assert "clean_rate" in stats and "org_norms_learned" in stats


def test_suggestions_are_returned(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order"}),
        "There is 1 purchase order."])
    r = eng.handle_command("how many POs?", "ravi")
    assert r.get("suggestions") and len(r["suggestions"]) <= 3


def test_precedent_recall(tmp_path):
    eng = mk(tmp_path, [])
    eng.memory.remember_org("ravi", "Purchase Order",
                            "rate::M6 hex screw::ACME Fasteners", "12.40")
    p = eng.memory.precedents("Purchase Order", "ACME")
    assert p.get("M6 hex screw") == "12.40"
