"""``document_read`` / ``document_info`` — let an agent open the files real work
arrives in: PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), CSV/TSV, and
plain text. Pure local extraction: ``pypdf`` for PDF, the stdlib ``zipfile`` +
``xml`` readers for Office formats (no LibreOffice, no network).

Scope: a path must live under one of the allowed ``roots`` — the agent's
working folder and the chat-channel media folder (where WhatsApp/Telegram
attachments are saved). Output is capped (``max_chars``) with an explicit
truncation note; ``pages`` / ``sheet`` / ``offset`` let the agent page through
large documents instead of flooding the context.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

DEFAULT_MAX_CHARS = 12000
HARD_MAX_CHARS = 60000
MAX_ROWS_PER_SHEET = 500
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".log", ".yaml", ".yml", ".toml", ".ini", ".html", ".htm", ".xml"}
SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".tsv", *TEXT_EXT}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocError(Exception):
    pass


# --------------------------------------------------------------------------- #
# path scoping
# --------------------------------------------------------------------------- #

def resolve_in_roots(path: str, roots: list[Path]) -> Path:
    """Resolve ``path`` (relative to the first root, or absolute) and refuse
    anything outside every allowed root."""
    rel = (path or "").strip()
    if not rel:
        raise DocError("path is required")
    p = Path(rel).expanduser()
    candidates = [p] if p.is_absolute() else [r / p for r in roots]
    for cand in candidates:
        resolved = cand.resolve(strict=False)
        for root in roots:
            rr = root.resolve(strict=False)
            if resolved == rr or rr in resolved.parents:
                if not resolved.exists():
                    continue
                return resolved
    raise DocError(f"{rel!r} is not inside an allowed folder (or does not exist)")


# --------------------------------------------------------------------------- #
# extractors
# --------------------------------------------------------------------------- #

def _parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-3,7' → zero-based page indexes (1-based in the spec). Empty → all."""
    if not spec or not spec.strip():
        return list(range(n))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo = max(1, int(a or 1)); hi = min(n, int(b or n))
            out.extend(range(lo - 1, hi))
        else:
            i = int(part)
            if 1 <= i <= n:
                out.append(i - 1)
    return sorted(set(out))


def extract_pdf(path: Path, pages: str | None = None) -> tuple[str, dict]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    n = len(reader.pages)
    idx = _parse_pages(pages, n)
    parts = []
    for i in idx:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = f"[page {i + 1}: extraction failed: {exc}]"
        parts.append(f"--- page {i + 1} ---\n{text.strip()}")
    meta = {"pages": n, "pagesRead": [i + 1 for i in idx]}
    try:
        info = reader.metadata or {}
        if info.get("/Title"):
            meta["title"] = str(info.get("/Title"))
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts), meta


def _docx_para_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{_W}t"))


def extract_docx(path: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    lines: list[str] = []
    paras = tables = 0
    for el in (body if body is not None else root):
        tag = el.tag
        if tag == f"{_W}p":
            t = _docx_para_text(el)
            style = el.find(f"{_W}pPr/{_W}pStyle")
            sv = (style.get(f"{_W}val") if style is not None else "") or ""
            if t.strip():
                paras += 1
                if sv.lower().startswith("heading"):
                    lvl = re.sub(r"\D", "", sv) or "1"
                    t = "#" * min(6, int(lvl)) + " " + t
            lines.append(t)
        elif tag == f"{_W}tbl":
            tables += 1
            for tr in el.iter(f"{_W}tr"):
                cells = ["".join(_docx_para_text(p) for p in tc.iter(f"{_W}p")).strip() for tc in tr.findall(f"{_W}tc")]
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines).strip(), {"paragraphs": paras, "tables": tables}


