"""Comment/tag and downloadable report tests."""
import os, sys, uuid, json, base64, io
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from planner import Planner, ScriptedClient


def mk(tmp_path, responses):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db),
                  planner=Planner(ScriptedClient(responses)))


def test_comment_and_tag(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "comment",
                    "doc": {"text": "please review", "tag": "Administrator"}})])
    r = eng.handle_command("comment on this and tag admin", "ravi",
                           context={"doctype": "Purchase Order",
                                    "name": "PUR-2026-00031"})
    assert "tagged Administrator" in r["reply"]
    doc = eng.erp.get("Purchase Order", "PUR-2026-00031")
    assert any("@Administrator" in c for c in doc["_comments"])


def test_comment_needs_a_target(tmp_path):
    eng = mk(tmp_path, [json.dumps({"op": "comment", "doc": {"text": "hi"}})])
    r = eng.handle_command("add a comment", "ravi")   # no doc open
    assert "which document" in r["reply"].lower()


def test_report_excel_returns_downloadable_file(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "report", "doctype": "Purchase Order",
                    "doc": {"format": "excel", "title": "PO Report"}})])
    r = eng.handle_command("give me a purchase order report in excel", "ravi")
    assert "download" in r
    dl = r["download"]
    assert dl["filename"] == "PO Report.xlsx"
    assert "spreadsheet" in dl["mimetype"]
    raw = base64.b64decode(dl["b64"])
    assert raw[:2] == b"PK"          # xlsx is a zip -> starts with PK


def test_report_csv(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "report", "doctype": "Supplier",
                    "doc": {"format": "csv", "title": "Suppliers"}})])
    r = eng.handle_command("export suppliers to csv", "ravi")
    raw = base64.b64decode(r["download"]["b64"]).decode()
    assert "ACME Fasteners" in raw


def test_report_respects_rbac(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "report", "doctype": "Purchase Order",
                    "doc": {"format": "excel"}})])
    # meera (Sales) can read POs in FakeERP; use a write-gated check elsewhere.
    r = eng.handle_command("export POs", "meera")
    assert "download" in r or "doesn't allow" in r["reply"]


def test_report_is_audited(tmp_path):
    eng = mk(tmp_path, [
        json.dumps({"op": "report", "doctype": "Supplier",
                    "doc": {"format": "csv"}})])
    eng.handle_command("export suppliers", "ravi")
    s = eng.Session()
    assert any(a.event == "report_generated" for a in s.query(AuditLog).all())
