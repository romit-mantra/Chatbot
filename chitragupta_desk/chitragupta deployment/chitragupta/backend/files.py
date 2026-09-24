"""File attachment handling: turn an uploaded file into text the model can read.

Supports CSV/TSV, Excel (xlsx/xls), PDF, plain text. Images are passed through
as a note (vision support is a later step).

Deliberately conservative: we extract a TEXT EXCERPT and hand it to the planner
as context. The model may then propose a create/query as usual — which still
goes through RBAC and the governance gate. A file can never bypass approval.
"""

from __future__ import annotations

import base64
import csv
import io

MAX_CHARS = 20000


def extract_bytes(name: str, raw: bytes) -> tuple[str, str]:
    """Extract text from raw bytes by filename extension."""
    return extract({"name": name,
                    "data": base64.b64encode(raw).decode()})


def extract(file: dict) -> tuple[str, str]:
    """-> (text_excerpt, kind). Raises on unreadable input."""
    name = (file.get("name") or "").lower()
    raw = base64.b64decode(file.get("data") or "")

    if name.endswith((".csv", ".tsv", ".txt")):
        text = raw.decode("utf-8", errors="replace")
        return text[:MAX_CHARS], "text"

    if name.endswith((".xlsx", ".xls")):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("openpyxl not installed (pip install openpyxl)")
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        out = io.StringIO()
        w = csv.writer(out)
        for ws in wb.worksheets:
            out.write(f"# sheet: {ws.title}\n")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    w.writerow(["" if c is None else c for c in row])
            if out.tell() > MAX_CHARS:
                break
        return out.getvalue()[:MAX_CHARS], "spreadsheet"

    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("pypdf not installed (pip install pypdf)")
        reader = PdfReader(io.BytesIO(raw))
        chunks = []
        for page in reader.pages[:20]:
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) > MAX_CHARS:
                break
        text = "\n".join(chunks).strip()
        if not text:
            raise RuntimeError("this PDF has no extractable text (scanned image?)")
        return text[:MAX_CHARS], "pdf"

    if name.endswith((".png", ".jpg", ".jpeg")):
        raise RuntimeError("image reading isn't supported yet — please upload a "
                           "PDF, Excel or CSV")

    raise RuntimeError(f"unsupported file type: {name}")
