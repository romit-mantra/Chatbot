"""The engine: interpret -> gather -> draft -> confirm -> commit -> read-back -> learn.

Extends the tested Slice-1 spine with:
- RBAC backstop per user (governance.user_can) before ANY erp call
- memory-aware interpretation (personal defaults + org norms pre-loaded)
- screen context (the embedded widget passes the doc the user is viewing)
- approve-with-edits (the human's diff becomes an org-memory lesson)
- "remember ..." explicit memory commands
- the daily brief (per-user morning summary)
Interpretation is pluggable: RuleInterpreter (deterministic, offline, used in
tests) or LLMInterpreter (production; same loop, model plugs in).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from governance import classify, Tier, user_can, USERS
from store import Job, Proposal, audit
from memory import MemoryService


@dataclass
class Intent:
    kind: str
    params: dict = field(default_factory=dict)


class RuleInterpreter:
    def interpret(self, text: str, erp, context: dict | None = None) -> Intent:
        t = text.lower().strip()
        if t.startswith("remember "):
            parts = text.strip().split(maxsplit=2)  # keep the value's original case
            if len(parts) == 3:
                return Intent("remember", {"key": parts[1].lower(), "value": parts[2]})
        ctx_doc = (context or {}).get("doctype"), (context or {}).get("name")
        if any(w in t for w in ["this", "it"]) and ctx_doc[0]:
            if any(w in t for w in ["status", "late", "where", "detail", "about"]):
                return Intent("about_context", {"doctype": ctx_doc[0], "name": ctx_doc[1]})
        item = self._item(t, erp)
        if any(w in t for w in ["delete", "remove"]) and "gl" in t:
            return Intent("delete", {"doctype": "GL Entry"})
        if any(w in t for w in ["stock", "how much", "kitna", "available"]) and item:
            return Intent("fetch_stock", {"item": item})
        if any(w in t for w in ["po", "purchase", "order", "reorder", "raise", "buy"]) and item:
            return Intent("raise_po", {"item": item})
        return Intent("unknown", {})

    @staticmethod
    def _item(t, erp):
        for r in erp.search("Item"):
            if r["name"].lower() in t:
                return r["name"]
        if "screw" in t:
            return "M6 hex screw"
        if "bolt" in t:
            return "M8 bolt"
        return None


# LLMInterpreter lives in interpreter_llm.py (real implementation).


class Engine:
    def __init__(self, erp, Session, interpreter=None, planner=None):
        self._default_erp = erp
        import threading
        self._erp_local = threading.local()
        self.Session = Session
        self.memory = MemoryService(Session)
        self.interpreter = interpreter or RuleInterpreter()
        self.planner = planner   # if set, the general any-doctype path is used
        # short-term conversation memory, per user (last N turns)
        self._history: dict[str, list[dict]] = {}

    # ---- per-request ERP binding (act-as-user), thread-safe ----
    @property
    def erp(self):
        return getattr(self._erp_local, "value", None) or self._default_erp

    @erp.setter
    def erp(self, adapter):
        # None resets this thread to the default (bot) adapter
        self._erp_local.value = adapter

    HISTORY_TURNS = 12

    def _hist(self, actor: str) -> list[dict]:
        return self._history.setdefault(actor, [])

    def _remember_turn(self, actor: str, role: str, content: str) -> None:
        h = self._hist(actor)
        h.append({"role": role, "content": content})
        del h[:-self.HISTORY_TURNS]

    def clear_history(self, actor: str) -> None:
        self._history.pop(actor, None)

    # ---------------------------------------------------- GENERAL (any doctype)
    def _general(self, s, job, actor, text, context, act):
        """Executes a model-produced plan through the generic ERP primitives.
        Works against ANY real ERPNext doctype — no hardcoded field names."""
        from governance import classify, Tier, user_can
        self._remember_turn(actor, "user", text)
        try:
            plan = self.planner.plan(text, context, self._hist(actor))
        except Exception as e:
            act.append({"tag": "plan", "text": f"could not plan: {e}"})
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": "I didn't follow that. Try asking about the document "
                             "on screen, a count, a list, or ask me to create one."}

        op = plan["op"]
        dt_ = plan.get("doctype")
        act.append({"tag": "plan", "text": f"op={op} doctype={dt_ or '-'}"})

        # ---- agent: multi-step / edit / cancel / compose-anything requests
        if op == "agent":
            return self._agent(s, job, actor, text, context, act)

        # ---- remember
        if op == "remember":
            d = plan.get("doc") or {}
            if d.get("key"):
                self.memory.remember_personal(actor, d["key"], str(d.get("value","")))
                act.append({"tag": "go", "text": f"stored memory {d['key']}"})
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"Noted — I'll remember {d['key']} = "
                                 f"{d.get('value')} for you."}

        # ---- refuse (blocked by policy)
        if op == "refuse":
            act.append({"tag": "stop", "text": "GATE blocked (ledger integrity)"})
            audit(s, "blocked", actor, request=text)
            job.status = "blocked"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": "I can't delete or cancel ledger/accounting records — "
                             "that's blocked to protect data integrity. I can draft "
                             "a reversing entry for approval instead."}

        # ---- answer about the document on screen (grounded in the REAL record)
        if op == "answer_context" and context and context.get("name"):
            cdt, cname = context["doctype"], context["name"]
            if not user_can(actor, cdt, "get"):
                return self._deny(s, job, act, actor, cdt, "read")
            try:
                doc = self.erp.get(cdt, cname)
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": (self._polite_denial(cdt, str(e))
                                  if "403" in str(e) or "permission" in
                                  str(e).lower()
                                  else f"I couldn't read {cdt} {cname}: {e}")}
            # attachments: list them; if the question is about a file, read it
            try:
                atts = self.erp.list_attachments(cdt, cname)
            except Exception:
                atts = []
            if atts:
                doc = dict(doc)
                doc["_attached_files"] = [a.get("file_name") for a in atts]
                act.append({"tag": "info",
                            "text": f"{len(atts)} attachment(s) on {cname}"})
                q = (plan.get("question") or text).lower()
                if any(w in q for w in ("attach", "file", "document", "pdf",
                                        "quotation", "upload", "read")):
                    from files import extract_bytes
                    for a in atts[:3]:
                        try:
                            raw = self.erp.fetch_file(a["file_url"])
                            excerpt, kind = extract_bytes(a["file_name"], raw)
                            doc[f"_content_of_{a['file_name']}"] = excerpt[:6000]
                            act.append({"tag": "info", "text":
                                        f"read attachment {a['file_name']} ({kind})"})
                        except Exception as ex:
                            doc[f"_content_of_{a.get('file_name')}"] = \
                                f"(could not read: {ex})"
            act += [{"tag": "info", "text": f"read {cdt} {cname} "
                     f"({len(doc)} fields)"},
                    {"tag": "go", "text": "auto: read-only"}]
            reply = self.planner.answer_from(doc, plan.get("question") or text)
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": reply,
                    "suggestions": self._suggestions("answer_context", cdt, True)}

        # ---- comment: add a note / tag someone on a document (a light write)
        if op == "comment":
            d = plan.get("doc") or {}
            cdt = dt_ or (context or {}).get("doctype")
            cname = plan.get("name") or (context or {}).get("name")
            if not cname:
                import re as _re
                m = _re.search(r"\b([A-Z]{2,}[A-Z-]*-\d{4}-\d{3,})\b", text)
                if m:
                    cname = m.group(1)
                    if not cdt:
                        pref = {"PUR-ORD": "Purchase Order", "SAL-ORD": "Sales Order",
                                "ACC-SINV": "Sales Invoice", "ACC-PINV": "Purchase Invoice"}
                        cdt = next((dc for p, dc in pref.items()
                                    if cname.startswith(p)), "Purchase Order")
            if not (cdt and cname):
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": "Which document should I comment on? Open it, or "
                                 "tell me its ID."}
            body = d.get("text", "")
            if d.get("tag"):
                body = f"@{d['tag']} " + body
            try:
                self.erp.add_comment(cdt, cname, body)
                act.append({"tag": "go", "text": f"commented on {cdt} {cname}"})
                audit(s, "comment_added", actor, doctype=cdt, name=cname)
                job.status = "done"; s.commit()
                who = f" and tagged {d['tag']}" if d.get("tag") else ""
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"Added your comment to {cname}{who}."}
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": self._friendly_erp_error(str(e))}

        # ---- report: build a downloadable file (Excel / CSV / PDF)
        if op == "report" and dt_:
            if not user_can(actor, dt_, "get"):
                return self._deny(s, job, act, actor, dt_, "read")
            filters = plan.get("filters") or {}
            try:
                rows = self.erp.search(dt_)
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": (self._polite_denial(dt_, str(e))
                                  if "403" in str(e) or "permission" in
                                  str(e).lower()
                                  else f"I couldn't read {dt_}: {e}")}
            from planner import fuzzy_match, apply_date_range
            if filters:
                rows = fuzzy_match(rows, filters)
            if plan.get("date_range"):
                rows = apply_date_range(rows, plan["date_range"])
            d = plan.get("doc") or {}
            fmt = (d.get("format") or "excel").lower()

            # child-table enrichment: "users WITH ROLES" must contain roles.
            # The planner sets doc.include_child = {"field": "roles",
            # "value_field": "role"}; we fetch each full doc (bounded) and
            # join the child values into one readable column.
            inc = d.get("include_child")
            # GUARANTEE: "users with roles" gets roles even if the planner
            # forgot include_child — detect the ask from the sentence.
            if not inc:
                import re as _re
                if dt_ == "User" and _re.search(r"\broles?\b", text.lower()):
                    inc = {"field": "roles", "value_field": "role"}
                else:
                    m = _re.search(r"\bwith\s+(items|roles|taxes)\b",
                                   text.lower())
                    if m:
                        inc = {"field": m.group(1),
                               "value_field": {"items": "item_code",
                                               "roles": "role",
                                               "taxes": "account_head"}[m.group(1)]}
            if inc and inc.get("field"):
                cap = 60
                enriched = []
                for r in rows[:cap]:
                    try:
                        full = self.erp.get(dt_, r.get("name"))
                        vals = [str(c.get(inc.get("value_field") or "name"))
                                for c in (full.get(inc["field"]) or [])]
                        r = dict(r)
                        r[inc["field"]] = ", ".join(v for v in vals if v)
                    except Exception:
                        pass
                    enriched.append(r)
                rows = enriched + rows[cap:]
                # the enriched field must survive whatever columns were chosen
                if d.get("columns") and inc["field"] not in d["columns"]:
                    d["columns"] = list(d["columns"]) + [inc["field"]]
                act.append({"tag": "info",
                            "text": f"enriched {min(len(rows), cap)} row(s) "
                                    f"with {inc['field']}"})
            title = d.get("title") or f"{dt_} report"
            from reports import build_report
            try:
                fileinfo = build_report(rows, fmt, title, dt_, columns=d.get("columns"))
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"I couldn't build that report: {e}"}
            act.append({"tag": "go", "text": f"built {fmt} report: "
                        f"{len(rows)} rows"})
            audit(s, "report_generated", actor, doctype=dt_, fmt=fmt,
                  rows=len(rows))
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": f"Here's your **{title}** — {len(rows)} "
                             f"{dt_} record(s), ready to download.",
                    "download": fileinfo}   # {filename, mimetype, b64}

        # ---- grant_role: privilege change — doubly gated, SM-only both ends
        if op == "grant_role":
            d = plan.get("doc") or {}
            tgt, role = d.get("user"), d.get("role")
            from governance import user_roles
            actor_roles = set(user_roles(actor) or [])
            if not actor_roles & {"System Manager", "Administrator"}:
                act.append({"tag": "stop", "text": "grant_role denied: not SM"})
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": "I'm sorry — granting roles is restricted to "
                                 "System Managers. Your account can't stage "
                                 "this change."}
            if not (tgt and role):
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": "Which user, and which role? e.g. "
                                 "\"grant Purchase User to priya@company.com\""}
            try:
                current = self.erp.get("User", tgt)
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"I couldn't find user {tgt}: "
                                 f"{self._friendly_erp_error(str(e))}"}
            before = [r.get("role") for r in (current.get("roles") or [])]
            if role in before:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"{tgt} already has the {role} role."}
            prop = Proposal(job_id=job.id, actor=actor, doctype="User",
                            draft_name=tgt, action="grant_role", value=0,
                            tier="approve",
                            reason="privilege change — System Manager approval",
                            summary=f"Grant role '{role}' to {tgt}",
                            payload_json=json.dumps({"role": role,
                                                     "roles_before": before}))
            s.add(prop); s.commit()
            audit(s, "proposal_staged", actor, proposal_id=prop.id,
                  doctype="User", action="grant_role", role=role, user=tgt)
            act.append({"tag": "hold", "text": f"GATE grant_role -> approve"})
            job.status = "awaiting_approval"; s.commit()
            return {"job_id": job.id, "activity": act,
                    "proposals": [self._view(prop)],
                    "reply": f"Staged for approval: grant **{role}** to {tgt}. "
                             f"Their current roles: "
                             f"{', '.join(before) or '(none)'}. A System "
                             f"Manager must approve this."}

        # ---- write: free-form artifact (memo/email/summary) as a download
        if op == "write":
            d = plan.get("doc") or {}
            content = d.get("content") or ""
            title = (d.get("title") or "document").strip()[:60]
            ext = "md" if (d.get("format") or "md") == "md" else "txt"
            import base64 as _b
            dl = {"filename": f"{title}.{ext}",
                  "mimetype": "text/markdown" if ext == "md" else "text/plain",
                  "b64": _b.b64encode(content.encode()).decode()}
            audit(s, "artifact_written", actor, title=title, chars=len(content))
            act.append({"tag": "go", "text": f"wrote {title}.{ext} "
                        f"({len(content)} chars)"})
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": f"Here's **{title}** — preview below, download "
                             f"attached.\n\n---\n{content[:1200]}",
                    "download": dl,
                    "suggestions": ["make it more formal",
                                    "shorten it to half", "convert to txt"]}

        # ---- chitchat: greetings & small talk (never the robotic menu)
        if op == "chitchat":
            reply = (plan.get("doc") or {}).get("reply") or \
                "Hello! What can I help you with in the ERP today?"
            act.append({"tag": "go", "text": "social reply"})
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": reply,
                    "suggestions": self._suggestions("chitchat", None, False)}

        # ---- explain: how-to / knowledge about ERPNext (no data lookup)
        if op == "explain":
            from governance import user_roles
            act.append({"tag": "go", "text": "knowledge answer (no data read)"})
            reply = self.planner.explain(plan.get("question") or text,
                                         user_roles(actor))
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": reply,
                    "suggestions": self._suggestions("explain", dt_, False)}

        # ---- query: fetch real rows, let the model ANALYZE them (any doctype)
        if op == "query" and dt_:
            if not user_can(actor, dt_, "get"):
                return self._deny(s, job, act, actor, dt_, "read")
            filters = plan.get("filters") or {}
            try:
                # fetch FULL rows (all fields) so sums/analysis are possible
                rows = self.erp.search(dt_)
            except Exception as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": (self._polite_denial(dt_, str(e))
                                  if "403" in str(e) or "permission" in
                                  str(e).lower()
                                  else f"I couldn't read {dt_}: {e}")}
            total = len(rows)
            all_rows = list(rows)          # before any filtering
            from planner import fuzzy_match, apply_date_range
            if filters:
                rows = fuzzy_match(rows, filters)
            dr = plan.get("date_range")
            if dr:
                rows = apply_date_range(rows, dr)
            desc = []
            if filters: desc.append(str(filters))
            if dr: desc.append(f"{dr.get('from','')}..{dr.get('to','')}")
            act.append({"tag": "info", "text": f"read {dt_}: {total} row(s)"
                        + (f", {len(rows)} match " + " ".join(desc) if desc else "")})
            act.append({"tag": "go", "text": "auto: read-only"})
            filt_desc = dict(filters or {})
            if plan.get("date_range"):
                filt_desc["date_range"] = plan["date_range"]
            # EXACT aggregation over ALL rows (no 200-row arithmetic risk);
            # the model narrates verified numbers + sees a small sample.
            from analytics import compute
            stats = compute(rows)
            # If a filter/date-range emptied the set, ALSO report the unfiltered
            # picture — users ask compound questions ("this month or till now")
            # and "none found" alone is unhelpful when 50 records exist.
            if not rows and (filters or plan.get("date_range")):
                stats["filtered_result"] = "no rows matched the filter"
                stats["unfiltered"] = compute(all_rows)
                act.append({"tag": "info",
                            "text": f"0 matched; {len(all_rows)} exist overall"})
            act.append({"tag": "info",
                        "text": f"aggregated {stats['row_count']} row(s) exactly"})
            reply = self.planner.analyze(rows, plan.get("question") or text,
                                         dt_, filt_desc, stats=stats)
            job.status = "done"; s.commit()
            chart = self._chart_from_stats(stats, dt_)
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": reply, "chart": chart,
                    "suggestions": self._suggestions("query", dt_, bool(rows))}

        # ---- need_info: ask for the missing required fields (don't guess)
        if op == "need_info":
            missing = plan.get("missing") or []
            act.append({"tag": "plan", "text": f"missing: {', '.join(missing)}"})
            job.status = "done"; s.commit()
            q = plan.get("question") or (
                f"To draft that {dt_ or 'document'} I need: "
                + ", ".join(missing) + ".")
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": q}

        # ---- create (drafts-first + governance gate, any doctype)
        if op == "create" and dt_:
            if not user_can(actor, dt_, "create_draft"):
                return self._deny(s, job, act, actor, dt_, "create")
            doc = plan.get("doc") or {}
            if not doc:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"I can draft a {dt_} — tell me the key details "
                                 f"(e.g. supplier/customer, item, quantity, rate)."}
            try:
                doc = self._complete_doc(dt_, doc, act)
            except ValueError as e:
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": str(e)}
            try:
                draft = self.erp.create_draft(dt_, doc)
            except Exception as e:
                msg = self._friendly_erp_error(str(e))
                act.append({"tag": "stop", "text": f"draft rejected by ERP: {msg}"})
                job.status = "done"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": msg}
            value = float(draft.get("grand_total") or draft.get("total") or 0)
            act.append({"tag": "go", "text": f"draft {draft['name']} created "
                        f"(docstatus 0)"})
            tier, reason = classify(dt_, "submit", value)
            act.append({"tag": "hold" if tier == Tier.APPROVE else "stop",
                        "text": f"GATE submit {dt_} -> {tier.value} ({reason})"})
            if tier is Tier.BLOCK:
                job.status = "blocked"; s.commit()
                return {"job_id": job.id, "activity": act, "proposals": [],
                        "reply": f"Drafted {draft['name']}, but submitting is "
                                 f"blocked: {reason}"}
            prop = Proposal(job_id=job.id, actor=actor, doctype=dt_,
                            draft_name=draft["name"], action="submit", value=value,
                            tier=tier.value, reason=reason,
                            summary=f"{dt_} {draft['name']}"
                                    + (f" · {value}" if value else ""),
                            payload_json=json.dumps(draft, default=str))
            s.add(prop); s.commit()
            audit(s, "proposal_staged", actor, proposal_id=prop.id, doctype=dt_,
                  value=value)
            job.status = "awaiting_approval"; s.commit()
            return {"job_id": job.id, "activity": act,
                    "reply": f"I've drafted **{draft['name']}** ({dt_}). It is NOT "
                             f"submitted — review and approve below.",
                    "proposals": [self._view(prop)]}

        job.status = "done"; s.commit()
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": "I can answer about the document you're viewing, count or "
                         "list records, draft a new document for approval, or "
                         "remember a preference. What would you like?"}


    # ------------------------------------------------- document completion
    def _complete_doc(self, doctype: str, doc: dict, act: list) -> dict:
        """Fill the fields ERPNext REQUIRES but users never say out loud:
        company, dates, and a well-formed items table. Resolve partial party
        names ("Summit" -> "Summit Traders Ltd."). Raise ValueError with a
        friendly message if something essential genuinely cannot be resolved."""
        import datetime as _dt
        from planner import fuzzy_match
        from dates import normalise, normalise_doc_dates
        doc = dict(doc)

        # Users type "20-07-2026" (DD-MM-YYYY) and sometimes run two dates
        # together. ERPNext's API requires YYYY-MM-DD, so normalise first.
        for note in normalise_doc_dates(doc, ["transaction_date", "schedule_date",
                                              "delivery_date", "due_date"]):
            act.append({"tag": "info", "text": f"date {note}"})

        # company — resolve partial names ("mantra" -> "Mantra Softech")
        try:
            comps = self.erp.search("Company")
        except Exception:
            comps = []
        if comps:
            given = doc.get("company")
            if not given:
                doc["company"] = comps[0]["name"]        # only one? just use it
                act.append({"tag": "info",
                            "text": f"company: {doc['company']}"})
            else:
                hit = fuzzy_match(comps, {"name": given})
                if hit:
                    doc["company"] = hit[0]["name"]
                    if hit[0]["name"] != given:
                        act.append({"tag": "info",
                                    "text": f"company: '{given}' -> "
                                            f"'{hit[0]['name']}'"})
                else:
                    names = ", ".join(c["name"] for c in comps)
                    raise ValueError(
                        f"I couldn't find a company called '{given}'. "
                        f"Available: {names}")

        # LEARNING APPLIED: fill blanks from norms learned from human edits.
        # Without this, "learning" only records — it never changes behaviour.
        learned = self.memory.org_norms(doctype)
        for key, val in learned.items():
            if key.startswith("preferred_"):
                field = key[len("preferred_"):]
                if field not in doc or doc.get(field) in (None, ""):
                    doc[field] = val
                    act.append({"tag": "info",
                                "text": f"applied learned default {field}={val}"})

        today = _dt.date.today()
        soon = (today + _dt.timedelta(days=7)).isoformat()
        party_field = {"Purchase Order": "supplier",
                       "Sales Order": "customer"}.get(doctype)
        party_dt = {"supplier": "Supplier", "customer": "Customer"}.get(party_field)
        date_field = {"Purchase Order": "schedule_date",
                      "Sales Order": "delivery_date"}.get(doctype, "schedule_date")

        # resolve a partial party name against the real records
        if party_field:
            given = doc.get(party_field)
            try:
                parties = self.erp.search(party_dt)
            except Exception:
                parties = []
            if not given:
                names = [p["name"] for p in parties][:8]
                raise ValueError(
                    f"Which {party_field} should this {doctype} go to? "
                    + ("Options: " + ", ".join(names) if names else ""))
            hit = fuzzy_match(parties, {"name": given})
            if hit:
                doc[party_field] = hit[0]["name"]
                if hit[0]["name"] != given:
                    act.append({"tag": "info",
                                "text": f"{party_field}: '{given}' -> "
                                        f"'{hit[0]['name']}'"})
            # if no match, pass through and let ERPNext validate

        doc.setdefault("transaction_date", today.isoformat())
        doc.setdefault(date_field, soon)

        # items table
        items = doc.get("items") or []
        if not items:
            raise ValueError(f"What item(s) and quantity should the {doctype} "
                             f"contain?")
        fixed = []
        for it in items:
            it = dict(it)
            code = it.get("item_code") or it.get("item")
            if not code:
                raise ValueError("Which item code should I use?")
            # resolve partial item codes
            try:
                matches = fuzzy_match(self.erp.search("Item"), {"name": code})
                if matches:
                    code = matches[0]["name"]
            except Exception:
                pass
            it["item_code"] = code
            it.pop("item", None)
            it["qty"] = float(it.get("qty") or 1)
            if not it.get("rate"):
                it["rate"] = self._last_rate(doctype, doc.get(party_field), code)
                if it["rate"]:
                    act.append({"tag": "info", "text": f"rate {it['rate']} from "
                                "the last order for this item"})
                else:
                    it["rate"] = 0
            row_date = normalise(it.get(date_field)) or doc.get(date_field) or soon
            it[date_field] = row_date
            fixed.append(it)
        doc["items"] = fixed
        return doc



    @staticmethod
    def _polite_denial(doctype: str, err: str) -> str:
        """Turn a raw 403 into a kind, honest refusal with a next step."""
        who = "your account" if "chitragupta-bot" not in err else \
              "the assistant's service account"
        return (f"I'm sorry — {who} doesn't have permission to view "
                f"{doctype} records. That area is restricted to specific "
                f"roles. If you need access, your System Manager can grant "
                f"the role in ERPNext (Users → Roles).")

    @staticmethod
    def _friendly_erp_error(raw: str) -> str:
        """Turn an ERPNext traceback into something a human can act on."""
        import re
        m = re.search(r"Could not find ([A-Za-z ]+): ([^\\\"]+)", raw)
        if m:
            return (f"ERPNext doesn't have a {m.group(1).strip()} called "
                    f"'{m.group(2).strip()}'. Could you check the exact name?")
        m = re.search(r"MandatoryError.*?: (.+?)[\\\"]", raw)
        if m:
            return f"ERPNext needs this field before it will accept the draft: {m.group(1)}"
        if "LinkValidationError" in raw:
            return ("ERPNext couldn't match one of the names I used (supplier, "
                    "item, or company). Could you give me the exact name?")
        if "PermissionError" in raw or "403" in raw:
            return "Your ERPNext role doesn't permit that. I can't override it."
        return "ERPNext rejected that draft. " + raw[:180]

    def _last_rate(self, doctype: str, party, item_code):
        """Reuse the rate from the most recent order for this item (the
        'previous pricing' behaviour asked for from day one)."""
        if not item_code:
            return None
        try:
            rows = self.erp.search(doctype)
        except Exception:
            return None
        rows = [r for r in rows if r.get("docstatus") == 1]
        rows.sort(key=lambda r: str(r.get("transaction_date", "")), reverse=True)
        for r in rows[:20]:
            try:
                full = self.erp.get(doctype, r["name"])
            except Exception:
                continue
            for it in (full.get("items") or []):
                if it.get("item_code") == item_code and it.get("rate"):
                    return float(it["rate"])
        return None


    # ==================================================== GENERIC AGENT LOOP
    AGENT_MAX_STEPS = 8

    def _agent(self, s, job, actor, text, context, act):
        """Multi-step compose-anything path. Every step is governed."""
        from agent import AGENT_SYSTEM, parse_tool_call, truncate_rows, MAX_STEPS
        from governance import classify, Tier, user_can
        import json as _json

        convo = [f"User request: {text}"]
        if context:
            convo.append(f"Screen context: {_json.dumps(context)}")
        hist = self._hist(actor)
        if hist:
            past = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in hist[-6:])
            convo.append(f"Recent conversation:\n{past}")
        system = AGENT_SYSTEM.replace("{max_steps}", str(MAX_STEPS))
        transcript = "\n\n".join(convo)
        proposals_out = []
        reply = None

        for step in range(MAX_STEPS):
            try:
                raw = self.planner.client.complete(system, transcript)
                call = parse_tool_call(raw)
            except Exception as e:
                act.append({"tag": "stop", "text": f"agent parse error: {e}"})
                break
            tool, args = call["tool"], call.get("args", {})
            act.append({"tag": "plan", "text": f"step {step+1}: {tool} "
                        f"{_json.dumps(args)[:120]}"})
            if tool == "finish":
                reply = (args.get("reply") or "").strip()
                if len(reply) < 20:          # "Done." and friends: not an answer
                    reply = ("Here's where I got to: " +
                             "; ".join(a["text"] for a in act
                                       if a.get("tag") in ("info", "go"))[-500:]
                             or "I couldn't complete that — could you rephrase "
                                "what you need?")
                break
            try:
                result = self._agent_tool(s, job, actor, tool, args, act,
                                          proposals_out)
            except PermissionError as e:
                result = {"error": f"not permitted: {e}"}
                act.append({"tag": "stop", "text": str(e)})
            except Exception as e:
                result = {"error": str(e)[:300]}
                act.append({"tag": "stop", "text": f"{tool} failed: "
                            f"{str(e)[:120]}"})
            transcript += (f"\n\nStep {step+1} — you called {tool}"
                           f"({_json.dumps(args)[:200]}).\nResult: "
                           f"{_json.dumps(result, default=str)[:2500]}")
        if reply is None:
            done = [a["text"] for a in act if a.get("tag") in ("info", "go")]
            reply = ("That took more steps than I'm allowed in one go. "
                     + ("So far: " + "; ".join(done[-4:]) + ". "
                        if done else "")
                     + "Ask me the specific part you need and I'll finish it.")
        job.status = "awaiting_approval" if proposals_out else "done"
        s.commit()
        return {"job_id": job.id, "activity": act, "reply": reply,
                "proposals": proposals_out}

    def _agent_tool(self, s, job, actor, tool, args, act, proposals_out):
        """Execute ONE agent tool under full governance. Reads run; writes stage."""
        from governance import classify, Tier, user_can
        import json as _json
        from agent import truncate_rows
        dt_ = args.get("doctype")

        if tool == "search":
            if not dt_:
                raise ValueError("search needs a doctype")
            rows = self.erp.search(dt_)
            filters = args.get("filters") or {}
            if filters:
                from planner import fuzzy_match
                rows = fuzzy_match(rows, filters)
            act.append({"tag": "info", "text": f"search {dt_}: {len(rows)} row(s)"})
            return {"rows": truncate_rows(rows), "count": len(rows)}

        if tool == "get":
            doc = self.erp.get(dt_, args.get("name"))
            act.append({"tag": "info", "text": f"get {dt_} {args.get('name')}"})
            return {"doc": {k: v for k, v in doc.items()
                            if not str(k).startswith("_")}}

        if tool == "create_draft":
            if not user_can(actor, dt_, "create_draft"):
                raise PermissionError(f"{actor} may not create {dt_}")
            doc = self._complete_doc(dt_, args.get("doc") or {}, act)
            draft = self.erp.create_draft(dt_, doc)
            act.append({"tag": "go", "text": f"draft {draft['name']} created "
                        f"(docstatus 0)"})
            value = float(draft.get("grand_total") or draft.get("total") or 0)
            self._stage(s, job, actor, dt_, draft["name"], "submit", value,
                        f"{dt_} {draft['name']}" + (f" · {value}" if value else ""),
                        draft, act, proposals_out)
            return {"created": draft.get("name"),
                    "staged_for_approval": True,
                    "draft": {k: draft.get(k) for k in
                              ("name", "supplier", "customer", "grand_total",
                               "total", "schedule_date") if k in draft}}

        if tool == "propose_update":
            if not user_can(actor, dt_, "update"):
                raise PermissionError(f"{actor} may not update {dt_}")
            name, patch = args.get("name"), args.get("patch") or {}
            current = self.erp.get(dt_, name)      # verifies existence + read
            self._stage(s, job, actor, dt_, name, "update", 0,
                        f"Update {dt_} {name}: "
                        + ", ".join(f"{k}→{v}" for k, v in patch.items()),
                        {"patch": patch, "current": {k: current.get(k)
                                                     for k in patch}},
                        act, proposals_out)
            return {"staged_update": name, "patch": patch}

        if tool == "propose_submit":
            if not user_can(actor, dt_, "submit"):
                raise PermissionError(f"{actor} may not submit {dt_}")
            name = args.get("name")
            doc = self.erp.get(dt_, name)
            if doc.get("docstatus") == 1:
                return {"already_submitted": name}
            value = float(doc.get("grand_total") or doc.get("total") or 0)
            self._stage(s, job, actor, dt_, name, "submit", value,
                        f"Submit {dt_} {name}" + (f" · {value}" if value else ""),
                        doc, act, proposals_out)
            return {"staged_submit": name}

        if tool == "propose_cancel":
            if not user_can(actor, dt_, "cancel"):
                raise PermissionError(f"{actor} may not cancel {dt_}")
            name = args.get("name")
            doc = self.erp.get(dt_, name)
            self._stage(s, job, actor, dt_, name, "cancel",
                        float(doc.get("grand_total") or 0),
                        f"Cancel {dt_} {name}", doc, act, proposals_out)
            return {"staged_cancel": name}

        if tool == "list_attachments":
            atts = self.erp.list_attachments(dt_, args.get("name"))
            act.append({"tag": "info", "text": f"{len(atts)} attachment(s)"})
            return {"attachments": [{k: a.get(k) for k in
                                     ("file_name", "file_url", "file_size")}
                                    for a in atts]}

        if tool == "read_attachment":
            from files import extract_bytes
            raw = self.erp.fetch_file(args.get("file_url"))
            excerpt, kind = extract_bytes(args.get("file_name") or
                                          args.get("file_url"), raw)
            act.append({"tag": "info", "text":
                        f"read attachment {args.get('file_name')} ({kind})"})
            return {"kind": kind, "content": excerpt[:8000]}

        if tool == "comment":
            name = args.get("name")
            body = args.get("text", "")
            if args.get("tag"):
                body = f"@{args['tag']} " + body
            self.erp.add_comment(dt_, name, body)
            audit(s, "comment_added", actor, doctype=dt_, name=name)
            act.append({"tag": "go", "text": f"commented on {dt_} {name}"})
            return {"commented": name}

        raise ValueError(f"unknown tool {tool!r}")

    def _stage(self, s, job, actor, doctype, name, action, value, summary,
               payload, act, proposals_out):
        """Create a pending proposal (an approval card) through the gate."""
        from governance import classify, Tier
        import json as _json
        tier, reason = classify(doctype, action, value)
        act.append({"tag": "hold" if tier == Tier.APPROVE else "stop",
                    "text": f"GATE {action} {doctype} -> {tier.value} ({reason})"})
        if tier == Tier.BLOCK:
            raise PermissionError(f"{action} on {doctype} is blocked: {reason}")
        prop = Proposal(job_id=job.id, actor=actor, doctype=doctype,
                        draft_name=name, action=action, value=value,
                        tier=tier.value, reason=reason, summary=summary,
                        payload_json=_json.dumps(payload, default=str))
        s.add(prop); s.commit()
        audit(s, "proposal_staged", actor, proposal_id=prop.id,
              doctype=doctype, action=action, value=value)
        proposals_out.append(self._view(prop))


    @staticmethod
    def _suggestions(op: str, doctype: str | None, had_rows: bool) -> list[str]:
        """2-3 next steps to show as tappable chips. Non-technical users don't
        know what to ask; showing the next move is the biggest adoption lever."""
        dt_ = doctype or "records"
        if op == "query" and had_rows:
            d = (doctype or "").lower()
            if any(w in d for w in ("purchase", "supplier", "material")):
                return ["break this down by supplier", "export this to Excel",
                        "show only this month"]
            if any(w in d for w in ("sales", "customer", "invoice", "quotation")):
                return ["break this down by customer", "export this to Excel",
                        "show only this month"]
            if "user" in d:
                return ["who was added most recently?",
                        "how many are disabled?", "export this to Excel"]
            return [f"summarise these {dt_} records",
                    "export this to Excel", "show only this month"]
        if op == "answer_context":
            return ["who created this?", "add a comment on this",
                    "what files are attached?"]
        if op == "explain":
            return ["do it for me instead", "what else can my role do?"]
        if op == "report":
            return ["same report as PDF", "filter to last month"]
        if op == "chitchat":
            return ["what can my role do?", "how many purchase orders are there?",
                    "show my pending approvals"]
        if op in ("create", "need_info"):
            return ["show my pending approvals", "what did I order last time?"]
        return ["how many purchase orders are there?",
                "total revenue this month", "what can my role do?"]


    @staticmethod
    def _chart_from_stats(stats: dict, doctype: str) -> dict | None:
        """Turn the primary group-by into a small bar chart for the widget.
        Only when there's something worth charting (2-12 groups)."""
        for key, groups in (stats or {}).items():
            if not key.startswith("by_") or not isinstance(groups, dict):
                continue
            labels, values = [], []
            for label, vals in groups.items():
                num = next((v for k, v in vals.items()
                            if k.startswith("sum_")), vals.get("count", 0))
                labels.append(str(label)[:18])
                values.append(round(float(num), 2))
            if 2 <= len(labels) <= 12 and any(values):
                metric = next((k[4:] for k in next(iter(groups.values()))
                               if k.startswith("sum_")), "count")
                return {"title": f"{doctype} — {key[3:]} by {metric}",
                        "labels": labels, "values": values}
        return None

    def _deny(self, s, job, act, actor, doctype, what):
        from governance import user_roles
        audit(s, "rbac_denied", actor, doctype=doctype, action=what)
        act.append({"tag": "stop", "text": f"RBAC: {actor} lacks {what} on {doctype}"})
        job.status = "denied"; s.commit()
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": f"Your role ({', '.join(user_roles(actor))}) doesn't allow "
                         f"{what} on {doctype}. I can hand this to someone who can."}

    # ------------------------------------------------------------------ command
    def handle_command(self, text: str, actor: str, context: dict | None = None,
                       file: dict | None = None) -> dict:
        out = self._handle_command(text, actor, context, file)
        # record what we said, so the next turn has the full thread
        if isinstance(out, dict) and out.get("reply"):
            self._remember_turn(actor, "assistant", out["reply"])
        return out

    def _handle_command(self, text: str, actor: str, context: dict | None = None,
                        file: dict | None = None) -> dict:
        s = self.Session()
        if actor not in USERS:
            return {"error": "unknown user", "reply": "I don't recognize this user."}
        job = Job(actor=actor, text=text)
        s.add(job); s.commit()
        act = [{"tag": "plan", "text": f"interpreting for {actor} "
                f"({', '.join(USERS[actor]['roles'])})"}]
        if file:
            try:
                from files import extract
                excerpt, kind = extract(file)
                act.append({"tag": "info", "text": f"read {kind}: {file.get('name')}"})
                text = (f"{text}\n\n[Attached file '{file.get('name')}' "
                        f"({kind}) contents:]\n{excerpt}")
            except Exception as e:
                act.append({"tag": "stop", "text": f"could not read file: {e}"})
        mem = self.memory.recall(actor)
        if mem["personal"]:
            act.append({"tag": "info", "text": f"memory: {len(mem['personal'])} personal "
                        f"preference(s) loaded"})
        # GENERAL path (real ERPNext, any doctype) when a model is configured
        if getattr(self, "planner", None):
            return self._general(s, job, actor, text, context, act)

        intent = self.interpreter.interpret(text, self.erp, context)

        if intent.kind == "remember":
            self.memory.remember_personal(actor, intent.params["key"], intent.params["value"])
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act + [{"tag": "go",
                    "text": f"stored personal memory {intent.params['key']}"}],
                    "reply": f"Noted — I'll remember {intent.params['key']} = "
                             f"{intent.params['value']} for you. You can see or delete "
                             f"everything I've learned about you anytime.",
                    "proposals": []}
        if intent.kind == "about_context":
            return self._about_context(s, job, intent, act, actor)
        if intent.kind == "fetch_stock":
            return self._fetch_stock(s, job, intent, act, actor)
        if intent.kind == "raise_po":
            return self._raise_po(s, job, intent, act, actor, mem)
        if intent.kind == "delete":
            return self._blocked(s, job, intent, act, actor)
        job.status = "done"; s.commit()
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": "I can fetch anything and prepare any change you're allowed to "
                         "make. Try stock questions, raising a PO, or 'remember "
                         "default_warehouse Unit-2'."}

    # ------------------------------------------------------------------ reads
    def _guard(self, s, job, actor, doctype, action, act):
        if not user_can(actor, doctype, action):
            audit(s, "rbac_denied", actor, doctype=doctype, action=action)
            act.append({"tag": "stop", "text": f"RBAC: {actor} lacks {action} on {doctype}"})
            job.status = "denied"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": f"Your role doesn't allow {action} on {doctype}. "
                             f"I can hand this to someone who can, if you'd like."}
        return None

    def _about_context(self, s, job, intent, act, actor):
        dt_, name = intent.params["doctype"], intent.params["name"]
        denied = self._guard(s, job, actor, dt_, "get", act)
        if denied:
            return denied
        doc = self.erp.get(dt_, name)
        act += [{"tag": "info", "text": f"screen context: {dt_} {name}"},
                {"tag": "go", "text": "auto: read-only"}]
        job.status = "done"; s.commit()
        nice = ", ".join(f"{k}: {v}" for k, v in doc.items()
                         if k not in ("docstatus",))
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": f"You're looking at {dt_} **{name}** — {nice}."}

    def _fetch_stock(self, s, job, intent, act, actor):
        denied = self._guard(s, job, actor, "Item", "get", act)
        if denied:
            return denied
        item = self.erp.get("Item", intent.params["item"])
        act += [{"tag": "info", "text": f"read Item {item['name']}"},
                {"tag": "go", "text": "auto: read-only, no approval"}]
        job.status = "done"; s.commit()
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": f"{item['name']}: {item['stock_qty']} in stock "
                         f"(reorder level {item['reorder_level']})."}

    # ------------------------------------------------------------------ action
    def _raise_po(self, s, job, intent, act, actor, mem):
        denied = (self._guard(s, job, actor, "Item", "get", act)
                  or self._guard(s, job, actor, "Purchase Order", "create_draft", act))
        if denied:
            return denied
        item = self.erp.get("Item", intent.params["item"])
        act.append({"tag": "info", "text": f"stock {item['stock_qty']} vs reorder "
                    f"{item['reorder_level']}"})
        sups = sorted(self.erp.search("Item Supplier", {"item": item["name"]}),
                      key=lambda r: r["last_rate"])
        if not sups:
            job.status = "done"; s.commit()
            return {"job_id": job.id, "activity": act, "proposals": [],
                    "reply": f"No approved suppliers for {item['name']} — which vendor "
                             f"should I consider? I'll remember for next time."}
        best = sups[0]
        # org memory can override the rate if a human correction taught us one
        learned = mem["org"].get(f"preferred_rate")
        rate = float(learned) if learned else best["last_rate"]
        note = " (using the rate you corrected last time)" if learned else \
               f" (matched to last PO {best['last_po']})"
        act.append({"tag": "info", "text": f"{len(sups)} suppliers; best "
                    f"{best['supplier']} @ ₹{rate}"})
        qty = max(item["reorder_level"] - item["stock_qty"], 0) + item["reorder_level"]
        wh = mem["personal"].get("default_warehouse")
        doc = {"supplier": best["supplier"], "item": item["name"], "qty": qty,
               "rate": rate, "total": round(qty * rate, 2), "terms": best["terms"]}
        if wh:
            doc["warehouse"] = wh
            act.append({"tag": "info", "text": f"memory: default warehouse {wh}"})
        draft = self.erp.create_draft("Purchase Order", doc)
        act.append({"tag": "go", "text": f"draft {draft['name']} qty {qty} @ ₹{rate} "
                    f"= ₹{doc['total']}"})
        tier, reason = classify("Purchase Order", "submit", doc["total"])
        act.append({"tag": "hold" if tier == Tier.APPROVE else "stop",
                    "text": f"GATE submit Purchase Order -> {tier.value} ({reason})"})
        prop = Proposal(job_id=job.id, actor=actor, doctype="Purchase Order",
                        draft_name=draft["name"], action="submit", value=doc["total"],
                        tier=tier.value, reason=reason,
                        summary=f"PO to {best['supplier']} · {qty} {item['uom']} · "
                                f"₹{doc['total']}",
                        payload_json=json.dumps(draft))
        s.add(prop); s.commit()
        audit(s, "proposal_staged", actor, proposal_id=prop.id,
              doctype="Purchase Order", value=doc["total"])
        job.status = "awaiting_approval"; s.commit()
        return {"job_id": job.id, "activity": act,
                "reply": f"Drafted a PO to **{best['supplier']}** for {qty} "
                         f"{item['name']} at ₹{rate}{note}, total ₹{doc['total']}. "
                         f"Ready for approval — you can edit any field before approving.",
                "proposals": [self._view(prop)]}

    def _blocked(self, s, job, intent, act, actor):
        tier, reason = classify(intent.params["doctype"], "delete")
        act.append({"tag": "stop", "text": f"GATE delete {intent.params['doctype']} -> "
                    f"{tier.value} ({reason})"})
        audit(s, "blocked", actor, doctype=intent.params["doctype"], action="delete")
        job.status = "blocked"; s.commit()
        return {"job_id": job.id, "activity": act, "proposals": [],
                "reply": "I can't delete ledger entries — blocked to protect ledger "
                         "integrity. I can draft a reversing Journal Entry instead."}

    # ------------------------------------------------- approve / edit / reject
    def approve(self, proposal_id: int, approver: str, edits: dict | None = None) -> dict:
        s = self.Session()
        prop = s.get(Proposal, proposal_id)
        if not prop:
            return {"error": "unknown proposal"}
        if prop.status != "pending":
            return {"error": f"proposal is {prop.status}"}
        if prop.tier == Tier.BLOCK.value:
            return {"error": "blocked proposals cannot be approved"}
        if not user_can(approver, prop.doctype, "submit"):
            audit(s, "rbac_denied", approver, proposal_id=prop.id, action="approve")
            return {"error": f"{approver} lacks submit permission on {prop.doctype}"}

        original = prop.payload
        payload = dict(original)
        if edits:
            clean = {k: v for k, v in edits.items() if k not in ("name", "docstatus")}
            if clean:
                if "qty" in clean or "rate" in clean:
                    q = float(clean.get("qty", payload.get("qty", 0)))
                    r = float(clean.get("rate", payload.get("rate", 0)))
                    clean["total"] = round(q * r, 2)
                payload.update(clean)
                self.erp.update_draft(prop.doctype, prop.draft_name, clean)
                prop.edited = True

        prop.status = "approved"; prop.payload_json = json.dumps(payload); s.commit()
        audit(s, "approved", approver, proposal_id=prop.id, edited=prop.edited)

        # LEARN from this decision. Edits become org norms that are applied to
        # future drafts; clean approvals reinforce the values they contained.
        learn_notes = self.memory.on_approved(approver, prop.doctype, payload,
                                              bool(prop.edited), original)
        self.memory.skill_run(f"{prop.action} {prop.doctype}", prop.doctype,
                              clean=not prop.edited)

        # Execute per action type. Every path ends with a read-back check.
        if prop.action == "grant_role":
            from governance import user_roles as _ur
            if not set(_ur(approver) or []) & {"System Manager",
                                               "Administrator"}:
                return {"error": "Only a System Manager can approve a role "
                                 "grant."}
            role = payload.get("role")
            self.erp.add_role(prop.draft_name, role)
            readback = self.erp.get("User", prop.draft_name)
            after = [r.get("role") for r in (readback.get("roles") or [])]
            ok = role in after
            submitted = {"name": prop.draft_name}
            audit(s, "role_granted", approver, user=prop.draft_name, role=role,
                  roles_before=payload.get("roles_before"), roles_after=after,
                  readback_ok=ok)
        elif prop.action == "update":
            patch = payload.get("patch", {})
            self.erp.update_draft(prop.doctype, prop.draft_name, patch)
            readback = self.erp.get(prop.doctype, prop.draft_name)
            ok = all(str(readback.get(k)) == str(v) for k, v in patch.items())
            submitted = {"name": prop.draft_name}
        elif prop.action == "cancel":
            cancelled = self.erp.cancel(prop.doctype, prop.draft_name)
            readback = self.erp.get(prop.doctype, cancelled["name"])
            ok = readback.get("docstatus") == 2
            submitted = cancelled
        else:  # submit (the default) — via the company Workflow if one exists
            wf = None
            try:
                wf = self.erp.get_workflow(prop.doctype)
            except Exception:
                wf = None
            if wf:
                doc = self.erp.get(prop.doctype, prop.draft_name)
                state = doc.get("workflow_state")
                tr = next((t for t in (wf.get("transitions") or [])
                           if t.get("state") == state), None)
                if tr:
                    self.erp.apply_workflow_action(prop.doctype,
                                                   prop.draft_name,
                                                   tr["action"])
                    readback = self.erp.get(prop.doctype, prop.draft_name)
                    ok = readback.get("workflow_state") == tr.get("next_state")
                    submitted = {"name": prop.draft_name}
                    audit(s, "workflow_advanced", approver,
                          proposal_id=prop.id, action=tr["action"],
                          to_state=tr.get("next_state"))
                else:
                    raise RuntimeError(
                        f"No workflow transition from state {state!r} — "
                        f"the document may need a different approver.")
            else:
                submitted = self.erp.submit(prop.doctype, prop.draft_name)
                readback = self.erp.get(prop.doctype, submitted["name"])
                ok = all(readback.get(k) == v for k, v in payload.items()
                         if k not in ("name", "docstatus"))
        prop.submitted_name = submitted["name"]
        prop.status = "executed" if ok else "verify_failed"; s.commit()
        audit(s, "committed", approver, proposal_id=prop.id,
              action=prop.action, submitted=submitted["name"], readback_ok=ok)
        for n in learn_notes:
            audit(s, "learned", approver, note=n)

        learned = self.memory.on_approved(approver, prop.doctype, payload,
                                          prop.edited, original)
        self.memory.skill_run(f"raise_{prop.doctype.lower().replace(' ', '_')}",
                              prop.doctype, clean=not prop.edited)
        return {"proposal_id": prop.id, "submitted": submitted["name"],
                "readback_ok": ok, "status": prop.status, "learned": learned}

    def reject(self, proposal_id: int, approver: str) -> dict:
        s = self.Session()
        prop = s.get(Proposal, proposal_id)
        if not prop or prop.status != "pending":
            return {"error": "not a pending proposal"}
        prop.status = "rejected"; s.commit()
        audit(s, "rejected", approver, proposal_id=prop.id)
        return {"proposal_id": prop.id, "status": "rejected"}

    def pending(self, user: str | None = None) -> list[dict]:
        s = self.Session()
        rows = s.query(Proposal).filter_by(status="pending").all()
        if user:
            rows = [p for p in rows if user_can(user, p.doctype, "submit")
                    or p.actor == user]
        return [self._view(p) for p in rows]

    # ------------------------------------------------------------------ brief
    def brief(self, user: str) -> dict:
        """The daily brief: per-user morning summary, permission-scoped."""
        s = self.Session()
        items = []
        approvals = self.pending(user)
        if approvals:
            items.append(f"{len(approvals)} approval(s) waiting on you")
        if user_can(user, "Item", "read"):
            try:
                # real ERPNext Items have no stock_qty field; guard with .get()
                low = [i for i in self.erp.search("Item")
                       if i.get("stock_qty") is not None
                       and i.get("reorder_level") is not None
                       and i["stock_qty"] < i["reorder_level"]]
                if low:
                    items.append("low stock: " + ", ".join(
                        f"{i['name']} ({i['stock_qty']}/{i['reorder_level']})"
                        for i in low))
            except Exception:
                pass
        if user_can(user, "Purchase Order", "read"):
            try:
                open_pos = [p for p in self.erp.search("Purchase Order")
                            if p.get("docstatus") == 1
                            and str(p.get("status", "")).startswith("To Receive")]
                if open_pos:
                    items.append(f"{len(open_pos)} PO(s) awaiting receipt")
            except Exception:
                pass
        skills = self.memory.skills()
        return {"user": user, "greeting": f"Morning, {USERS[user]['full_name']}.",
                "items": items or ["Nothing urgent — all clear."],
                "skills": skills, "pending": approvals}

    @staticmethod
    def _view(p: Proposal) -> dict:
        return {"id": p.id, "summary": p.summary, "tier": p.tier, "reason": p.reason,
                "doctype": p.doctype, "value": p.value, "actor": p.actor,
                "fields": p.payload}
