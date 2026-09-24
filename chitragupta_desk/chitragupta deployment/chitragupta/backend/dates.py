"""Date normalisation for ERPNext.

Users type dates the Indian way ("20-07-2026" = 20 July 2026) and sometimes
run two together ("20-07-202620-08-2026"). ERPNext's REST API requires
ISO format: YYYY-MM-DD. Passing DD-MM-YYYY through causes silent misparsing
or a validation rejection.
"""

from __future__ import annotations

import datetime as dt
import re

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def split_runon(text: str) -> list[str]:
    """'20-07-202620-08-2026' -> ['20-07-2026', '20-08-2026']"""
    return re.findall(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}-\d{2}-\d{2}", text or "")


def normalise(value) -> str | None:
    """Return YYYY-MM-DD, or None if it can't be understood.

    Handles: ISO (passthrough), DD-MM-YYYY, DD/MM/YYYY, and run-together pairs
    (takes the first date). Assumes DAY-FIRST, which is correct for India.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if _ISO.match(s):
        return s
    found = split_runon(s)
    if found:
        s = found[0]
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d",
                "%d-%b-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalise_doc_dates(doc: dict, fields: list[str]) -> list[str]:
    """Normalise the named date fields in-place. Returns notes about changes."""
    notes = []
    for f in fields:
        if f in doc and doc[f]:
            fixed = normalise(doc[f])
            if fixed and fixed != str(doc[f]):
                notes.append(f"{f}: '{doc[f]}' -> {fixed}")
                doc[f] = fixed
            elif not fixed:
                notes.append(f"{f}: could not read '{doc[f]}'")
                doc.pop(f, None)
    return notes
