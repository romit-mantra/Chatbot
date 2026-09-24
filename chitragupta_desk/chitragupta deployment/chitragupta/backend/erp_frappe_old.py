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


class FrappeERP(ERPAdapter):
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 api_secret: str | None = None, timeout: int = 30):
        self.base = (base_url or os.environ.get("ERP_URL", "")).rstrip("/")
        key = api_key or os.environ.get("ERP_API_KEY", "")
        secret = api_secret or os.environ.get("ERP_API_SECRET", "")
        if not self.base:
            raise ERPError("ERP_URL not set")
        if not (key and secret):
            raise ERPError("ERP_API_KEY / ERP_API_SECRET not set")
        self._auth = f"token {key}:{secret}"
        self.timeout = timeout

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
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code in (401, 403):
                raise PermissionDenied(f"{e.code} on {method} {path}: {detail}")
            if e.code == 404:
                raise ERPError(f"not found: {method} {path}")
            raise ERPError(f"{e.code} on {method} {path}: {detail}")
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
