"""Governance: the risk-tier gate + per-user role permissions.

Two independent layers, both enforced in the engine (never only in a prompt):
1) RBAC backstop — can THIS USER touch THIS DOCTYPE with THIS ACTION at all?
   (In production this is ERPNext's own permission engine via act-as-user OAuth;
   here a faithful role->doctype permission map plays that part.)
2) Risk tier — even if permitted, is the action auto / needs-approval / blocked?
"""

from __future__ import annotations
from enum import Enum


class Tier(str, Enum):
    AUTO = "auto"
    APPROVE = "approve"
    BLOCK = "block"


FINANCIAL_DOCTYPES = {"Payment Entry", "Journal Entry", "Sales Invoice",
                      "Purchase Invoice", "Expense Claim", "Payment Request"}

FORBIDDEN = {("GL Entry", "delete"), ("GL Entry", "cancel"),
             ("Account", "delete"), ("Accounting Period", "update")}

HARD_VALUE_CAP = 1_000_000.0


def classify(doctype: str, action: str, value: float | None = None) -> tuple[Tier, str]:
    action = action.lower()
    if (doctype, action) in FORBIDDEN:
        return Tier.BLOCK, f"{action} on {doctype} is forbidden (ledger integrity)"
    if action in {"get", "list", "search", "describe", "report"}:
        return Tier.AUTO, "read-only"
    if action == "create_draft":
        return Tier.AUTO, "creating an unsubmitted draft is reversible"
    if action == "delete":
        return Tier.BLOCK, "deletes are blocked by default"
    if value is not None and value > HARD_VALUE_CAP:
        return Tier.BLOCK, f"value {value} exceeds hard cap {HARD_VALUE_CAP}"
    if doctype in FINANCIAL_DOCTYPES and action in {"submit", "cancel"}:
        return Tier.APPROVE, f"financial {action} on {doctype} always needs approval"
    if action in {"submit", "update", "cancel"}:
        return Tier.APPROVE, f"{action} is a state-changing write"
    return Tier.BLOCK, f"unrecognized action '{action}'"


# ---------------- RBAC (stand-in for ERPNext's permission engine) --------------

# NOTE ON READS
# Earlier this map also gated READS. That was wrong: it duplicated ERPNext's own
# permission engine and got it wrong (a Purchase User WAS allowed to read
# Company, but this map said no, so the assistant refused a legitimate request).
# ERPNext is the source of truth for permissions — it returns 403 if a user may
# not read something. We now let reads through to the ERP and surface its answer.
# This map governs WRITES only (create/submit), as a defence-in-depth layer on
# top of ERPNext's own check.

ROLE_PERMS: dict[str, dict[str, set[str]]] = {
    # role -> doctype -> allowed actions ("*" doctype = fallback)
    "Purchase User": {
        "*": {"read"},                       # reads: ERPNext decides
        "Item": {"read"}, "Supplier": {"read"}, "Item Supplier": {"read"},
        "Purchase Order": {"read", "write", "submit"},
    },
    "Sales User": {
        "*": {"read"},                       # reads: ERPNext decides
        "Item": {"read"}, "Sales Order": {"read", "write", "submit"},
        "Purchase Order": {"read"},
    },
    "Accounts Manager": {
        "*": {"read"}, "Payment Entry": {"read", "write", "submit"},
        "Journal Entry": {"read", "write", "submit"},
        "Purchase Order": {"read", "submit"},
    },
    "System Manager": {"*": {"read", "write", "submit"}},
}

USERS: dict[str, dict] = {
    "ravi":   {"full_name": "Ravi (Purchase)", "roles": ["Purchase User"]},
    "meera":  {"full_name": "Meera (Sales)", "roles": ["Sales User"]},
    "priya":  {"full_name": "Priya (Accounts Mgr)", "roles": ["Accounts Manager"]},
    "admin":  {"full_name": "Admin", "roles": ["System Manager"]},
}

_ACTION_MAP = {"get": "read", "list": "read", "search": "read", "describe": "read",
               "report": "read", "create_draft": "write", "update": "write",
               "submit": "submit", "cancel": "submit", "delete": "write"}


def user_can(user: str, doctype: str, action: str) -> bool:
    """Defence-in-depth check. ERPNext remains the authority.

    READS: always allowed through this layer — ERPNext will 403 if the user
    genuinely may not read that doctype, and we surface that. (Previously this
    map refused reads ERPNext would have allowed, e.g. Company for a Purchase
    User, which made the assistant refuse legitimate questions.)

    WRITES: still gated here, so a compromised or confused model cannot even
    attempt a write outside the user's role.
    """
    info = USERS.get(user)
    if not info:
        return False
    need = _ACTION_MAP.get(action.lower(), action.lower())
    if need == "read":
        return True                      # ERPNext is the authority on reads
    for role in info["roles"]:
        perms = ROLE_PERMS.get(role, {})
        allowed = perms.get(doctype, perms.get("*", set()))
        if need in allowed:
            return True
    return False


def register_user(user: str, full_name: str, roles: list[str]) -> None:
    """Provision a user discovered via ERP SSO.

    Roles are the user's REAL ERPNext roles (e.g. "Purchase User",
    "Accounts Manager"), which map directly onto ROLE_PERMS where known.
    Unknown roles simply grant nothing at the write-gate (ERPNext still
    enforces its own permissions on every call)."""
    USERS[user] = {"full_name": full_name or user, "roles": roles,
                   "provisioned": True}


def user_roles(user: str) -> list[str]:
    return USERS.get(user, {}).get("roles", [])


def can_see_memory(user: str, doctype: str) -> bool:
    """Strict scope check for ORG MEMORY recall.

    Deliberately separate from user_can(): reads against the live ERP are
    delegated to ERPNext, but remembered org facts have no ERPNext permission
    behind them — so we must scope them ourselves, or something learned while
    helping Accounts could leak to a Sales clerk.
    """
    info = USERS.get(user)
    if not info:
        return False
    for role in info["roles"]:
        perms = ROLE_PERMS.get(role, {})
        if doctype in perms:            # the role explicitly names this doctype
            return True
        if "*" in perms and "write" in perms["*"]:   # system-wide roles
            return True
    return False
