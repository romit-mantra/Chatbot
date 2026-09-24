"""The generic operation engine — a governed agentic tool-loop.

This is the "compose anything" layer. Instead of one fixed operation per
message, the model works STEP BY STEP with a small set of tools:

  search / get                    reads — execute immediately (ERPNext enforces
                                  read permissions; 403s surface honestly)
  create_draft                    reversible (docstatus 0) — executes, and the
                                  SUBMIT is staged as a proposal
  propose_update / propose_submit
  propose_cancel                  writes — NEVER executed here; each becomes a
                                  pending proposal (an approval card)
  comment                         light write — executes, audited
  finish                          end the loop with the reply to the user

Every single step passes through the same RBAC write-gate and risk tiers as
before. The model composes; the engine governs. A jailbroken model can at most
stage proposals a human must approve — locked by tests.

The loop is bounded (MAX_STEPS) and every step is recorded in the activity
stream so the user can watch it work.
"""

from __future__ import annotations

import json

MAX_STEPS = 8

AGENT_SYSTEM = """You are Chitragupta's operations agent inside an ERPNext
system, acting for a specific user. You work in STEPS. At each step reply with
ONLY one JSON object — a single tool call, no prose, no fences:

{"tool": "<name>", "args": {...}}

Tools:
- search   args {"doctype": "...", "filters": {...}}   filters optional; values
           match loosely (partial names fine). Returns rows.
- get      args {"doctype": "...", "name": "..."}      one full document.
- create_draft   args {"doctype": "...", "doc": {...}} creates an UNSUBMITTED
           draft; its submit is automatically staged for human approval.
           For Purchase/Sales Orders put child rows in doc["items"].
- propose_update args {"doctype": "...", "name": "...", "patch": {...}}
           stage a field change for human approval (works on drafts).
- propose_submit args {"doctype": "...", "name": "..."} stage submission of an
           existing draft for human approval.
- propose_cancel args {"doctype": "...", "name": "..."} stage cancellation of a
           submitted document for human approval.
- list_attachments args {"doctype": "...", "name": "..."} files attached to a
           document (quotations, signed PDFs, spreadsheets...).
- read_attachment  args {"file_url": "...", "file_name": "..."} extract the
           text of an attachment (PDF/Excel/CSV/TXT) so you can answer from it.
- comment  args {"doctype": "...", "name": "...", "text": "...", "tag": null}
           add a timeline comment, optionally tagging a user.
- finish   args {"reply": "..."} REQUIRED final step: the answer/summary for
           the user, in clear markdown. Mention what was staged for approval.
           The reply MUST substantively answer the user's question — never
           "Done.", never one word. If you achieved nothing, say what you
           tried and what's needed.

Rules:
- "Does <user> have permission for <doctype>?" / "what can <user> do?" =
  ONE step: get {"doctype": "User", "name": "<email>"} — the roles child
  table is in the result. Compare roles to the doctype (e.g. Sales Invoice
  needs an Accounts/Sales role) and finish with a clear yes/no + which roles
  they have. Do NOT search other doctypes for this.
- Plan silently; emit only tool calls. One call per step.
- Reads first when you need information. Never invent names or values — search.
- You CANNOT submit, update or cancel anything yourself; those tools only
  STAGE a proposal for human approval. Say so in your finish reply.
- Respect the user's request scope. If something is impossible or forbidden,
  finish with a short honest explanation.
- ERPNext DocTypes are Title Case: "Purchase Order", "Sales Order", "Item".
- Dates: YYYY-MM-DD.
- At most {max_steps} steps including finish. Be efficient.

The user's message, screen context and any prior conversation follow."""


def parse_tool_call(raw: str) -> dict:
    raw = raw.replace("```json", "").replace("```", "").strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON tool call in model output")
    call = json.loads(raw[i:j + 1])
    if "tool" not in call:
        raise ValueError("missing 'tool'")
    call.setdefault("args", {})
    return call


def truncate_rows(rows: list, limit: int = 30) -> list:
    """Keep tool results small enough to feed back to the model."""
    slim = []
    for r in rows[:limit]:
        slim.append({k: v for k, v in r.items()
                     if not str(k).startswith("_") and v not in (None, "", [])})
    return slim