def _col_index(ref: str) -> int:
    col = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in (col.group(0) if col else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n


def _xlsx_sheets(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_map = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_PR}Relationship")}
    out = []
    for sh in wb.iter(f"{_S}sheet"):
        target = rel_map.get(sh.get(f"{_R}id"), "")
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        out.append((sh.get("name") or "Sheet", target))
    return out


def _xlsx_shared(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{_S}t")) for si in root.iter(f"{_S}si")]


def extract_xlsx(path: Path, sheet: str | None = None, max_rows: int = MAX_ROWS_PER_SHEET) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as z:
        sheets = _xlsx_sheets(z)
        shared = _xlsx_shared(z)
        chosen = [s for s in sheets if not sheet or s[0] == sheet or str(sheets.index(s) + 1) == str(sheet)]
        if not chosen:
            raise DocError(f"no sheet named {sheet!r}; sheets: {', '.join(n for n, _ in sheets)}")
        out: list[str] = []
        meta: dict = {"sheets": [n for n, _ in sheets], "rows": {}}
        for name, target in chosen:
            root = ET.fromstring(z.read(target))
            out.append(f"=== sheet: {name} ===")
            rows_done = 0
            total = 0
            for row in root.iter(f"{_S}row"):
                total += 1
                if rows_done >= max_rows:
                    continue
                cells: dict[int, str] = {}
                for c in row.findall(f"{_S}c"):
                    ci = _col_index(c.get("r") or "")
                    t = c.get("t")
                    v = c.find(f"{_S}v")
                    if t == "s" and v is not None:
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = v.text or ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{_S}t"))
                    else:
                        val = (v.text or "") if v is not None else ""
                    cells[ci] = val
                if cells:
                    width = max(cells)
                    out.append(",".join(_csv_escape(cells.get(i, "")) for i in range(1, width + 1)))
                rows_done += 1
            meta["rows"][name] = total
            if total > max_rows:
                out.append(f"[… {total - max_rows} more rows not shown — use max_rows or sheet to narrow]")
    return "\n".join(out), meta


def _csv_escape(v: str) -> str:
    return f'"{v.replace(chr(34), chr(34) * 2)}"' if any(ch in v for ch in ',"\n') else v


def extract_pptx(path: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        parts = []
        for i, n in enumerate(names, 1):
            root = ET.fromstring(z.read(n))
            texts = []
            for p in root.iter(f"{_A}p"):
                t = "".join(r.text or "" for r in p.iter(f"{_A}t")).strip()
                if t:
                    texts.append(t)
            parts.append(f"--- slide {i} ---\n" + "\n".join(texts))
    return "\n\n".join(parts), {"slides": len(names)}


def extract_csv(path: Path, max_rows: int = MAX_ROWS_PER_SHEET) -> tuple[str, dict]:
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    shown = rows[:max_rows]
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="\n").writerows(shown)
    out = buf.getvalue()
    if len(rows) > max_rows:
        out += f"[… {len(rows) - max_rows} more rows not shown]"
    return out, {"rows": len(rows), "columns": len(rows[0]) if rows else 0}


def extract_text(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text, {"lines": text.count("\n") + 1}


def extract(path: Path, *, pages: str | None = None, sheet: str | None = None,
            max_rows: int = MAX_ROWS_PER_SHEET) -> tuple[str, dict]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path, pages)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".xlsx":
        return extract_xlsx(path, sheet, max_rows)
    if ext == ".pptx":
        return extract_pptx(path)
    if ext in (".csv", ".tsv"):
        return extract_csv(path, max_rows)
    if ext in TEXT_EXT:
        return extract_text(path)
    raise DocError(f"unsupported file type {ext or '(none)'}; supported: {', '.join(sorted(SUPPORTED))}")


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

DOC_TOOL_NAMES = ["document_read", "document_info"]


def make_doc_tools(roots: list[str | Path]) -> dict[str, dict]:
    rts = [Path(r).expanduser() for r in roots if str(r).strip()]

    async def document_read(path: str = "", pages: str = "", sheet: str = "",
                            offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS,
                            max_rows: int = MAX_ROWS_PER_SHEET) -> str:
        try:
            p = resolve_in_roots(path, rts)
            text, meta = extract(p, pages=pages or None, sheet=sheet or None,
                                 max_rows=max(1, int(max_rows or MAX_ROWS_PER_SHEET)))
        except DocError as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"error: could not read {path!r}: {exc}"
        cap = max(500, min(int(max_chars or DEFAULT_MAX_CHARS), HARD_MAX_CHARS))
        off = max(0, int(offset or 0))
        chunk = text[off:off + cap]
        head = f"[{p.name} · {_fmt_meta(meta)} · chars {off}-{off + len(chunk)} of {len(text)}]\n"
        tail = ""
        if off + len(chunk) < len(text):
            tail = f"\n[… truncated — call again with offset={off + len(chunk)} for more]"
        return head + chunk + tail

    async def document_info(path: str = "") -> str:
        try:
            p = resolve_in_roots(path, rts)
        except DocError as exc:
            return f"error: {exc}"
        info = {"name": p.name, "sizeBytes": p.stat().st_size, "type": p.suffix.lower().lstrip(".") or "unknown",
                "supported": p.suffix.lower() in SUPPORTED}
        try:
            _, meta = extract(p, max_rows=1)
            info.update({k: v for k, v in meta.items() if k != "rows"} | ({"rows": meta["rows"]} if "rows" in meta else {}))
        except Exception as exc:  # noqa: BLE001
            info["note"] = f"could not inspect: {exc}"
        return "\n".join(f"{k}: {v}" for k, v in info.items())

    def _fmt_meta(meta: dict) -> str:
        return ", ".join(f"{k}={v}" for k, v in meta.items() if k not in ("pagesRead",))

    return {
        "document_read": {
            "fn": document_read,
            "schema": {"type": "function", "function": {
                "name": "document_read",
                "description": ("Read the text of a document: PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), "
                                "CSV/TSV or plain text. Paths are relative to your working folder; files people sent "
                                "in chat are given as absolute paths. Large files are returned in chunks — use "
                                "'offset' to continue, 'pages' (e.g. '1-3') for PDFs, 'sheet' for spreadsheets."),
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "file path"},
                    "pages": {"type": "string", "description": "PDF page range like '1-3,7' (default all)"},
                    "sheet": {"type": "string", "description": "spreadsheet sheet name or 1-based index (default all)"},
                    "offset": {"type": "integer", "description": "character offset to continue from"},
                    "max_chars": {"type": "integer", "description": f"max characters to return (default {DEFAULT_MAX_CHARS})"},
                    "max_rows": {"type": "integer", "description": f"max rows per sheet/CSV (default {MAX_ROWS_PER_SHEET})"},
                }, "required": ["path"]},
            }},
        },
        "document_info": {
            "fn": document_info,
            "schema": {"type": "function", "function": {
                "name": "document_info",
                "description": "Size, type and structure (pages / sheets / slides) of a document without reading it all.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }},
        },
    }
