"""General planning + analytical answering over ANY ERPNext doctype.

WHY THIS WAS REWRITTEN
The first version gave the model a tiny op menu (count/list/get/create). Asked
for "total amount of all POs" it had no `sum` op, so it picked `count` and
answered "12 records" — the wrong SHAPE of answer. Asked for "Summit Traders
total" it emitted an exact filter {"supplier": "Summit Traders"} which does not
match the real value "Summit Traders Ltd.", so it found 0 rows and said so.
Both failures came from forcing the answer into pre-decided buckets.

THE FIX
1. ONE flexible `query` op: doctype + optional filters. The ENGINE fetches rows
   (permission-checked, as always).
2. Filters match FUZZILY (substring, case-insensitive) so "Zuckerman" finds
   "Zuckerman Security Ltd." — humans don't type legal names.
3. The MODEL then reads the actual rows and answers the actual question: sums,
   counts, comparisons, groupings — reasoning over real data instead of
   filling a template.

Safety unchanged: the model never touches the ERP. It names a doctype and
filters; the engine does every read under RBAC, and every WRITE still goes
through the governance gate and human approval.
"""

from __future__ import annotations

import json
import os
import urllib.request

ALLOWED_OPS = {"query", "answer_context", "explain", "create", "need_info",
               "comment", "report", "write", "agent", "grant_role", "chitchat", "remember", "refuse",
               "unknown"}

