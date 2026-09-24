"""Regression: the connection pool must not be exhausted by repeated requests.

Before scoped_session, every `Session()` call opened a new connection that was
never closed -> QueuePool limit reached after ~15 requests and the app hung.
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def test_many_requests_do_not_exhaust_the_pool(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    S = make_session(db)
    # 60 commands: 4x the old pool ceiling (5 + 10 overflow)
    # each command = one plan call + one analyze call
    responses = []
    for _ in range(60):
        responses.append(json.dumps({"op": "query", "doctype": "Item"}))
        responses.append("There are 2 items.")
    eng = Engine(FakeERP(), S, planner=Planner(ScriptedClient(responses)))
    for i in range(60):
        r = eng.handle_command("how many items?", "ravi")
        assert "items" in r["reply"].lower(), f"failed on request {i}"


def test_brief_repeatedly(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(FakeERP(), make_session(db))
    for _ in range(40):
        b = eng.brief("ravi")
        assert "greeting" in b
