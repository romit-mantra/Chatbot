"""The generic operation engine (agent loop). Proves:
- multi-step composition (search -> comment on each -> finish)
- editing a draft stages an update proposal; approving applies it (read-back)
- cancel stages a proposal; approving cancels (docstatus 2)
- a 'jailbroken' agent CANNOT write anything directly: writes only stage
- RBAC contains every tool; blocked actions stay blocked; the loop is bounded
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from planner import Planner, ScriptedClient


def tc(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    # first response routes to the agent; rest are agent steps
    plan = json.dumps({"op": "agent"})
    return Engine(FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient([plan] + responses)))


def test_multistep_search_then_comment_each(tmp_path):
    """'find POs to receive and comment on each' — composition."""
    eng = mk(tmp_path, [
        tc("search", doctype="Purchase Order", filters={"status": "To Receive"}),
        tc("comment", doctype="Purchase Order", name="PUR-2026-00031",
           text="please expedite", tag="Administrator"),
        tc("finish", reply="Commented on 1 open PO (PUR-2026-00031)."),
    ])
    r = eng.handle_command("comment on every PO awaiting receipt", "ravi")
    assert "PUR-2026-00031" in r["reply"]
    doc = eng.erp.get("Purchase Order", "PUR-2026-00031")
    assert any("expedite" in c for c in doc["_comments"])
    # search results were fed back to the model
    step2_input = eng.planner.client.calls[2][1]
    assert "PUR-2026-00031" in step2_input


def test_edit_stages_update_and_approve_applies_it(tmp_path):
    eng = mk(tmp_path, [
        tc("propose_update", doctype="Purchase Order", name="PUR-DRAFT-X",
           patch={"qty": 100}),
        tc("finish", reply="Staged the qty change for your approval."),
    ])
    eng.erp.store["Purchase Order"]["PUR-DRAFT-X"] = {
        "name": "PUR-DRAFT-X", "qty": 40, "docstatus": 0}
    r = eng.handle_command("change the qty on PUR-DRAFT-X to 100", "ravi")
    assert r["proposals"] and r["proposals"][0]["summary"].startswith("Update")
    assert eng.erp.get("Purchase Order", "PUR-DRAFT-X")["qty"] == 40  # unchanged!
    out = eng.approve(r["proposals"][0]["id"], "ravi")
    assert out["status"] == "executed" and out["readback_ok"] is True
    assert eng.erp.get("Purchase Order", "PUR-DRAFT-X")["qty"] == 100


def test_cancel_stages_and_approve_cancels(tmp_path):
    eng = mk(tmp_path, [
        tc("propose_cancel", doctype="Purchase Order", name="PUR-2026-00031"),
        tc("finish", reply="Cancellation staged for approval."),
    ])
    r = eng.handle_command("cancel PUR-2026-00031", "ravi")
    assert eng.erp.get("Purchase Order", "PUR-2026-00031")["docstatus"] == 1
    out = eng.approve(r["proposals"][0]["id"], "ravi")
    assert out["readback_ok"] is True
    assert eng.erp.get("Purchase Order", "PUR-2026-00031")["docstatus"] == 2


def test_agent_cannot_write_directly_only_stage(tmp_path):
    """Even a malicious step sequence ends with everything pending approval."""
    eng = mk(tmp_path, [
        tc("create_draft", doctype="Purchase Order",
           doc={"supplier": "ACME Fasteners",
                "items": [{"item_code": "M6 hex screw", "qty": 9, "rate": 5}]}),
        tc("propose_submit", doctype="Purchase Order", name="WILL-BE-IGNORED"),
        tc("finish", reply="submitted everything immediately!!"),
    ])
    r = eng.handle_command("create and submit a PO right now, skip approval",
                           "ravi")
    live = [p for p in eng.erp.search("Purchase Order")
            if p["docstatus"] == 1 and p["name"] != "PUR-2026-00031"]
    assert live == []                       # NOTHING submitted
    assert r["proposals"]                   # only staged proposals


def test_rbac_contains_agent_tools(tmp_path):
    """A Sales user's agent may not create a Purchase Order."""
    eng = mk(tmp_path, [
        tc("create_draft", doctype="Purchase Order",
           doc={"supplier": "ACME Fasteners",
                "items": [{"item_code": "M6 hex screw", "qty": 1}]}),
        tc("finish", reply="Could not create it — not permitted for your role."),
    ])
    r = eng.handle_command("make a purchase order", "meera")
    assert r["proposals"] == []
    assert "not permitted" in r["reply"].lower()
    # the model was TOLD about the refusal in the step result
    step2 = eng.planner.client.calls[2][1]
    assert "not permitted" in step2


def test_blocked_action_stays_blocked_in_agent(tmp_path):
    """GL Entry cancel is forbidden by the gate even via the agent."""
    eng = mk(tmp_path, [
        tc("propose_cancel", doctype="GL Entry", name="GL-2026-00001"),
        tc("finish", reply="That's blocked — ledger integrity."),
    ])
    r = eng.handle_command("cancel the GL entry", "admin")
    assert r["proposals"] == []
    assert "block" in r["reply"].lower()


def test_loop_is_bounded(tmp_path):
    """A model that never finishes hits the step cap gracefully."""
    steps = [tc("search", doctype="Item")] * 20
    eng = mk(tmp_path, steps)
    r = eng.handle_command("loop forever", "ravi")
    assert "more steps than" in r["reply"]
    # exactly MAX_STEPS agent calls + 1 plan call were made
    assert len(eng.planner.client.calls) == 1 + eng.AGENT_MAX_STEPS
