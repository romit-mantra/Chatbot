"""Regression: the assistant must REMEMBER the conversation.

Real failure observed in the ERP:
  User: draft a PO for sku001 of 23 pieces
  Bot : need supplier, rate, required-by date
  User: 1 MA inc, 2 546 inr, 3 15-08-2026
  Bot : got those... now what item and quantity?      <-- already given!
  User: tshirt and 48 quantity
  Bot : got those... now what supplier, rate, date?   <-- LOOP FOREVER
Each turn was planned in isolation with no history.
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


def test_history_is_passed_to_the_planner(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "need_info", "doctype": "Purchase Order",
                    "missing": ["supplier"],
                    "question": "Which supplier?"}),
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 23, "rate": 12}]}}),
    ])
    eng.handle_command("draft a PO for M6 of 23 pieces", "ravi")
    eng.handle_command("ACME", "ravi")

    # the SECOND plan call must have seen the whole thread
    second_call = eng.planner.client.calls[1][1]
    assert "Conversation so far" in second_call
    assert "23 pieces" in second_call          # the original request
    assert "Which supplier?" in second_call    # what the bot asked
    assert "Latest message: ACME" in second_call


def test_details_accumulate_and_the_draft_completes(tmp_path):
    """The multi-turn flow reaches a real draft instead of looping."""
    eng = mk(tmp_path, [
        json.dumps({"op": "need_info", "doctype": "Purchase Order",
                    "missing": ["supplier", "rate"],
                    "question": "Which supplier and what rate?"}),
        json.dumps({"op": "create", "doctype": "Purchase Order",
                    "doc": {"supplier": "ACME Fasteners",
                            "items": [{"item_code": "M6 hex screw",
                                       "qty": 23, "rate": 546}]}}),
    ])
    r1 = eng.handle_command("draft a PO for M6 hex screw of 23 pieces", "ravi")
    assert r1["proposals"] == []                      # asked, didn't guess
    r2 = eng.handle_command("1 ACME, 2 546 inr", "ravi")
    assert r2["proposals"], "should have drafted after the details were given"
    f = r2["proposals"][0]["fields"]
    assert f["supplier"] == "ACME Fasteners"
    assert f["items"][0]["qty"] == 23                 # carried from turn 1
    assert f["items"][0]["rate"] == 546               # given in turn 2


def test_history_is_per_user(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "explain", "question": "hi"}), "Hello Ravi.",
        json.dumps({"op": "explain", "question": "hi"}), "Hello Meera.",
    ])
    eng.handle_command("hi", "ravi")
    eng.handle_command("hi", "meera")
    # meera's plan call must NOT contain ravi's thread
    meera_call = eng.planner.client.calls[2][1]
    assert "Hello Ravi" not in meera_call


def test_history_is_capped(tmp_path):
    eng = mk(tmp_path, [])
    for i in range(30):
        eng._remember_turn("ravi", "user", f"msg {i}")
    assert len(eng._hist("ravi")) == eng.HISTORY_TURNS


def test_assistant_replies_are_recorded(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "explain", "question": "how to raise PO"}),
        "Go to Buying > Purchase Order.",
    ])
    eng.handle_command("how to raise PO?", "ravi")
    h = eng._hist("ravi")
    assert h[0]["role"] == "user"
    assert h[1]["role"] == "assistant"
    assert "Buying" in h[1]["content"]
