import hashlib
import hmac
import time

import frappe


@frappe.whitelist()
def get_session_assertion():
    """Runs AS the logged-in ERP user, so the identity is authoritative.
    Returns a short-lived HMAC-signed assertion the backend can verify."""
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("Not logged in")
    secret = frappe.conf.get("chitragupta_sso_secret")
    if not secret:
        frappe.throw("chitragupta_sso_secret not configured on this site")
    ts = str(time.time())
    sig = hmac.new(secret.encode(), f"{user}|{ts}".encode(),
                   hashlib.sha256).hexdigest()
    return {"user": user, "ts": ts, "sig": sig}
