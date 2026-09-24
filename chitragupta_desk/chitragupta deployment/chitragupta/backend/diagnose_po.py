#!/usr/bin/env python3
"""Diagnose Purchase Order creation against the LIVE ERP.

Prints the RAW response for several payload shapes so we can see exactly what
ERPNext objects to. Creates DRAFTS only (docstatus 0) — never submits.

    (set ERP_URL / ERP_API_KEY / ERP_API_SECRET first)
    python diagnose_po.py
"""
from __future__ import annotations
import datetime as dt, json, os, urllib.error, urllib.parse, urllib.request

BASE = os.environ.get("ERP_URL", "").rstrip("/")
KEY = os.environ.get("ERP_API_KEY", "")
SEC = os.environ.get("ERP_API_SECRET", "")
AUTH = f"token {KEY}:{SEC}"


def call(method, path, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": AUTH, "Accept": "application/json",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)


def human(raw):
    try:
        d = json.loads(raw)
    except Exception:
        return raw[:300]
    out = []
    sm = d.get("_server_messages")
    if sm:
        try:
            for entry in json.loads(sm):
                try:
                    out.append(json.loads(entry).get("message", ""))
                except Exception:
                    out.append(str(entry))
        except Exception:
            out.append(str(sm))
    if d.get("exception"):
        out.append(str(d["exception"])[:200])
    return " | ".join(x for x in out if x) or raw[:300]


def show(label, status, raw):
    print(f"\n--- {label} ---")
    print(f"HTTP {status}")
    try:
        d = json.loads(raw)
        if "data" in d and isinstance(d["data"], dict):
            print(f"  CREATED: {d['data'].get('name')} "
                  f"docstatus={d['data'].get('docstatus')}")
            return
    except Exception:
        pass
    print(f"  ERPNext says: {human(raw)}")


def main():
    if not (BASE and KEY and SEC):
        print("Set ERP_URL / ERP_API_KEY / ERP_API_SECRET first.")
        return 1
    print(f"target: {BASE}")
    _, who = call("GET", "/api/method/frappe.auth.get_logged_user")
    print("auth  :", who[:120])

    _, sup = call("GET", "/api/resource/Supplier?limit_page_length=1")
    _, itm = call("GET", "/api/resource/Item?limit_page_length=1")
    _, comp = call("GET", "/api/resource/Company?limit_page_length=1")
    supplier = json.loads(sup)["data"][0]["name"]
    item = json.loads(itm)["data"][0]["name"]
    company = json.loads(comp)["data"][0]["name"]
    print(f"using supplier={supplier!r} item={item!r} company={company!r}")

    today = dt.date.today().isoformat()
    soon = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    quoted = "/api/resource/" + urllib.parse.quote("Purchase Order")

    good = {"supplier": supplier, "company": company,
            "transaction_date": today, "schedule_date": soon,
            "items": [{"item_code": item, "qty": 5, "rate": 100,
                       "schedule_date": soon}]}
    show("A1: full body, ISO dates, item has schedule_date (EXPECTED TO WORK)",
         *call("POST", quoted, good))

    show("A2: no dates anywhere (expect: 'Please enter Reqd by Date')",
         *call("POST", quoted, {"supplier": supplier, "company": company,
                                "items": [{"item_code": item, "qty": 5,
                                           "rate": 100}]}))

    ddmm = dict(good)
    ddmm["transaction_date"] = "20-07-2026"
    ddmm["schedule_date"] = "20-08-2026"
    ddmm["items"] = [{"item_code": item, "qty": 5, "rate": 100,
                      "schedule_date": "20-08-2026"}]
    show("A3: DD-MM-YYYY dates (what the user types — does ERPNext accept?)",
         *call("POST", quoted, ddmm))

    show("A4: dates only at top level, NOT on the item row",
         *call("POST", quoted, {"supplier": supplier, "company": company,
                                "transaction_date": today, "schedule_date": soon,
                                "items": [{"item_code": item, "qty": 5,
                                           "rate": 100}]}))

    print("\nDone. Any draft created above can be deleted from the PO list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
