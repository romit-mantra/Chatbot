"""Date-range filtering — the live bug where 'sales this year' returned nothing
while the unfiltered list showed 2026 orders.

The model sets date_range {field, from, to}; the engine filters real rows by it.
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient, apply_date_range


class SalesERP(FakeERP):
    def __init__(self):
        super().__init__()
        self.store["Sales Order"] = {}
        rows = [
            ("SAL-2026-01", "West View",   "2026-06-06", 32000),
            ("SAL-2026-02", "West View",   "2026-08-19", 229000),
            ("SAL-2026-03", "Palmer",      "2026-07-11", 15000),
            ("SAL-2027-01", "Grant",       "2027-02-17", 20000),
            ("SAL-2025-01", "Grant",       "2025-12-30", 5000),
        ]
        for n, c, d, t in rows:
            self.store["Sales Order"][n] = {
                "name": n, "customer": c, "transaction_date": d,
                "grand_total": t, "docstatus": 1, "currency": "INR"}


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(SalesERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_apply_date_range_unit():
    rows = [{"transaction_date": "2026-06-06"}, {"transaction_date": "2027-02-17"},
            {"transaction_date": "19-08-2026"}]     # mixed formats
    got = apply_date_range(rows, {"field": "transaction_date",
                                  "from": "2026-01-01", "to": "2026-12-31"})
    assert len(got) == 2


def test_sales_in_2026_filters_correctly(tmp_path):
    """THE BUG: this used to return 'no orders' despite 2026 data existing."""
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Sales Order",
                    "date_range": {"field": "transaction_date",
                                   "from": "2026-01-01", "to": "2026-12-31"}}),
        "2026 sales: 3 orders totalling INR 2,76,000."])
    r = eng.handle_command("how much sales in 2026?", "ravi")
    # the model received exactly the 3 rows in 2026 (not 5, not 0)
    analyze_input = eng.planner.client.calls[-1][1]
    assert "Records found: 3" in analyze_input
    assert "SAL-2027-01" not in analyze_input
    assert "SAL-2025-01" not in analyze_input


def test_sales_in_2027(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Sales Order",
                    "date_range": {"field": "transaction_date",
                                   "from": "2027-01-01", "to": "2027-12-31"}}),
        "2027: 1 order, INR 20,000."])
    r = eng.handle_command("sales in 2027", "ravi")
    assert "Records found: 1" in eng.planner.client.calls[-1][1]


def test_unfiltered_still_returns_all(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "query", "doctype": "Sales Order"}),
        "5 orders total."])
    r = eng.handle_command("all sales orders", "ravi")
    assert "Records found: 5" in eng.planner.client.calls[-1][1]


def test_report_respects_date_range(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "report", "doctype": "Sales Order",
                    "date_range": {"field": "transaction_date",
                                   "from": "2026-01-01", "to": "2026-12-31"},
                    "doc": {"format": "csv", "title": "2026 Sales"}})])
    r = eng.handle_command("export 2026 sales to csv", "ravi")
    import base64
    csv = base64.b64decode(r["download"]["b64"]).decode()
    assert "SAL-2026-01" in csv and "SAL-2027-01" not in csv
