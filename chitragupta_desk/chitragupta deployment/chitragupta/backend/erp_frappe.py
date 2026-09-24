"""Real ERPNext adapter — Frappe native REST API.

Implements the six ERPAdapter methods against a live Frappe/ERPNext site.
No extra app required: uses Frappe's built-in /api/resource and /api/method
endpoints, which every ERPNext install exposes.

Auth: token-based (API key + secret) per Frappe's token auth:
    Authorization: token <api_key>:<api_secret>
Credentials are read from the environment — never hardcoded, never logged.

    export ERP_URL="http://192.168.179.128:8000"
    export ERP_API_KEY="..."
    export ERP_API_SECRET="..."

Frappe semantics preserved exactly as FakeERP mimics them:
  - insert  -> docstatus 0 (draft, reversible)   [create_draft]
  - update  -> only allowed while docstatus 0    [update_draft]
  - submit  -> docstatus 0 -> 1                  [submit]
so the drafts-first governance guarantee holds against the real ERP.

Safety: this class performs NO governance decisions. It is a dumb transport.
All tiering, RBAC and human approval happen in the engine above it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from erp import ERPAdapter, ERPError, PermissionDenied


def _frappe_message(raw: str) -> str:
    """Extract the human-readable complaint from a Frappe error response.

    Frappe buries the useful text in _server_messages (a JSON string containing
    a JSON list of JSON strings), or in exception/exc_type.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    msgs = []
    sm = data.get("_server_messages")
    if sm:
        try:
            for entry in json.loads(sm):
                try:
                    msgs.append(json.loads(entry).get("message", ""))
                except Exception:
                    msgs.append(str(entry))
        except Exception:
            msgs.append(str(sm))
    if not msgs and data.get("exception"):
        msgs.append(str(data["exception"]).split(":", 1)[-1].strip())
    if not msgs and data.get("message"):
        msgs.append(str(data["message"]))
    import re as _re
    text = " ".join(m for m in msgs if m).strip()
    return _re.sub(r"<[^>]+>", "", text)          # ERPNext embeds HTML in errors


