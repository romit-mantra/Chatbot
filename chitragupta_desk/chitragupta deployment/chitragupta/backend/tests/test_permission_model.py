"""Regression tests for the real bug seen in the ERP:

  User (Purchase User): "which companies are there?"
  Bot: "Your role doesn't allow read on Company."      <-- WRONG

Our hardcoded map refused a read that ERPNext itself permits. ERPNext is the
authority on reads. Writes remain gated in-engine. Memory keeps its own strict
scope so this delegation doesn't leak org facts across roles.
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient
from governance import user_can, can_see_memory


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_purchase_user_can_read_company(tmp_path):
    """THE BUG: this used to be refused."""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Company"}),
        "You have one company: ACME Corp.",
    ])
    r = eng.handle_command("which companies are there?", "ravi")
    assert "doesn't allow" not in r["reply"]


def test_reads_pass_the_engine_layer():
    for dt in ("Company", "Item", "Supplier", "Purchase Order", "GL Entry"):
        assert user_can("ravi", dt, "get"), dt


def test_writes_are_still_gated():
    assert user_can("ravi", "Purchase Order", "submit")
    assert not user_can("ravi", "Payment Entry", "submit")   # not his role
    assert not user_can("meera", "Purchase Order", "submit")  # sales user


def test_memory_scope_is_independent_of_reads():
    """Delegating reads must NOT open a memory leak between roles."""
    assert can_see_memory("priya", "Payment Entry")      # accounts mgr
    assert not can_see_memory("meera", "Payment Entry")  # sales user
    assert can_see_memory("ravi", "Purchase Order")


def test_company_partial_name_is_resolved(tmp_path):
    """'mantra' must resolve to the real company, not be passed through raw."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME", "company": "acme corp",
                            "items": [{"item_code": "M6 hex screw", "qty": 40,
                                       "rate": 538}]}})])
    eng.erp.store["Company"] = {"ACME Corp": {"name": "ACME Corp"}}
    r = eng.handle_command("draft a PO", "ravi")
    assert r["proposals"]
    assert r["proposals"][0]["fields"]["company"] == "ACME Corp"


def test_unknown_company_gives_a_helpful_message(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME", "company": "Nonexistent Ltd",
                            "items": [{"item_code": "M6 hex screw", "qty": 1}]}})])
    eng.erp.store["Company"] = {"ACME Corp": {"name": "ACME Corp"}}
    r = eng.handle_command("draft a PO", "ravi")
    assert r["proposals"] == []
    assert "ACME Corp" in r["reply"]        # tells the user what IS available


def test_erp_errors_are_humanised():
    from engine import Engine as E
    raw = ('{"exception":"frappe.exceptions.LinkValidationError: Could not find '
           'Company: mantra","exc_type":"LinkValidationError"}')
    msg = E._friendly_erp_error(raw)
    assert "mantra" in msg and "Traceback" not in msg and "exc_type" not in msg
