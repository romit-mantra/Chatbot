"""ERP adapter layer.

The engine talks ONLY to ERPAdapter (generic primitives over any DocType).

- FakeERP: faithful in-memory stand-in (Frappe semantics: drafts at docstatus 0,
  live at docstatus 1 on submit, server-assigned names). Used for dev + tests.
- FrappeAssistantAdapter: the real path via the adopted Frappe Assistant Core MCP
  server on your ERPNext site. Documented stub until a test instance + per-user
  OAuth token exist (docs/03-connect-to-erpnext.md). Swapping is a config change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import itertools


class ERPError(RuntimeError):
    pass


class PermissionDenied(ERPError):
    pass


class ERPAdapter(ABC):
    @abstractmethod
    def describe(self, doctype: str) -> dict: ...
    @abstractmethod
    def search(self, doctype: str, filters: dict | None = None) -> list[dict]: ...
    @abstractmethod
    def get(self, doctype: str, name: str) -> dict: ...
    @abstractmethod
    def create_draft(self, doctype: str, doc: dict) -> dict: ...
    @abstractmethod
    def update_draft(self, doctype: str, name: str, patch: dict) -> dict: ...
    @abstractmethod
    def submit(self, doctype: str, name: str) -> dict: ...


class FakeERP(ERPAdapter):
    def __init__(self) -> None:
        self._counters: dict[str, itertools.count] = {}
        self.files: dict[str, bytes] = {}   # file_url -> bytes (attachments)
        self.workflows: dict[str, dict] = {}  # doctype -> workflow def (tests)
        self.store: dict[str, dict[str, dict]] = {
            "Item": {
                "M6 hex screw": {"name": "M6 hex screw", "stock_qty": 4200,
                                 "reorder_level": 5000, "uom": "Nos"},
                "M8 bolt": {"name": "M8 bolt", "stock_qty": 1800,
                            "reorder_level": 2500, "uom": "Nos"},
            },
            "Supplier": {"ACME Fasteners": {"name": "ACME Fasteners"},
                         "Bharat Bolts": {"name": "Bharat Bolts"}},
            "Item Supplier": {
                "IS-1": {"name": "IS-1", "item": "M6 hex screw", "supplier": "ACME Fasteners",
                         "last_rate": 12.40, "last_po": "PUR-2026-00031", "terms": "net-30"},
                "IS-2": {"name": "IS-2", "item": "M6 hex screw", "supplier": "Bharat Bolts",
                         "last_rate": 12.90, "last_po": "PUR-2026-00028", "terms": "net-15"},
                "IS-3": {"name": "IS-3", "item": "M8 bolt", "supplier": "Bharat Bolts",
                         "last_rate": 8.10, "last_po": "PUR-2026-00026", "terms": "net-15"},
            },
            "Purchase Order": {
                "PUR-2026-00031": {"name": "PUR-2026-00031", "supplier": "ACME Fasteners",
                                   "item": "M6 hex screw", "qty": 5000, "rate": 12.40,
                                   "total": 62000.0, "docstatus": 1, "status": "To Receive"},
            },
            "Sales Order": {},
            "GL Entry": {"GL-2026-00001": {"name": "GL-2026-00001", "amount": 50000}},
        }

    def _next(self, key: str, prefix: str) -> str:
        if key not in self._counters:
            self._counters[key] = itertools.count(40)
        return f"{prefix}-{next(self._counters[key]):05d}"

    def describe(self, doctype):
        if doctype not in self.store:
            raise ERPError(f"unknown doctype {doctype}")
        sample = next(iter(self.store[doctype].values()), {})
        return {"doctype": doctype, "fields": sorted(sample.keys())}

    def search(self, doctype, filters=None):
        if doctype not in self.store:
            raise ERPError(f"unknown doctype {doctype}")
        rows = [dict(r) for r in self.store[doctype].values()]
        if filters:
            rows = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
        return rows

    def get(self, doctype, name):
        try:
            return dict(self.store[doctype][name])
        except KeyError:
            raise ERPError(f"{doctype} {name} not found")

    def create_draft(self, doctype, doc):
        name = self._next(doctype + ":d", {"Purchase Order": "PUR-DRAFT",
                                           "Sales Order": "SO-DRAFT"}.get(doctype, "DOC-DRAFT"))
        rec = {**doc, "name": name, "docstatus": 0}
        self.store.setdefault(doctype, {})[name] = rec
        return dict(rec)

    def update_draft(self, doctype, name, patch):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        if rec.get("docstatus") == 1:
            raise ERPError("cannot edit a submitted document")
        rec.update(patch)
        return dict(rec)

    def list_attachments(self, doctype, name):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        return rec.get("_attachments", [])

    def fetch_file(self, file_url):
        blob = self.files.get(file_url)
        if blob is None:
            raise ERPError(f"file not found: {file_url}")
        return blob

    def get_workflow(self, doctype):
        return self.workflows.get(doctype)

    def apply_workflow_action(self, doctype, name, action):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        wf = self.workflows.get(doctype) or {}
        tr = next((t for t in wf.get("transitions", [])
                   if t["action"] == action
                   and t["state"] == rec.get("workflow_state")), None)
        if not tr:
            raise ERPError(f"action {action!r} not allowed from state "
                           f"{rec.get('workflow_state')!r}")
        rec["workflow_state"] = tr["next_state"]
        if tr.get("next_state") in (wf.get("submit_state"),):
            rec["docstatus"] = 1
        return dict(rec)

    def cancel(self, doctype, name):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        if rec.get("docstatus") != 1:
            raise ERPError("only submitted documents can be cancelled")
        rec["docstatus"] = 2
        return dict(rec)

    def add_role(self, user_email, role):
        rec = self.store.setdefault("User", {}).setdefault(
            user_email, {"name": user_email, "roles": []})
        if not any(r.get("role") == role for r in rec.get("roles", [])):
            rec.setdefault("roles", []).append({"role": role})
        return dict(rec)

    def add_comment(self, doctype, name, text):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        rec.setdefault("_comments", []).append(text)
        return {"name": name, "comment": text}

    def submit(self, doctype, name):
        rec = self.store.get(doctype, {}).get(name)
        if not rec:
            raise ERPError(f"{doctype} {name} not found")
        if rec.get("docstatus") == 1:
            return dict(rec)  # idempotent
        final = self._next(doctype, {"Purchase Order": "PUR-2026",
                                     "Sales Order": "SO-2026"}.get(doctype, "DOC-2026"))
        del self.store[doctype][name]
        rec = {**rec, "name": final, "docstatus": 1}
        self.store[doctype][final] = rec
        return dict(rec)


class FrappeAssistantAdapter(ERPAdapter):
    """Real path against ERPNext via Frappe Assistant Core (MCP). See docs/03."""

    def __init__(self, base_url: str, token: str):
        self.base_url, self.token = base_url, token

    def _todo(self):
        raise ERPError("FrappeAssistantAdapter not wired: supply a test ERPNext + "
                       "per-user OAuth token, then implement these 6 MCP calls "
                       "(docs/03-connect-to-erpnext.md).")

    def describe(self, doctype): self._todo()
    def search(self, doctype, filters=None): self._todo()
    def get(self, doctype, name): self._todo()
    def create_draft(self, doctype, doc): self._todo()
    def update_draft(self, doctype, name, patch): self._todo()
    def submit(self, doctype, name): self._todo()
