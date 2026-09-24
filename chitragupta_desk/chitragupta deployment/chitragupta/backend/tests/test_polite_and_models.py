"""Polite RBAC refusals + per-purpose model configuration."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from erp import FakeERP, PermissionDenied
from store import make_session
from engine import Engine
from planner import Planner, ScriptedClient


class DenyingERP(FakeERP):
    def search(self, doctype, **kw):
        if doctype == "Salary Slip":
            raise PermissionDenied(
                "403: User priya@m.com does not have doctype access via role "
                "permission for document Salary Slip")
        return super().search(doctype, **kw)


def test_denial_is_polite_and_helpful(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    eng = Engine(DenyingERP(), make_session(db), planner=Planner(ScriptedClient([
        json.dumps({"op": "query", "doctype": "Salary Slip"})])))
    r = eng.handle_command("show me salary slips", "ravi")
    rep = r["reply"]
    assert "I'm sorry" in rep and "System Manager" in rep
    assert "403" not in rep and "doctype access" not in rep   # no raw tech text


def test_model_purpose_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("CHITRAGUPTA_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("CHITRAGUPTA_MODEL_PLAN", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("CHITRAGUPTA_MODEL_AGENT", "claude-fable-5")
    from planner import AnthropicClient
    assert AnthropicClient(purpose="chat").model == "claude-sonnet-4-6"
    assert AnthropicClient(purpose="plan").model == "claude-haiku-4-5-20251001"
    assert AnthropicClient(purpose="agent").model == "claude-fable-5"
