"""Report generation: turn ERP rows into a downloadable file.

Returns {filename, mimetype, b64} which the API passes to the widget as a
`download` field. The widget renders a Download button; the file is NOT written
to the user's disk automatically — the user chooses to download it.
"""

from __future__ import annotations

import base64
import csv
import io


# System/metadata fields no human wants in a report unless they ask
SYSTEM_COLS = {"owner", "creation", "modified", "modified_by", "docstatus",
               "idx", "naming_series", "_user_tags", "_comments", "_assign",
               "_liked_by", "parent", "parentfield", "parenttype", "doctype",
               "amended_from", "letter_head", "print_heading"}

BOOL_COLS = {"enabled", "disabled", "is_active", "is_default", "is_group"}


def _fmt_cell(col, v):
    """Human formatting: dates without microseconds, Yes/No booleans."""
    if v is None:
        return ""
    s = str(v)
    if col in BOOL_COLS and s in ("0", "1", "True", "False"):
        return "Yes" if s in ("1", "True") else "No"
    import re
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}", s)
    if m:
        return m.group(1)
    return s


def _columns(rows):
    cols, seen = [], set()
    for r in rows:
        for k in r:
            if (not k.startswith("_") and k not in seen
                    and k not in SYSTEM_COLS):
                seen.add(k); cols.append(k)
    front = [c for c in ("name", "full_name", "supplier", "customer",
                         "grand_total", "status", "transaction_date",
                         "roles", "enabled") if c in seen]
    rest = [c for c in cols if c not in front]
    return front + rest


def build_report(rows, fmt, title, doctype, columns=None):
    """columns: model-chosen field list (Claude-style judgment); falls back to
    heuristics when absent. Unknown fields are ignored."""
    if columns:
        have = set().union(*[set(r) for r in rows]) if rows else set()
        cols = [c for c in columns if c in have] or _columns(rows) or ["name"]
    else:
        cols = _columns(rows) or ["name"]
    fmt = (fmt or "excel").lower()

    if fmt in ("excel", "xlsx"):
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = doctype[:31]
        ws.append([c.replace("_", " ").title() for c in cols])
        for cell in ws[1]:
            cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
            cell.fill = openpyxl.styles.PatternFill("solid", fgColor="0E8C7F")
        for r in rows:
            ws.append([_fmt_cell(c, r.get(c, "")) for c in cols])
        for i, col in enumerate(cols, 1):
            width = max([len(str(col))] +
                        [len(str(_fmt_cell(col, r.get(col, ""))))
                         for r in rows[:50]]) + 2
            ws.column_dimensions[
                openpyxl.utils.get_column_letter(i)].width = min(width, 45)
        ws.freeze_panes = "A2"
        buf = io.BytesIO(); wb.save(buf)
        return {"filename": f"{title}.xlsx",
                "mimetype": "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet",
                "b64": base64.b64encode(buf.getvalue()).decode()}

    if fmt == "csv":
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt_cell(c, r.get(c, "")) for c in cols])
        return {"filename": f"{title}.csv", "mimetype": "text/csv",
                "b64": base64.b64encode(out.getvalue().encode()).decode()}

    if fmt == "pdf":
        # lightweight PDF table via reportlab if available
        try:
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                            Paragraph, Spacer)
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet
        except ImportError:
            raise RuntimeError("PDF needs reportlab (pip install reportlab); "
                               "try excel or csv instead")
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
        styles = getSampleStyleSheet()
        show = cols[:8]
        body = styles["BodyText"]; body.fontSize = 7; body.leading = 8.5
        data = ([[Paragraph(c.replace("_", " ").title(), body) for c in show]]
                + [[Paragraph(_fmt_cell(c, r.get(c, "")), body) for c in show]
                   for r in rows])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e8c7f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f6f8fa")]),
        ]))
        import datetime as _dt
        sub = Paragraph(
            f"{len(rows)} records \u00b7 generated by Chitragupta \u00b7 "
            f"{_dt.date.today().isoformat()}", styles["Normal"])
        doc.build([Paragraph(title, styles["Title"]), sub, Spacer(1, 8), t])
        return {"filename": f"{title}.pdf", "mimetype": "application/pdf",
                "b64": base64.b64encode(buf.getvalue()).decode()}

    raise RuntimeError(f"unsupported format: {fmt}")
