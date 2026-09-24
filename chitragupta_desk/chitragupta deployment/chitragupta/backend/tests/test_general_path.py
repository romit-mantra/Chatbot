"""General-path tests (the any-doctype planner). Offline via ScriptedClient.
Proves free-form questions work on ANY doctype, grounded in real records, and
that RBAC + the governance gate still contain the model."""

import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def mk(tmp_path, responses):
    p = Planner(ScriptedClient(responses))
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db), planner=p)


def test_context_question_is_grounded(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "answer_context", "question": "who created this?"}),
        "It was created by chitragupta-bot on 13-07-2026.",
    ])
    r = eng.handle_command("who raised this po?", "ravi",
                           context={"doctype": "Purchase Order",
                                    "name": "PUR-2026-00031"})
    assert "chitragupta-bot" in r["reply"]
    assert r["proposals"] == []


def test_count_any_doctype(tmp_path):
    """Counts now go through the unified `query` op: the engine fetches the
    rows and the model answers from them."""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order"}),
        "There is **1** purchase order."])
    r = eng.handle_command("how many pos are there?", "ravi")
    assert "1" in r["reply"]
    assert "Records found: 1" in eng.planner.client.calls[-1][1]


def test_list_any_doctype(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Supplier"}),
        "You have 2 suppliers: ACME Fasteners and Bharat Bolts."])
    r = eng.handle_command("show me all suppliers", "ravi")
    assert "ACME Fasteners" in r["reply"]
    assert "ACME Fasteners" in eng.planner.client.calls[-1][1]  # real rows sent


def test_create_goes_through_the_gate(tmp_path):
    """A well-formed create (with an items table) drafts and hits the gate."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 100, "rate": 12.4}]}})])
    r = eng.handle_command("draft a PO to ACME for 100 screws", "ravi")
    assert r["proposals"] and r["proposals"][0]["tier"] == "approve"
    live = [p for p in eng.erp.search("Purchase Order") if p["docstatus"] == 1]
    assert len(live) == 1  # only the pre-seeded one — nothing submitted


def test_create_without_items_asks_instead_of_failing(tmp_path):
    """The old bug: a doc with no items table was sent to ERPNext and 404'd.
    Now we ask the user."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners"}})])
    r = eng.handle_command("draft a PO for ACME", "ravi")
    assert r["proposals"] == []
    assert "item" in r["reply"].lower()


def test_create_without_supplier_asks_and_offers_options(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"items": [{"item_code": "M6 hex screw", "qty": 23}]}})])
    r = eng.handle_command("can u draft a PO for M6 hex screw of 23 pieces", "ravi")
    assert r["proposals"] == []
    assert "supplier" in r["reply"].lower()
    assert "ACME" in r["reply"]        # offers the real options


def test_partial_supplier_name_is_resolved(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME",
                            "items": [{"item_code": "M6", "qty": 5,
                                       "rate": 10}]}})])
    r = eng.handle_command("PO to ACME for 5 M6", "ravi")
    assert r["proposals"]
    fields = r["proposals"][0]["fields"]
    assert fields["supplier"] == "ACME Fasteners"        # resolved in full
    assert fields["items"][0]["item_code"] == "M6 hex screw"


def test_refuse_blocks_ledger_delete(tmp_path):
    eng = mk(tmp_path, [json.dumps({"op": "refuse"})])
    r = eng.handle_command("delete the january GL entry", "admin")
    assert r["proposals"] == []
    assert "block" in r["reply"].lower() or "can't" in r["reply"].lower()


def test_rbac_still_contains_the_model(tmp_path):
    # model says "create a PO" for a SALES user -> RBAC must refuse
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners", "qty": 1}})])
    r = eng.handle_command("make a purchase order", "meera")
    assert r["proposals"] == []
    assert "doesn't allow" in r["reply"]


def test_read_is_delegated_to_erpnext(tmp_path):
    """Our map no longer refuses reads — ERPNext is the authority and will 403
    if the user truly may not read it. This prevents the assistant refusing
    legitimate questions (e.g. a Purchase User asking about Company)."""
    eng = mk(tmp_path, [json.dumps({"op": "answer_context"}),
                        "It is a GL entry for 50000."])
    r = eng.handle_command("what is this?", "meera",
                           context={"doctype": "GL Entry", "name": "GL-2026-00001"})
    assert "doesn't allow" not in r["reply"]


def test_remember_needs_no_model_call(tmp_path):
    eng = mk(tmp_path, [])   # empty client: a model call would raise
    r = eng.handle_command("remember default_warehouse Unit-2", "ravi")
    assert "Unit-2" in r["reply"]
    assert eng.memory.recall("ravi")["personal"]["default_warehouse"] == "Unit-2"


def test_bad_model_output_degrades(tmp_path):
    eng = mk(tmp_path, ["complete garbage, not json"])
    r = eng.handle_command("hey", "ravi")
    assert r["proposals"] == []
    assert "didn't follow" in r["reply"] or "try" in r["reply"].lower()


def test_disallowed_op_rejected(tmp_path):
    eng = mk(tmp_path, [json.dumps({"op": "drop_database"})])
    r = eng.handle_command("drop everything", "admin")
    assert r["proposals"] == []      # unknown op -> safe fallback, nothing executed


def test_brief_survives_real_schema(tmp_path):
    """Real ERPNext Items have no stock_qty — brief() must not KeyError."""
    eng = mk(tmp_path, [])
    for item in eng.erp.store["Item"].values():
        item.pop("stock_qty", None)          # simulate the real ERPNext shape
        item.pop("reorder_level", None)
    b = eng.brief("ravi")                     # must not raise
    assert "greeting" in b and isinstance(b["items"], list)
