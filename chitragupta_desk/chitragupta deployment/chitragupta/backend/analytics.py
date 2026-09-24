"""Exact server-side aggregation.

Previously up to 200 raw rows went to the model, which did the arithmetic —
wrong beyond the cap and expensive. Now Python computes exact stats over ALL
rows; the model receives verified numbers + a small sample and only narrates.
"""

from __future__ import annotations
from collections import defaultdict

NUMERIC_HINTS = ("grand_total", "total", "amount", "qty", "rate", "outstanding")


def numeric_fields(rows):
    if not rows:
        return []
    found = []
    for k in rows[0]:
        if any(h in k for h in NUMERIC_HINTS):
            try:
                float(rows[0].get(k) or 0)
                found.append(k)
            except (TypeError, ValueError):
                pass
    return found


def group_fields(rows):
    cands = ("supplier", "supplier_name", "customer", "customer_name",
             "status", "company", "item_group", "territory")
    return [c for c in cands if rows and c in rows[0]]


def compute(rows):
    """-> exact stats dict over the FULL row set."""
    stats = {"row_count": len(rows)}
    nums = numeric_fields(rows)
    for f in nums:
        vals = []
        for r in rows:
            try:
                vals.append(float(r.get(f) or 0))
            except (TypeError, ValueError):
                pass
        if vals:
            stats[f] = {"sum": round(sum(vals), 2),
                        "avg": round(sum(vals) / len(vals), 2),
                        "min": min(vals), "max": max(vals)}
    primary = next((f for f in ("grand_total", "total", "amount") if f in nums),
                   nums[0] if nums else None)
    for g in group_fields(rows)[:3]:
        agg = defaultdict(lambda: [0, 0.0])
        for r in rows:
            key = r.get(g) or "(blank)"
            agg[key][0] += 1
            if primary:
                try:
                    agg[key][1] += float(r.get(primary) or 0)
                except (TypeError, ValueError):
                    pass
        top = sorted(agg.items(), key=lambda kv: -kv[1][1])[:12]
        stats[f"by_{g}"] = {k: {"count": c, ("sum_" + primary) if primary else "count2":
                                round(t, 2) if primary else c}
                            for k, (c, t) in top}
    return stats
