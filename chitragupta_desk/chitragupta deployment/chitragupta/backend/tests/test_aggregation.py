"""Exact aggregation: sums correct over MANY rows (beyond the old 200 cap)."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from analytics import compute
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def test_compute_exact_over_500_rows():
    rows = [{"name": f"SO-{i}", "supplier": f"S{i%3}", "grand_total": 100.0,
             "status": "Completed"} for i in range(500)]
    st = compute(rows)
    assert st["row_count"] == 500
    assert st["grand_total"]["sum"] == 50000.0
    assert st["by_supplier"]["S0"]["count"] == 167  # 500/3 rounded up


def test_model_receives_exact_stats_not_arithmetic_duty(tmp_path):
    erp = FakeERP(); erp.store["Sales Order"] = {
        f"SO-{i}": {"name": f"SO-{i}", "customer": "C", "grand_total": 10.0,
                    "docstatus": 1} for i in range(300)}   # > old cap
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Sales Order"}),
        "Total sales: **INR 3,000** across 300 orders."])))
    r = eng.handle_command("total sales?", "ravi")
    sent = eng.planner.client.calls[-1][1]
    assert "EXACT AGGREGATES" in sent and '"sum": 3000.0' in sent
    assert "SO-299" not in sent            # full rows NOT dumped; sample only
    assert "aggregated 300 row(s) exactly" in " ".join(
        a["text"] for a in r["activity"])
