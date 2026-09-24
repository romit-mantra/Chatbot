"""Regression for the LIVE ERP failure diagnosed via diagnose_po.py:

  A2 (minimal body, no dates) -> HTTP 417 "Please enter Reqd by Date"
  A1 (full body WITH item schedule_date) -> HTTP 200 created

So every item row MUST carry the schedule/delivery date. _complete_doc must
guarantee that even when the model omits it.
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


def test_every_item_row_gets_a_date(tmp_path):
    """Model omits dates entirely -> engine must fill schedule_date on each row."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw", "qty": 67,
                                       "rate": 675}]}})])   # NO dates at all
    r = eng.handle_command("draft a PO, 67 M6 at 675", "ravi")
    assert r["proposals"], "should have drafted"
    fields = r["proposals"][0]["fields"]
    # top-level date present
    assert fields.get("schedule_date")
    # EVERY item row has the date (this is what the live ERP requires)
    for row in fields["items"]:
        assert row.get("schedule_date"), f"item row missing schedule_date: {row}"


def test_multi_item_all_rows_dated(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw", "qty": 5, "rate": 10},
                                      {"item_code": "M8 bolt", "qty": 3, "rate": 8}]}})])
    r = eng.handle_command("PO for two items", "ravi")
    rows = r["proposals"][0]["fields"]["items"]
    assert len(rows) == 2
    assert all(row.get("schedule_date") for row in rows)
