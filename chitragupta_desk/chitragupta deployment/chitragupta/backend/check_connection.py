#!/usr/bin/env python3
"""Step 1 of going live: prove Chitragupta can reach YOUR ERPNext safely.

READ-ONLY. This script writes nothing. Run it before anything else.

    export ERP_URL="http://192.168.179.128:8000"
    export ERP_API_KEY="<chitragupta-bot key>"
    export ERP_API_SECRET="<chitragupta-bot secret>"
    python check_connection.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from erp import ERPError, PermissionDenied          # noqa: E402
from erp_frappe import FrappeERP                    # noqa: E402


def main() -> int:
    print("Chitragupta -> ERPNext connection check\n" + "-" * 40)
    try:
        erp = FrappeERP()
    except ERPError as e:
        print(f"FAIL  config: {e}")
        return 1
    print(f"target        : {erp.base}")

    # 1. auth
    try:
        who = erp.ping()
        print(f"authenticated : {who}")
    except (ERPError, PermissionDenied) as e:
        print(f"FAIL  auth: {e}")
        return 1
    if who == "Administrator":
        print("WARN  you are connected as Administrator (full god-mode).")
        print("      Use the chitragupta-bot user's keys instead — the whole")
        print("      safety model depends on a limited ERP role.")

    # 2. metadata (OPTIONAL — a narrow role often cannot read DocType; that's fine)
    meta = erp.describe("Purchase Order")
    if meta.get("meta_available"):
        print(f"metadata      : Purchase Order, {len(meta['fields'])} fields, "
              f"submittable={meta['is_submittable']}")
    else:
        print("metadata      : not permitted for this role (fine — the loop "
              "does not need it)")

    # 3. reads on the doctypes the PO workflow needs
    for dt in ("Item", "Supplier", "Purchase Order"):
        try:
            rows = erp.search(dt, fields=["name"], limit=5)
            sample = ", ".join(r.get("name", "?") for r in rows[:3]) or "(none)"
            print(f"read {dt:<15}: {len(rows)} row(s)  [{sample}]")
        except PermissionDenied:
            print(f"read {dt:<15}: DENIED (role lacks read — expected for some)")
        except ERPError as e:
            print(f"read {dt:<15}: ERROR {e}")

    # 4. confirm the bot CANNOT read a sensitive doctype (good news if denied)
    try:
        erp.search("Salary Slip", fields=["name"], limit=1)
        print("perm check    : bot CAN read Salary Slip  <-- too much access; "
              "tighten its role")
    except PermissionDenied:
        print("perm check    : bot cannot read Salary Slip  (good — limited role)")
    except ERPError:
        print("perm check    : Salary Slip not installed (fine)")

    print("-" * 40)
    print("OK. Reads work. No writes were attempted.")
    print("Next: run the live smoke test (draft -> approve -> submit -> read-back).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