PLAN_SYSTEM = """You turn an ERP user's message into ONE structured plan.
You never execute anything - a governed engine does, under the user's own
permissions, with human approval for any change.

Reply with ONLY JSON, no prose, no fences:
{"op":"<op>","doctype":"<DocType or null>","filters":{},"fields":[],"doc":{},
 "question":"<the user's question, verbatim>"}

Ops:
- explain        : HOW-TO / knowledge questions about ERPNext itself - "how do
                   I raise a PO", "where is X", "what is the PO workflow",
                   "what does this status mean", "what is my role allowed to
                   do". No data lookup needed; you answer from ERP knowledge.
- date ranges    : for "this month / this year / in 2026 / last quarter /
                   between X and Y", DO NOT put the date in `filters`. Instead
                   set `date_range`: {"field": "transaction_date",
                   "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}. Compute the actual
                   from/to (today is provided). Use transaction_date for orders
                   and invoices unless the user names another field.
- query          : ANY question answered from records - counts, totals, sums,
                   averages, comparisons, groupings, listings, "which supplier
                   is biggest", "how many X", "total value of Y".
                   Set `doctype`. Set `filters` ONLY to narrow rows; the engine
                   matches filter values loosely, so partial names are fine
                   ("Zuckerman" matches "Zuckerman Security Ltd.").
                   DO NOT compute the answer yourself - just fetch the right
                   rows; you will be shown them and asked to answer afterwards.
- answer_context : a question about the document currently OPEN on screen.
- create         : the user wants to CREATE a document -> doctype + doc fields.
                   ERPNext documents need REQUIRED fields. For a Purchase Order:
                     supplier (name), company, transaction_date, schedule_date,
                     items: [{item_code, qty, rate, schedule_date}]
                   For a Sales Order: customer, company, transaction_date,
                     delivery_date, items:[{item_code, qty, rate, delivery_date}]
                   Put child rows in an `items` LIST inside `doc`.
                   If the user did NOT give a supplier/customer, DO NOT guess -
                   use op "need_info" and list what is missing.
- need_info      : the user wants to create something but required details are
                   STILL missing after considering the WHOLE conversation.
                   Set doctype, `doc` with everything gathered SO FAR, and
                   `missing`: only the fields still genuinely unknown.
                   `question` = a short ask for ONLY those.
                   NEVER re-ask for something already answered in an earlier
                   turn. If the user answers "1 MA inc, 2 546 inr, 3 15-08-2026"
                   to a numbered question, map those answers back to the fields
                   you asked for.
                   When you finally have everything, emit `create` - not
                   another need_info.
- remember       : "remember <key> <value>" -> doc {"key":..., "value":...}
- comment        : add a comment / note to a document, optionally tagging a
                   user. Set doctype+name (use screen context if "this"),
                   and doc {"text": "...", "tag": "Administrator" or null}.
- report         : YOU choose the columns: set doc.columns = ["field", ...]
                   — only fields a human wants for THIS request, in a sensible
                   order (identifiers first, then what they asked about).
                   Never include owner/creation/modified/docstatus/idx unless
                   asked. Also set a clear doc.title.
- report         : if the user asks for records WITH child-table data (e.g.
                   "users with roles", "POs with items"), set
                   doc.include_child = {"field": "<child fieldname>",
                   "value_field": "<field to show>"} — e.g. users with roles:
                   {"field": "roles", "value_field": "role"}. Without this the
                   file will NOT contain that data.
- report         : the user wants a downloadable REPORT/EXPORT (Excel/PDF/CSV)
                   of records - "give me a purchase report", "export all POs to
                   excel", "download supplier summary". Set doctype, filters,
                   and doc {"format": "excel"|"pdf"|"csv", "title": "..."}.
- explain        : ALSO for "what can I do?", "what can my role do?", "what
                   are my permissions?" — the user asking about THEMSELVES.
                   Answer from the roles you were given. NEVER route these to
                   agent; answer directly and fully.
- agent          : ONLY for comparing OTHER records' detail that list views
                   don't carry — roles/permissions of users, child-table
                   contents (e.g. "which user has the least permissions?",
                   "what items are on each of these POs?"). The agent can
                   `get` full documents (which include child tables like a
                   user's roles) and compare them. Prefer agent over saying
                   the data is missing.
- agent          : MULTI-STEP or EDIT/CANCEL requests — anything that needs
                   composing several operations, or changing existing documents:
                   "find all overdue POs and comment on each", "change the qty
                   on that draft to 100", "cancel PUR-ORD-...", "compare last
                   month's POs with this month and email... ". A step-by-step
                   agent will handle it; all changes still need human approval.
- grant_role     : "give/grant <role> to <user>", "make X a Purchase User".
                   The MOST security-sensitive operation. Set doc
                   {"user": "<email>", "role": "<exact ERPNext role name>"}.
                   It is staged for System-Manager approval; never instant.
- write          : compose a FREE-FORM document — supplier email, memo,
                   meeting summary, policy note. Put the full well-written
                   content in doc {"title": "...", "content": "...(markdown)",
                   "format": "md"|"txt"}. Use real data from the conversation.
- chitchat       : greetings, thanks, small talk ("hi", "hey hows it going",
                   "how are you", "who are you"). Put a SHORT warm human reply
                   in doc {"reply": "..."} — one or two friendly lines, offer
                   one concrete thing you could help with. NEVER a menu list.
- refuse         : asks to delete/cancel a LEDGER or accounting record
                   (GL Entry, Account). Cancelling normal documents like
                   Purchase Orders is fine — use `agent` for those.
- unknown        : cannot be mapped.

Guidance:
- ERPNext DocTypes are Title Case singular: "Purchase Order", "Sales Order",
  "Item", "Supplier", "Customer", "Purchase Invoice", "Sales Invoice".
- Money questions about purchase orders -> doctype "Purchase Order".
- If the user names a supplier/customer/item, put it in filters, e.g.
  {"supplier": "Zuckerman"} - partial is fine.
- If they ask about "this"/"it" and a document is open -> answer_context.
- Data questions ("how many", "total", "which supplier") -> query.
  Knowledge questions ("how do I", "where do I", "what is") -> explain.
- Use `unknown` very sparingly.

Today's date: {today}
Screen context (may be null): {context}
"""

ANALYZE_SYSTEM = """You are an ERP analyst. You are given REAL records from the
user's ERPNext and their question. Answer the question from the data.

Rules:
- Compute what is asked: sums, counts, averages, comparisons, breakdowns.
- LEAD with the answer in one bold line, e.g.
  "**Summit Traders Ltd. - INR 2,62,800** across 3 orders."
- Then, if useful, a short markdown table or bullets for the breakdown.
- Format money with the currency in the data (usually INR), Indian grouping.
- NEVER show your working, corrections, or "let me recalculate". Compute
  silently and present ONLY the final, correct result.
- If the records lack what is needed, say exactly what is missing.
  NEVER invent numbers or records.
- Be concise. No preamble. Markdown only - no JSON, no raw pipes outside a
  proper table.
- If zero records were found BUT the aggregates include an "unfiltered" block,
  say plainly that none match the filter AND give the overall figure from
  "unfiltered" (e.g. "No POs in August 2026 — but there are 50 in total worth
  INR X"). Users often ask compound questions ("this month or till now");
  answer BOTH parts when the data is present.
- If zero records were found and there is no unfiltered block, say so and
  suggest what might match instead.
"""

