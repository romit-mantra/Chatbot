"""Regression tests for the three REAL failures seen in the ERP:
  1. "total amount of all POs"      -> answered "12 records" (count, not sum)
  2. "how many are of zukerman?"    -> generic fallback
  3. "summit traders total PO?"     -> "0 records" though POs plainly existed
     (exact-match filter vs real value "Summit Traders Ltd.")
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient, fuzzy_match


class RealisticERP(FakeERP):
    """Mirrors the user's actual ERP data shape (full supplier legal names)."""
    def __init__(self):
        super().__init__()
        self.store["Purchase Order"] = {}
        rows = [
            ("PUR-ORD-2026-00001", "Zuckerman Security Ltd.", 500.0),
            ("PUR-ORD-2026-00002", "Zuckerman Security Ltd.", 71250.0),
            ("PUR-ORD-2026-00003", "Summit Traders Ltd.", 105000.0),
            ("PUR-ORD-2026-00004", "Summit Traders Ltd.", 104600.0),
            ("PUR-ORD-2026-00005", "MA Inc.", 40404.0),
        ]
        for name, sup, total in rows:
            self.store["Purchase Order"][name] = {
                "name": name, "supplier": sup, "supplier_name": sup,
                "grand_total": total, "currency": "INR", "docstatus": 1,
                "status": "To Receive and Bill"}


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(RealisticERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


# ---------- the fuzzy filter itself ----------

def test_fuzzy_filter_finds_partial_supplier_name():
    rows = [{"supplier": "Summit Traders Ltd.", "grand_total": 105000},
            {"supplier": "MA Inc.", "grand_total": 40404}]
    hit = fuzzy_match(rows, {"supplier": "Summit Traders"})
    assert len(hit) == 1 and hit[0]["grand_total"] == 105000

def test_fuzzy_filter_is_case_insensitive():
    rows = [{"supplier": "Zuckerman Security Ltd."}]
    assert len(fuzzy_match(rows, {"supplier": "zukerman"})) == 0   # typo -> no match
    assert len(fuzzy_match(rows, {"supplier": "zuckerman"})) == 1  # case-insensitive

def test_fuzzy_filter_matches_alt_key():
    rows = [{"supplier_name": "Summit Traders Ltd.", "grand_total": 1}]
    assert len(fuzzy_match(rows, {"supplier": "Summit"})) == 1


# ---------- the three real failures ----------

def test_total_amount_of_all_pos_sums_not_counts(tmp_path):
    """FAILURE 1: must SUM, not COUNT."""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "question": "total amount of all POs"}),
        "The total value of all 5 purchase orders is **INR 3,21,754**.",
    ])
    r = eng.handle_command("total amount of all Pos", "ravi")
    assert "3,21,754" in r["reply"]
    # the model must have RECEIVED the real rows with grand_total values
    analyze_call = eng.planner.client.calls[-1][1]
    assert "105000" in analyze_call and "grand_total" in analyze_call

def test_supplier_filter_finds_rows(tmp_path):
    """FAILURE 3: 'Summit Traders' must match 'Summit Traders Ltd.'"""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "filters": {"supplier": "Summit Traders"},
                    "question": "summit traders total PO amount?"}),
        "Summit Traders Ltd. has 2 POs totalling **INR 2,09,600**.",
    ])
    r = eng.handle_command("summit traders total PO amount?", "ravi")
    assert "2,09,600" in r["reply"]
    analyze_call = eng.planner.client.calls[-1][1]
    assert "Records found: 2" in analyze_call        # NOT 0
    assert "MA Inc." not in analyze_call             # correctly filtered out

def test_count_for_a_supplier(tmp_path):
    """FAILURE 2: 'how many are of zuckerman' must count that supplier's rows."""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "filters": {"supplier": "Zuckerman"},
                    "question": "how many are of zuckerman?"}),
        "Zuckerman Security Ltd. has **2** purchase orders.",
    ])
    r = eng.handle_command("how many are of zuckerman?", "ravi")
    assert "2" in r["reply"]
    assert "Records found: 2" in eng.planner.client.calls[-1][1]


# ---------- the model gets REAL data, and governance still holds ----------

def test_analyze_receives_full_records(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order"}),
        "answer"])
    eng.handle_command("which supplier is biggest?", "ravi")
    sent = eng.planner.client.calls[-1][1]
    for supplier in ("Zuckerman", "Summit", "MA Inc."):
        assert supplier in sent          # all real rows handed to the model

def test_writes_are_still_gated_by_role(tmp_path):
    """Reads go to ERPNext, but WRITES are still refused in-engine for a role
    that lacks them — the model asking changes nothing."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "Summit",
                            "items": [{"item_code": "SKU1", "qty": 1}]}})])
    r = eng.handle_command("make a purchase order", "meera")   # Sales user
    assert "doesn't allow" in r["reply"]
    assert r["proposals"] == []

def test_zero_results_is_honest(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Purchase Order",
                    "filters": {"supplier": "Nonexistent Co"}}),
        "I found no purchase orders for a supplier matching 'Nonexistent Co'."])
    r = eng.handle_command("total for Nonexistent Co", "ravi")
    assert "no purchase orders" in r["reply"].lower()
    assert "Records found: 0" in eng.planner.client.calls[-1][1]
