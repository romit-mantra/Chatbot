"""ERPNext Workflow chains v1: when a doctype has an active Workflow, approval
advances the company's real chain instead of a bare submit."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from planner import Planner, ScriptedClient

WF = {"name": "PO Approval", "document_type": "Purchase Order",
      "submit_state": "Approved",
      "transitions": [
          {"state": "Pending", "action": "Approve", "next_state": "Approved"},
          {"state": "Draft", "action": "Submit for Review",
           "next_state": "Pending"}]}


def mk(tmp_path, responses):
    erp = FakeERP()
    erp.workflows["Purchase Order"] = WF
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(erp, make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_approve_advances_workflow_not_bare_submit(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 2, "rate": 5}]}})])
    r = eng.handle_command("draft a PO", "ravi")
    name = r["proposals"][0]["fields"]["name"]
    # put the draft into the workflow's Pending state (as the chain would)
    eng.erp.store["Purchase Order"][name]["workflow_state"] = "Pending"
    out = eng.approve(r["proposals"][0]["id"], "ravi")
    doc = eng.erp.get("Purchase Order", name)
    assert doc["workflow_state"] == "Approved"      # chain advanced
    assert doc["docstatus"] == 1                    # submit_state reached
    assert out["readback_ok"] is True
    s = eng.Session()
    assert any(a.event == "workflow_advanced" for a in s.query(AuditLog).all())


def test_no_transition_from_state_fails_honestly(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 1, "rate": 5}]}})])
    r = eng.handle_command("draft a PO", "ravi")
    name = r["proposals"][0]["fields"]["name"]
    eng.erp.store["Purchase Order"][name]["workflow_state"] = "Rejected"
    try:
        eng.approve(r["proposals"][0]["id"], "ravi")
        raised = False
    except Exception as ex:
        raised = "transition" in str(ex)
    assert raised


def test_doctype_without_workflow_still_bare_submits(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Sales Order",
                    "doc": {"customer": "Beta Retail",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 1, "rate": 5}]}})])
    # sales user creates SO
    r = eng.handle_command("draft an SO", "meera")
    out = eng.approve(r["proposals"][0]["id"], "meera")
    assert out["status"] == "executed"
