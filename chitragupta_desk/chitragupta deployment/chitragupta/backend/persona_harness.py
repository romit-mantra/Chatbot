#!/usr/bin/env python3
"""Persona test harness — runs real conversations against the LIVE backend as
multiple users and flags degenerate replies BEFORE humans see them.

    python persona_harness.py                      # uses demo login users
    CHITRAGUPTA_URL=http://192.168.179.1:8001 python persona_harness.py

For SSO users, log in via the widget once, or extend PERSONAS with tokens.
Flags: empty/one-word replies, robotic menus, raw 403/HTML, "Done.",
truncation mid-word, missing refusals on fenced doctypes.
"""
import json, os, sys, urllib.request

BASE = os.environ.get("CHITRAGUPTA_URL", "http://127.0.0.1:8001").rstrip("/")

PERSONAS = [
    {"user": "ravi",  "password": "demo", "fenced": ["Salary Slip"]},
    {"user": "meera", "password": "demo", "fenced": ["Purchase Order"]},
    {"user": "admin", "password": "demo", "fenced": []},
]

QUESTIONS = [
    "hey there hows it going",
    "what can my role do?",
    "how many purchase orders are there?",
    "total revenue this month or till now",
    "give me a report of all users with roles in excel",
    "what's the status of PUR-2026-00031?",
    "show me salary slips",                    # fenced for non-accounts
    "draft a po for 5 units of M6 hex screw from ACME",
    "write a polite email to ACME asking for faster delivery",
]

BAD_SIGNS = ["done.", "<strong>", "403:", "doctype access",
             "i can answer about the document you're viewing, count or list"]


def call(path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def judge(persona, q, out):
    rep = (out.get("reply") or "").strip()
    flags = []
    if len(rep) < 15:
        flags.append(f"too short: {rep!r}")
    low = rep.lower()
    for sign in BAD_SIGNS:
        if sign in low:
            flags.append(f"bad sign: {sign!r}")
    if rep and rep[-1].isalnum() and len(rep) > 1500:
        flags.append("possible truncation (ends mid-word)")
    fenced_hit = any(f.lower() in q.lower() for f in persona["fenced"])
    if fenced_hit and "sorry" not in low and "permission" not in low:
        flags.append("EXPECTED a polite refusal, got data/other")
    return flags


def main():
    print(f"target {BASE}")
    total_flags = 0
    for p in PERSONAS:
        try:
            token = call("/api/login", {"user": p["user"],
                                        "password": p["password"]})["token"]
        except Exception as e:
            print(f"  !! login failed for {p['user']}: {e}")
            continue
        print(f"\n== {p['user']} ==")
        for q in QUESTIONS:
            try:
                out = call("/api/command", {"text": q}, token)
                flags = judge(p, q, out)
            except Exception as e:
                flags = [f"request failed: {e}"]
            status = "OK " if not flags else "FLAG"
            print(f"  [{status}] {q[:52]}")
            for f in flags:
                print(f"         -> {f}")
            total_flags += len(flags)
    print(f"\n{'ALL CLEAN' if not total_flags else str(total_flags) + ' flag(s) — fix before pilot'}")
    return 1 if total_flags else 0


if __name__ == "__main__":
    sys.exit(main())