class FrappeERP(ERPAdapter):
    """Live ERPNext adapter. Supports bot-token auth AND per-user session auth
    (act-as-user Phase B) — see __init__."""
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 api_secret: str | None = None, timeout: int = 30,
                 session_sid: str | None = None, csrf_token: str | None = None,
                 acting_user: str | None = None):
        """Two auth modes:

        TOKEN (the bot):     api_key/api_secret -> Authorization: token k:s
        SESSION (a person):  session_sid (+ csrf_token for writes) -> the call
                             runs AS that logged-in ERP user, so ERPNext
                             enforces THEIR permissions and stamps THEIR name
                             on owner/modified_by. This is act-as-user Phase B.
        """
        self.base = (base_url or os.environ.get("ERP_URL", "")).rstrip("/")
        if not self.base:
            raise ERPError("ERP_URL not set")
        self.timeout = timeout
        self.acting_user = acting_user
        self._sid = session_sid
        self._csrf = csrf_token
        if session_sid:
            self._auth = None                      # cookie mode
        else:
            key = api_key or os.environ.get("ERP_API_KEY", "")
            secret = api_secret or os.environ.get("ERP_API_SECRET", "")
            if not (key and secret):
                raise ERPError("ERP_API_KEY / ERP_API_SECRET not set")
            self._auth = f"token {key}:{secret}"

    # ---------------------------------------------------------------- transport
    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None):
        # NOTE: do NOT blanket-quote here. Callers pass already-encoded segments.
        # Blanket-quoting turned "Purchase Order" into %20 on POST, which Frappe's
        # resource router 404s (GET tolerates it, POST does not).
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json"}
        if self._auth:                              # bot token mode
            headers["Authorization"] = self._auth
        else:                                       # person session mode
            headers["Cookie"] = f"sid={self._sid}"
            if method != "GET" and self._csrf:
                headers["X-Frappe-CSRF-Token"] = self._csrf
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            human = _frappe_message(detail)
            if e.code in (401, 403):
                raise PermissionDenied(f"{e.code}: {human or detail[:300]}")
            # NOTE: previously a 404 discarded `detail`, hiding ERPNext's real
            # complaint behind "not found: POST /api/resource/Purchase%20Order".
            # ERPNext returns 404/417 for VALIDATION problems too, so always
            # surface what it actually said.
            raise ERPError(f"HTTP {e.code}: {human or detail[:300]}")
        except urllib.error.URLError as e:
            raise ERPError(f"cannot reach ERP at {self.base}: {e.reason}")
        # Frappe wraps results in "data" (resource API) or "message" (method API)
        if isinstance(payload, dict):
            if "data" in payload:
                return payload["data"]
            if "message" in payload:
                return payload["message"]
        return payload

    # ------------------------------------------------------------------ reads
    def ping(self) -> str:
        """Connectivity + auth check. Returns the logged-in user."""
        return self._call("GET", "/api/method/frappe.auth.get_logged_user")

    def describe(self, doctype: str) -> dict:
        """DocType metadata. OPTIONAL — a narrow bot role often cannot read the
        DocType table (that is correct and safe). We try the permission-friendly
        method endpoint first, then the resource endpoint, and if both are
        denied we degrade gracefully: the loop does not need introspection to
        read, draft, or submit."""
        try:
            meta = self._call("GET", "/api/method/frappe.client.get_meta",
                              params={"doctype": doctype})
        except (PermissionDenied, ERPError):
            try:
                meta = self._call(
                    "GET", f"/api/resource/DocType/{urllib.parse.quote(doctype)}")
            except (PermissionDenied, ERPError):
                return {"doctype": doctype, "fields": [], "is_submittable": None,
                        "meta_available": False}
        fields = [{"fieldname": f.get("fieldname"), "label": f.get("label"),
                   "fieldtype": f.get("fieldtype"), "reqd": f.get("reqd"),
                   "options": f.get("options")}
                  for f in (meta.get("fields") or [])]
        return {"doctype": doctype, "fields": fields,
                "is_submittable": bool(meta.get("is_submittable")),
                "meta_available": True}

    def search(self, doctype: str, filters: dict | None = None,
               fields: list[str] | None = None, limit: int = 50) -> list[dict]:
        params = {
            "fields": json.dumps(fields or ["*"]),
            "limit_page_length": limit,
        }
        if filters:
            # dict -> Frappe's [[field, "=", value], ...] form
            params["filters"] = json.dumps([[k, "=", v] for k, v in filters.items()])
        rows = self._call("GET", f"/api/resource/{urllib.parse.quote(doctype)}",
                          params=params)
        return rows or []

    def get(self, doctype: str, name: str) -> dict:
        return self._call(
            "GET",
            f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name)}")

    # ----------------------------------------------------------------- writes
    def create_draft(self, doctype: str, doc: dict) -> dict:
        """Insert. Frappe inserts at docstatus 0 — this NEVER submits.

        Frappe versions differ in what they accept, so we try in order:
          1. POST /api/resource/<DocType>   (the standard REST insert; the
             doctype is quoted with '+' for the space, NOT %20 — %20 404s on
             POST in some builds, which is what broke this originally)
          2. POST /api/method/frappe.client.insert   (doctype in the body)
        Whichever the site supports wins. Both land at docstatus 0.
        """
        body = {k: v for k, v in doc.items() if k != "docstatus"}
        # POST /api/resource/<DocType> with the doc as the JSON body.
        # Path segment uses quote() (space -> %20), the documented, working form.
        path = "/api/resource/" + urllib.parse.quote(doctype)
        created = self._call("POST", path, body=body)
        return self._check_draft(created)

    @staticmethod
    def _check_draft(created: dict) -> dict:
        if not isinstance(created, dict) or "name" not in created:
            raise ERPError(f"unexpected insert response: {str(created)[:200]}")
        if created.get("docstatus", 0) != 0:
            raise ERPError("insert unexpectedly produced a submitted document")
        return created

    def update_draft(self, doctype: str, name: str, patch: dict) -> dict:
        current = self.get(doctype, name)
        if current.get("docstatus") == 1:
            raise ERPError("cannot edit a submitted document")
        return self._call(
            "PUT",
            f"/api/resource/{urllib.parse.quote(doctype)}/"
            f"{urllib.parse.quote(name)}",
            body={k: v for k, v in patch.items() if k not in ("name", "docstatus")})

    def submit(self, doctype: str, name: str) -> dict:
        """docstatus 0 -> 1. Idempotent: an already-submitted doc is returned.

        Tries the REST PUT (docstatus=1), then frappe.client.submit, since
        Frappe builds differ in what they whitelist."""
        current = self.get(doctype, name)
        if current.get("docstatus") == 1:
            return current
        submitted = self._call(
            "PUT",
            f"/api/resource/{urllib.parse.quote(doctype)}/"
            f"{urllib.parse.quote(name)}",
            body={"docstatus": 1})
        if submitted.get("docstatus") != 1:
            raise ERPError(f"submit did not take effect for {doctype} {name}")
        return submitted

    def cancel(self, doctype: str, name: str) -> dict:
        """docstatus 1 -> 2 (cancelled). Only via an approved proposal."""
        cancelled = self._call(
            "PUT",
            f"/api/resource/{urllib.parse.quote(doctype)}/"
            f"{urllib.parse.quote(name)}",
            body={"docstatus": 2})
        if cancelled.get("docstatus") != 2:
            raise ERPError(f"cancel did not take effect for {doctype} {name}")
        return cancelled

    def list_attachments(self, doctype: str, name: str) -> list[dict]:
        """Files attached to a document (the paperclip items in the sidebar).
        ERPNext stores them as File records linked by attached_to_*."""
        import urllib.parse as _p
        flt = json.dumps([["attached_to_doctype", "=", doctype],
                          ["attached_to_name", "=", name]])
        fields = json.dumps(["name", "file_name", "file_url", "file_size",
                             "is_private"])
        rows = self._call("GET", "/api/resource/File",
                          params={"filters": flt, "fields": fields,
                                  "limit_page_length": 0})
        return rows if isinstance(rows, list) else []

    def fetch_file(self, file_url: str) -> bytes:
        """Download an attachment's raw bytes (works for /private/files too,
        because the request carries our auth)."""
        url = f"{self.base}{file_url}"
        headers = {}
        if self._auth:
            headers["Authorization"] = self._auth
        else:
            headers["Cookie"] = f"sid={self._sid}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()

    def get_workflow(self, doctype: str) -> dict | None:
        """Active ERPNext Workflow for a doctype (states + transitions), or None."""
        try:
            flt = json.dumps([["document_type", "=", doctype],
                              ["is_active", "=", 1]])
            rows = self._call("GET", "/api/resource/Workflow",
                              params={"filters": flt, "limit_page_length": 1})
            if not rows:
                return None
            return self._call("GET", "/api/resource/Workflow/"
                              + urllib.parse.quote(rows[0]["name"]))
        except Exception:
            return None

    def apply_workflow_action(self, doctype: str, name: str, action: str) -> dict:
        """Apply a workflow transition (the same call the Desk buttons make)."""
        doc = self.get(doctype, name)
        return self._call("POST",
                          "/api/method/frappe.model.workflow.apply_workflow",
                          body={"doc": doc, "action": action})

    def add_role(self, user_email: str, role: str) -> dict:
        """Append a role to a User's roles child table (PUT full list back)."""
        doc = self.get("User", user_email)
        roles = [r.get("role") for r in (doc.get("roles") or []) if r.get("role")]
        if role not in roles:
            roles.append(role)
        return self._call(
            "PUT", "/api/resource/User/" + urllib.parse.quote(user_email),
            body={"roles": [{"role": r} for r in roles]})

    def add_comment(self, doctype: str, name: str, text: str) -> dict:
        """Add a comment to a document's timeline (Desk's add_comment endpoint).
        Tagging works via @Username in the text — ERPNext parses mentions."""
        who = self.acting_user or "chitragupta-bot@mantratec.com"
        return self._call(
            "POST", "/api/method/frappe.desk.form.utils.add_comment",
            body={"reference_doctype": doctype, "reference_name": name,
                  "content": text, "comment_email": who, "comment_by": who})
