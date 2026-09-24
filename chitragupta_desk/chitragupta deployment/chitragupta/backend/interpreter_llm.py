"""LLM interpretation layer.

Design (the safety-critical part):
- The model's ONLY job is language -> structured Intent (constrained JSON).
  It never calls tools, never writes, never sees credentials.
- Whatever it returns is validated against a whitelist of intent kinds and
  then passes through the SAME RBAC + governance gate as any other request.
  A jailbroken or hallucinating model can at worst produce a wrong intent,
  which is then permission-checked, gated, and human-confirmed like all others.
- The model client is pluggable:
    AnthropicClient  -> production (needs ANTHROPIC_API_KEY)
    ScriptedClient   -> tests (deterministic, offline)
  On ANY model failure (network, bad JSON, unknown intent) we fall back to the
  RuleInterpreter, so the assistant degrades gracefully instead of breaking.
"""

from __future__ import annotations

import json
import os
import urllib.request


ALLOWED_INTENTS = {"fetch_stock", "raise_po", "about_context", "remember",
                   "delete", "unknown"}

SYSTEM = """You translate a user's message into ONE structured intent for an ERP
assistant. You do not execute anything; a governed engine does, under the
user's permissions, with human approval for any change.

Reply with ONLY a JSON object, no prose, no fences:
{"kind": "<intent>", "params": {...}}

Intents:
- fetch_stock  params: {"item": "<item name>"}      — stock/availability questions
- raise_po     params: {"item": "<item name>"}      — reorder / raise a purchase order
- about_context params: {}                          — questions about "this"/the open document
- remember     params: {"key": "<snake_key>", "value": "<value>"} — user asks you to remember a preference
- delete       params: {"doctype": "<doctype>"}     — user asks to delete something
- unknown      params: {}                           — anything else

Known items: {items}
Screen context (may be null): {context}
If the user references "this/it" and context exists, prefer about_context.
Map item mentions to the closest known item name exactly as listed.
Never invent intents outside the list. When unsure: unknown."""


class ModelClient:
    """Interface: complete(system, user) -> str (the raw model text)."""
    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


class AnthropicClient(ModelClient):
    """Production client. Requires ANTHROPIC_API_KEY in the environment."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        body = json.dumps({"model": self.model, "max_tokens": 300,
                           "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json",
                     "x-api-key": self.key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")


class ScriptedClient(ModelClient):
    """Deterministic client for tests: returns queued responses in order."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise RuntimeError("scripted client exhausted")
        return self.responses.pop(0)


class LLMInterpreter:
    """Language -> Intent via a model, validated, with rule fallback."""

    def __init__(self, client: ModelClient, fallback=None):
        from engine import Intent, RuleInterpreter  # local import avoids cycle
        self._Intent = Intent
        self.client = client
        self.fallback = fallback or RuleInterpreter()

    def interpret(self, text: str, erp, context: dict | None = None):
        try:
            items = ", ".join(r["name"] for r in erp.search("Item"))
            system = SYSTEM.replace("{items}", items).replace(
                "{context}", json.dumps(context) if context else "null")
            raw = self.client.complete(system, text)
            out = self._parse(raw)
            kind, params = out["kind"], out.get("params", {}) or {}
            if kind not in ALLOWED_INTENTS:
                raise ValueError(f"disallowed intent {kind!r}")
            if kind == "about_context":
                if not context:
                    raise ValueError("about_context without context")
                params = {"doctype": context["doctype"], "name": context["name"]}
            if kind in ("fetch_stock", "raise_po"):
                known = {r["name"] for r in erp.search("Item")}
                if params.get("item") not in known:
                    raise ValueError(f"unknown item {params.get('item')!r}")
            if kind == "remember" and not (params.get("key") and
                                           str(params.get("value", "")).strip()):
                raise ValueError("remember missing key/value")
            return self._Intent(kind, params)
        except Exception:
            # graceful degradation: deterministic rules take over
            return self.fallback.interpret(text, erp, context)

    @staticmethod
    def _parse(raw: str) -> dict:
        raw = raw.replace("```json", "").replace("```", "").strip()
        i, j = raw.find("{"), raw.rfind("}")
        if i < 0 or j < 0:
            raise ValueError("no JSON in model output")
        return json.loads(raw[i:j + 1])


def make_interpreter():
    """Factory used by app.py: LLM if a key is present, rules otherwise."""
    from engine import RuleInterpreter
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return LLMInterpreter(AnthropicClient(
                model=os.environ.get("CHITRAGUPTA_MODEL",
                                     "claude-haiku-4-5-20251001")))
        except Exception:
            pass
    return RuleInterpreter()