EXPLAIN_SYSTEM = """You are an ERPNext expert helping a colleague inside their
ERP. Answer their how-to / knowledge question clearly and practically.

- Give the actual steps in ERPNext, with the real menu path where useful
  (e.g. Buying > Purchase Order > Add Purchase Order).
- Mention what their ROLE allows, using the roles given to you.
- NEVER invent or guess a role. If the roles list includes "Administrator" or
  "System Manager", the user has FULL system access — say so. If the list is
  empty, say you can't determine their roles rather than naming one.
- If they can do it through you (the assistant), say so - e.g. "you can also
  just tell me: 'raise a PO for 100 units of SKU001 from Summit Traders' and
  I'll draft it for your approval."
- Be concise. Use short bullets or numbered steps. No filler.
- Never invent ERPNext features that don't exist.
"""

ANSWER_SYSTEM = """You answer a question about ONE ERP record, using only the
JSON provided. Be short, plain, specific. Use real values.
- 'owner' = who created it; 'modified_by' = who last changed it.
- Money: include the currency. Never invent data.
- If the field isn't present, say you can't see it.
"""


class ModelClient:
    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


class AnthropicClient(ModelClient):
    # Known-good model strings (August 2026). There is no "Sonnet 5":
    #   claude-fable-5     — newest, most capable (Mythos-class)
    #   claude-opus-4-8    — very strong reasoning
    #   claude-sonnet-4-6  — the balanced default (recommended)
    #   claude-haiku-4-5-20251001 — fastest/cheapest (planning, chitchat)
    # claude-sonnet-5: strict upgrade over 4.6; intro $2/$10 per M tokens
    # until 31 Aug 2026 (then $3/$15) — better AND cheaper: the default.
    # claude-fable-5 exists but is premium-priced; not recommended here.
    KNOWN_MODELS = ("claude-sonnet-5", "claude-opus-4-8", "claude-sonnet-4-6",
                    "claude-haiku-4-5-20251001")

    def __init__(self, model: str | None = None, purpose: str = "chat"):
        """purpose: 'chat' (answers/analytics), 'agent' (tool loop), or
        'plan' (intent classification). Each can run a different model:
          CHITRAGUPTA_MODEL         default for everything
          CHITRAGUPTA_MODEL_AGENT   override for the multi-step agent
          CHITRAGUPTA_MODEL_PLAN    override for planning (haiku = big savings)
        Switch models with an env change + restart; /api/version shows them."""
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        base = os.environ.get("CHITRAGUPTA_MODEL", "claude-sonnet-5")
        per = {"agent": os.environ.get("CHITRAGUPTA_MODEL_AGENT"),
               "plan": os.environ.get("CHITRAGUPTA_MODEL_PLAN")}
        self.model = model or per.get(purpose) or base
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        body = json.dumps({"model": self.model, "max_tokens": 2000,
                           "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json", "x-api-key": self.key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")

    def stream(self, system, user):  # pragma: no cover
        """Yield text deltas as they arrive (Anthropic SSE)."""
        body = json.dumps({"model": self.model, "max_tokens": 2000,
                           "system": system, "stream": True,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json", "x-api-key": self.key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=90) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                if ev.get("type") == "content_block_delta":
                    t = (ev.get("delta") or {}).get("text", "")
                    if t:
                        yield t


class ScriptedClient(ModelClient):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise RuntimeError("scripted client exhausted")
        return self.responses.pop(0)


def _parse_json(raw: str) -> dict:
    raw = raw.replace("```json", "").replace("```", "").strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON in model output")
    return json.loads(raw[i:j + 1])


def apply_date_range(rows, date_range):
    """Filter rows whose date field falls within [from, to] inclusive.

    Handles ERPNext ISO dates and DD-MM-YYYY. A row whose date can't be parsed
    is EXCLUDED from a ranged query (it can't be shown to satisfy the range)."""
    if not date_range:
        return rows
    from dates import normalise
    field = date_range.get("field") or "transaction_date"
    lo = normalise(date_range.get("from")) if date_range.get("from") else None
    hi = normalise(date_range.get("to")) if date_range.get("to") else None
    out = []
    for r in rows:
        raw = r.get(field)
        d = normalise(raw) if raw else None
        if not d:
            continue
        if lo and d < lo:
            continue
        if hi and d > hi:
            continue
        out.append(r)
    return out


def fuzzy_match(rows, filters):
    """Loose, case-insensitive substring matching on filter values.

    Real ERPNext stores "Zuckerman Security Ltd."; humans type "Zuckerman".
    Exact-match filters silently return 0 rows - which is how the assistant
    previously answered "0 records" for a supplier that plainly had orders.
    """
    if not filters:
        return rows
    out = []
    for r in rows:
        ok = True
        for k, v in filters.items():
            got = r.get(k)
            if got is None:
                alt = next((kk for kk in r
                            if kk.replace("_name", "") == str(k).replace("_name", "")),
                           None)
                got = r.get(alt) if alt else None
            if got is None:
                ok = False
                break
            if isinstance(v, str) and isinstance(got, str):
                if v.strip().lower() not in got.strip().lower():
                    ok = False
                    break
            elif str(got) != str(v):
                ok = False
                break
        if ok:
            out.append(r)
    return out


class Planner:
    def __init__(self, client):
        self.client = client

    def plan(self, text, context, history=None):
        """history: [{"role":"user"/"assistant","content":...}] earlier turns.

        WITHOUT this the assistant asked for the same fields forever: each turn
        was planned in isolation, so "1 MA inc, 2 546 inr" was meaningless and
        it re-asked for details the user had already given.
        """
        t = text.strip()
        if t.lower().startswith("remember "):
            parts = t.split(maxsplit=2)
            if len(parts) == 3:
                return {"op": "remember",
                        "doc": {"key": parts[1].lower(), "value": parts[2]},
                        "question": t}
        import datetime as _dt
        system = (PLAN_SYSTEM
                  .replace("{today}", _dt.date.today().isoformat())
                  .replace("{context}", json.dumps(context) if context else "null"))
        convo = ""
        if history:
            lines = []
            for m in history[-10:]:
                who = "User" if m["role"] == "user" else "You"
                lines.append(f"{who}: {m['content']}")
            convo = ("Conversation so far (use it! the user may have already "
                     "given details in earlier turns - CARRY THEM FORWARD and "
                     "do not ask again):\n" + "\n".join(lines) + "\n\n")
        plan = _parse_json(self.client.complete(system, convo + "Latest message: " + t))
        if plan.get("op") not in ALLOWED_OPS:
            raise ValueError("disallowed op %r" % plan.get("op"))
        if plan["op"] == "answer_context" and not context:
            plan["op"] = "query"
        plan.setdefault("missing", [])
        plan.setdefault("date_range", None)
        plan.setdefault("filters", {})
        plan.setdefault("fields", [])
        plan.setdefault("doc", {})
        plan.setdefault("question", t)
        return plan

    def analyze(self, rows, question, doctype, filters, stats=None):
        """Model narrates EXACT precomputed stats; a sample gives colour."""
        sample = [self._slim(r) for r in rows[:25]]
        payload = json.dumps(sample, default=str)[:20000]
        head = ("DocType: %s\nFilters applied: %s\nRecords found: %d\n"
                % (doctype, filters or "none", len(rows)))
        if stats:
            head += ("\nEXACT AGGREGATES (computed over ALL %d rows — use "
                     "THESE numbers verbatim; do NOT recompute from the "
                     "sample):\n%s\n" % (len(rows),
                                          json.dumps(stats, default=str)))
        head += "\nSample records (first %d, for names/context only):\n%s" % (
            len(sample), payload)
        return self.client.complete(
            ANALYZE_SYSTEM, "%s\nQuestion: %s" % (head, question)).strip()

    def explain(self, question, roles):
        """Answer a how-to / knowledge question about ERPNext."""
        return self.client.complete(
            EXPLAIN_SYSTEM,
            "The user's ERPNext roles: %s\n\nQuestion: %s" % (
                ", ".join(roles) or "unknown", question)).strip()

    def answer_from(self, record, question):
        payload = json.dumps(record, default=str)[:8000]
        return self.client.complete(
            ANSWER_SYSTEM, "Record:\n%s\n\nQuestion: %s" % (payload, question)).strip()

    @staticmethod
    def _slim(r):
        drop = {"doctype", "idx", "parent", "parentfield", "parenttype",
                "_user_tags", "_comments", "_assign", "_liked_by", "amended_from",
                "naming_series", "letter_head", "language", "print_heading"}
        return {k: v for k, v in r.items()
                if k not in drop and not k.startswith("_") and v not in (None, "", [])}


def make_planner():
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return Planner(AnthropicClient())
        except Exception:
            return None
    return None
