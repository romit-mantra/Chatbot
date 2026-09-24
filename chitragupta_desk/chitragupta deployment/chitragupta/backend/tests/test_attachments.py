"""Attachment reading: 'not a single thing should be missed'.

- the assistant SEES a document's attachments in context answers
- it READS attachment content (PDF/Excel/CSV) when the question is about them
- the agent can list + read attachments as tools
- HTML is stripped from ERP error messages (the <strong> leak)
"""
import os, sys, uuid, json, io
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


def mk(tmp_path, responses, erp=None):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(erp or FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def erp_with_attachment():
    erp = FakeERP()
    po = erp.store["Purchase Order"]["PUR-2026-00031"]
    po["_attachments"] = [{"name": "F1", "file_name": "quotation.csv",
                           "file_url": "/private/files/quotation.csv",
                           "file_size": 64, "is_private": 1}]
    erp.files["/private/files/quotation.csv"] = \
        b"item,qty,rate\nM6 hex screw,500,11.80\n"
    return erp


def test_context_answer_sees_attachments(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "answer_context",
                    "question": "what files are attached to this?"}),
        "One attachment: quotation.csv.",
    ], erp_with_attachment())
    r = eng.handle_command("what files are attached to this?", "ravi",
                           context={"doctype": "Purchase Order",
                                    "name": "PUR-2026-00031"})
    sent = eng.planner.client.calls[-1][1]
    assert "quotation.csv" in sent                 # model saw the list
    assert "11.80" in sent                          # and the CONTENT (q mentions files)


def test_attachment_content_not_fetched_for_unrelated_question(tmp_path):
    """Don't waste tokens reading files when the question isn't about them."""
    eng = mk(tmp_path, [
        json.dumps({"op": "answer_context", "question": "who created this?"}),
        "Created by chitragupta-bot.",
    ], erp_with_attachment())
    eng.handle_command("who created this?", "ravi",
                       context={"doctype": "Purchase Order",
                                "name": "PUR-2026-00031"})
    sent = eng.planner.client.calls[-1][1]
    assert "quotation.csv" in sent                 # the LIST is always visible
    assert "11.80" not in sent                     # content NOT fetched


def test_agent_reads_attachment(tmp_path):
    plan = json.dumps({"op": "agent"})
    steps = [
        json.dumps({"tool": "list_attachments",
                    "args": {"doctype": "Purchase Order",
                             "name": "PUR-2026-00031"}}),
        json.dumps({"tool": "read_attachment",
                    "args": {"file_url": "/private/files/quotation.csv",
                             "file_name": "quotation.csv"}}),
        json.dumps({"tool": "finish",
                    "args": {"reply": "The quotation offers M6 at 11.80."}}),
    ]
    db = f"sqlite:///{tmp_path}/x.db"
    eng = Engine(erp_with_attachment(), make_session(db),
                 planner=Planner(ScriptedClient([plan] + steps)))
    r = eng.handle_command("read the quotation attached to PUR-2026-00031 "
                           "and tell me the offered rate", "ravi")
    assert "11.80" in r["reply"]
    step3_input = eng.planner.client.calls[3][1]
    assert "11.80" in step3_input                  # content reached the model


def test_html_stripped_from_erp_errors():
    from erp_frappe import _frappe_message
    raw = json.dumps({"_server_messages": json.dumps([json.dumps(
        {"message": "User <strong>bot@x.com</strong> does not have access"})])})
    msg = _frappe_message(raw)
    assert "<strong>" not in msg and "bot@x.com" in msg
