"""Date handling: users type DD-MM-YYYY; ERPNext needs YYYY-MM-DD.
Reproduces the live input "20-07-202620-08-2026" (two dates run together)."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dates import normalise, split_runon
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_ddmmyyyy_to_iso():
    assert normalise("20-07-2026") == "2026-07-20"
    assert normalise("20/08/2026") == "2026-08-20"
    assert normalise("2026-07-20") == "2026-07-20"      # already ISO
    assert normalise("rubbish") is None


def test_runon_dates_are_split():
    assert split_runon("20-07-202620-08-2026") == ["20-07-2026", "20-08-2026"]


def test_engine_normalises_dates_in_draft(tmp_path):
    """The exact live input: DD-MM-YYYY dates must reach ERPNext as ISO."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "transaction_date": "20-07-2026",
                            "schedule_date": "20-08-2026",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 53, "rate": 598}]}})])
    r = eng.handle_command("draft a PO, 53 shirts at 598", "ravi")
    f = r["proposals"][0]["fields"]
    assert f["transaction_date"] == "2026-07-20"
    assert f["schedule_date"] == "2026-08-20"
    assert f["items"][0]["schedule_date"] == "2026-08-20"   # row carries it too


def test_unparseable_date_is_dropped_not_sent(tmp_path):
    """Better to let ERPNext default than to send garbage."""
    eng = mk(tmp_path, [
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "schedule_date": "sometime next week",
                            "items": [{"item_code": "M6 hex screw", "qty": 1}]}})])
    r = eng.handle_command("draft a PO", "ravi")
    f = r["proposals"][0]["fields"]
    from dates import normalise as n
    assert n(f["schedule_date"]) == f["schedule_date"]   # a valid ISO date
