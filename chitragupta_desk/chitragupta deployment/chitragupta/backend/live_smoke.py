#!/usr/bin/env python3
"""Step 2 of going live: the full Chitragupta loop against YOUR ERPNext.

This DOES write — but only a DRAFT, and it will not submit anything without
you typing 'yes' at the prompt. Run check_connection.py first.

    export ERP_URL="http://192.168.179.128:8000"
    export ERP_API_KEY="..." ERP_API_SECRET="..."
    python live_smoke.py            # drafts only, prompts before submit
    python live_smoke.py --no-submit  # never submits, just drafts + cleans up

What it proves, end to end, on real data:
  read -> draft (docstatus 0) -> governance gate -> human confirm ->
  submit (docstatus 1) -> read-back verification -> audit trail
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from erp import ERPError                       # noqa: E402
from erp_frappe import FrappeERP               # noqa: E402
from governance import classify, Tier          # noqa: E402
from store import make_session, AuditLog, audit  # noqa: E402

NO_SUBMIT = "--no-submit" in sys.argv


def pick(erp, doctype, label):
    rows = erp.search(doctype, fields=["name"], limit=5)
    if not rows:
        print(f"FAIL  no {doctype} records exist. Create one in ERPNext first "
              f"(you need at least one Item and one Supplier).")
        raise SystemExit(1)
    name = rows[0]["name"]
    print(f"{label:<14}: {name}")
    return name


def main() -> int:
    erp = FrappeERP()
    Session = make_session("sqlite:///live_smoke.db")
    s = Session()
    print("Chitragupta LIVE loop test\n" + "-" * 46)
    print(f"target        : {erp.base}")
    print(f"as user       : {erp.ping()}\n")

    # --- 1. GATHER (reads) ---
    item = pick(erp, "Item", "item")
    supplier = pick(erp, "Supplier", "supplier")
    company = erp.search("Company", fields=["name"], limit=1)
    if not company:
        print("FAIL  no Company found.")
        return 1
    company = company[0]["name"]
    print(f"{'company':<14}: {company}")
    item_doc = erp.get("Item", item)
    uom = item_doc.get("stock_uom") or "Nos"

    # --- 2. DRAFT (auto tier — reversible) ---
    tier, reason = classify("Purchase Order", "create_draft")
    print(f"\ngate(draft)   : {tier.value} ({reason})")
    import datetime as dt
    sched = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    po = {
        "supplier": supplier,
        "company": company,
        "transaction_date": dt.date.today().isoformat(),
        "schedule_date": sched,
        "items": [{"item_code": item, "qty": 5, "rate": 100,
                   "schedule_date": sched, "uom": uom}],
    }
    try:
        draft = erp.create_draft("Purchase Order", po)
    except ERPError as e:
        print(f"FAIL  draft: {e}")
        return 1
    name = draft["name"]
    total = draft.get("grand_total")
    print(f"draft created : {name}  docstatus={draft['docstatus']}  "
          f"total={total}")
    audit(s, "proposal_staged", "chitragupta-bot", doctype="Purchase Order",
          name=name, value=total)
    assert draft["docstatus"] == 0, "draft must not be submitted"

    # --- 3. GATE (submit) ---
    tier, reason = classify("Purchase Order", "submit", float(total or 0))
    print(f"gate(submit)  : {tier.value.upper()} ({reason})")
    if tier is Tier.BLOCK:
        print("blocked — nothing further. (This is the gate doing its job.)")
        return 0

    # --- 4. HUMAN CONFIRM ---
    if NO_SUBMIT:
        print("\n--no-submit set: leaving the draft unsubmitted. "
              f"Inspect it at {erp.base}/app/purchase-order/{name}")
        return 0
    print("\nA human must approve before anything is submitted.")
    print(f"  Review: {erp.base}/app/purchase-order/{name}")
    if input("  Approve and submit this PO? (yes/no): ").strip().lower() != "yes":
        print("Rejected — draft left unsubmitted. Nothing was committed.")
        audit(s, "rejected", "you", name=name)
        return 0
    audit(s, "approved", "you", name=name)

    # --- 5. COMMIT + READ-BACK ---
    submitted = erp.submit("Purchase Order", name)
    print(f"submitted     : {submitted['name']}  docstatus="
          f"{submitted['docstatus']}")
    readback = erp.get("Purchase Order", submitted["name"])
    ok = (readback["docstatus"] == 1
          and readback["supplier"] == supplier
          and abs(float(readback.get("grand_total") or 0) - float(total or 0)) < 0.01)
    print(f"read-back     : {'VERIFIED' if ok else 'MISMATCH'}")
    audit(s, "committed", "you", name=submitted["name"], readback_ok=ok)

    # --- 6. AUDIT ---
    print("\naudit trail   :", [a.event for a in s.query(AuditLog).all()])
    print("-" * 46)
    print("LOOP COMPLETE on real ERPNext data." if ok else "READ-BACK FAILED.")
    print(f"See it: {erp.base}/app/purchase-order/{submitted['name']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
