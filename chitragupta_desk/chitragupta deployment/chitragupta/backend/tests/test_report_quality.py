"""Report quality — the 'not like Claude.ai' fixes, from real generated PDFs:
system columns dropped, dates/booleans humanised, and 'users WITH ROLES'
actually contains the roles."""
import os, sys, uuid, json, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from erp import FakeERP
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient
from reports import _columns, _fmt_cell


def test_system_columns_dropped_and_cells_humanised():
    rows = [{"name": "a@x.com", "owner": "Administrator",
             "creation": "2026-02-20 15:24:44.735925", "modified_by": "x",
             "docstatus": 0, "idx": 0, "enabled": 1}]
    cols = _columns(rows)
    assert "owner" not in cols and "creation" not in cols and "idx" not in cols
    assert _fmt_cell("enabled", 1) == "Yes"
    assert _fmt_cell("creation", "2026-02-20 15:24:44.735925") == "2026-02-20"


def test_users_with_roles_report_contains_roles(tmp_path):
    erp = FakeERP()
    erp.store["User"] = {
        "a@x.com": {"name": "a@x.com", "enabled": 1,
                    "roles": [{"role": "Stock User"}, {"role": "QC Manager"}]},
        "b@x.com": {"name": "b@x.com", "enabled": 0,
                    "roles": [{"role": "Sales User"}]},
    }
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "report", "doctype": "User",
                    "doc": {"format": "csv", "title": "All Users with Roles",
                            "include_child": {"field": "roles",
                                              "value_field": "role"}}})])))
    r = eng.handle_command("report of all users with roles in csv", "admin")
    csv_text = base64.b64decode(r["download"]["b64"]).decode()
    assert "Stock User, QC Manager" in csv_text     # THE fix: roles present
    assert "Sales User" in csv_text
    assert "Administrator" not in csv_text.split("\n")[0]  # no owner col


def test_model_chosen_columns_are_respected(tmp_path):
    erp = FakeERP()
    erp.store["User"] = {"a@x.com": {"name": "a@x.com", "enabled": 1,
                                     "full_name": "A", "language": "en"}}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "report", "doctype": "User",
                    "doc": {"format": "csv", "title": "Users",
                            "columns": ["full_name", "name"]}})])))
    r = eng.handle_command("report of users, just name and full name", "admin")
    header = base64.b64decode(r["download"]["b64"]).decode().split("\n")[0]
    assert header.strip() == "full_name,name"      # model's choice, its order


def test_write_op_produces_artifact(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(FakeERP(), make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "write",
                    "doc": {"title": "Delivery reminder",
                            "content": "Dear ACME,\n\nPlease expedite...",
                            "format": "md"}})])))
    r = eng.handle_command("write an email to ACME about faster delivery",
                           "ravi")
    assert r["download"]["filename"] == "Delivery reminder.md"
    assert "Dear ACME" in r["reply"]


def test_roles_column_survives_model_chosen_columns(tmp_path):
    """LIVE BUG: model chose columns without 'roles' -> roles vanished even
    though the user asked 'with roles'. Engine must guarantee the column."""
    erp = FakeERP()
    erp.store["User"] = {"a@x.com": {"name": "a@x.com", "full_name": "A",
                                     "enabled": 1,
                                     "roles": [{"role": "Stock User"}]}}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(erp, make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "report", "doctype": "User",
                    "doc": {"format": "csv", "title": "Users with Roles",
                            # model chose columns WITHOUT roles AND forgot
                            # include_child — the worst case, as seen live:
                            "columns": ["name", "full_name", "enabled"]}})])))
    r = eng.handle_command("give me a report of all users with roles", "admin")
    csv_text = base64.b64decode(r["download"]["b64"]).decode()
    assert "roles" in csv_text.split("\n")[0]
    assert "Stock User" in csv_text
