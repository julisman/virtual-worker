"""Local document extraction — the engine behind the document tools (PRD-18).

Turns the file types real work arrives in — PDF, .docx, .xlsx, .pptx, CSV/TSV —
into readable text and Markdown tables, entirely on-device. OOXML formats are
parsed with the stdlib (``zipfile`` + ``xml.etree``); only PDF needs a
dependency (``pypdf``, pure Python), imported lazily so everything else works
without it.

Shared by ``agent/doc_tools.py`` (the ``document_read``/``document_info`` agent
tools) and intended for the KB ingest route (PRD-03 M3) so both surfaces read
documents identically.
"""

from __future__ import annotations

import csv
import itertools
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

# Guardrails: refuse huge inputs and OOXML zip bombs before parsing anything.
MAX_FILE_BYTES = 50_000_000
MAX_MEMBER_BYTES = 100_000_000    # sum of decompressed OOXML member sizes
DEFAULT_MAX_CHARS = 20_000
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 30

SUPPORTED_EXTS = (".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".tsv")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocError(Exception):
    """A document problem worth reporting back to the model verbatim."""


def extract(path: Path, *, pages: str = "", sheet: str = "", rows: str = "",
            max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Extract readable text from ``path``; raises DocError on any problem."""
    ext = _checked(path)
    if ext == ".pdf":
        text = _pdf_text(path, pages)
    elif ext == ".docx":
        text = _docx_text(path)
    elif ext == ".xlsx":
        text = _xlsx_text(path, sheet, rows)
    elif ext == ".pptx":
        text = _pptx_text(path)
    else:  # .csv / .tsv
        text = _csv_text(path, delimiter="\t" if ext == ".tsv" else "", rows_spec=rows)
    return _truncate(text.strip() or "(no readable text)", max_chars)


def info(path: Path) -> str:
    """One-line structure summary so the model can pick pages/sheets first."""
    ext = _checked(path)
    size = path.stat().st_size
    if ext == ".pdf":
        return f"PDF, {len(_pdf_reader(path).pages)} pages, {size} bytes"
    if ext == ".docx":
        body = _member_root(path, "word/document.xml")
        paras = sum(1 for _ in body.iter(f"{_W}p"))
        tables = sum(1 for _ in body.iter(f"{_W}tbl"))
        return f"Word document, ~{paras} paragraphs, {tables} tables, {size} bytes"
    if ext == ".xlsx":
        with _zip(path) as zf:
            labels = []
            for name, member in _xlsx_sheets(path):
                count = _sheet_row_count(zf, member)
                labels.append(f"{name} ({count} rows)" if count else name)
        return f"Excel workbook, sheets: [{', '.join(labels)}], {size} bytes"
    if ext == ".pptx":
        return f"PowerPoint, {len(_pptx_slides(path))} slides, {size} bytes"
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        rows = sum(1 for _ in fh)
    return f"{ext[1:].upper()} file, {rows} rows, {size} bytes"


def _checked(path: Path) -> str:
    ext = path.suffix.lower()
    if not path.is_file():
        raise DocError(f"not found: {path.name}")
    if ext not in SUPPORTED_EXTS:
        raise DocError(f"unsupported document type {ext or '(none)'!r} — supported: "
                       f"{', '.join(SUPPORTED_EXTS)}. For plain text use read_file.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocError(f"file exceeds {MAX_FILE_BYTES // 1_000_000} MB — refusing to parse.")
    return ext


def _truncate(text: str, max_chars: int) -> str:
    cap = max(200, min(int(max_chars or DEFAULT_MAX_CHARS), 200_000))
    if len(text) > cap:
        return (text[:cap] + f"\n…(+{len(text) - cap} chars truncated — "
                "raise max_chars or narrow pages/sheet)")
    return text


# --------------------------------------------------------------------------- #
# OOXML plumbing
# --------------------------------------------------------------------------- #

def _zip(path: Path) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise DocError(f"{path.name} is not a valid Office file: {exc}") from exc
    if sum(i.file_size for i in zf.infolist()) > MAX_MEMBER_BYTES:
        raise DocError(f"{path.name} decompresses too large — refusing to parse.")
    return zf


def _root(zf: zipfile.ZipFile, member: str) -> ElementTree.Element:
    try:
        with zf.open(member) as fh:
            return ElementTree.parse(fh).getroot()
    except KeyError as exc:
        raise DocError(f"malformed Office file: missing {member}") from exc
    except ElementTree.ParseError as exc:
        raise DocError(f"malformed Office file: bad XML in {member}") from exc


def _member_root(path: Path, member: str) -> ElementTree.Element:
    with _zip(path) as zf:
        return _root(zf, member)


def _table_md(rows: list[list[str]]) -> str:
    """Render rows as a Markdown table, bounded to MAX_TABLE_ROWS × MAX_TABLE_COLS."""
    if not rows:
        return "(empty table)"
    width = min(max(len(r) for r in rows), MAX_TABLE_COLS)
    shown = rows[:MAX_TABLE_ROWS]
    clean = [[(c or "").replace("|", "\\|").replace("\n", " ") for c in (r + [""] * width)[:width]]
             for r in shown]
    out = ["| " + " | ".join(clean[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    dropped_rows = len(rows) - len(shown)
    dropped_cols = max(len(r) for r in rows) - width
    if dropped_rows > 0 or dropped_cols > 0:
        out.append(f"…(table truncated: +{max(dropped_rows, 0)} rows, "
                   f"+{max(dropped_cols, 0)} columns not shown)")
    return "\n".join(out)


def _parse_rows(spec: str, count: int | None) -> tuple[int, int]:
    """'201-400' or '350' → 1-based inclusive (lo, hi) window.

    Empty spec → the whole range. ``count`` is the total row count when known
    (Excel — bounds-checked) or ``None`` when it isn't (CSV streaming)."""
    spec = (spec or "").strip()
    if not spec:
        return 1, count if count is not None else 10**9
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    if not m:
        raise DocError(f"bad rows spec {spec!r} — use e.g. '201-400'")
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    if lo < 1 or lo > hi:
        raise DocError(f"bad rows spec {spec!r} — use e.g. '201-400'")
    if count is not None and lo > count:
        raise DocError(f"rows {spec!r} out of range — the sheet has {count} rows")
    return lo, min(hi, count) if count is not None else hi


# --------------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------------- #

def _docx_text(path: Path) -> str:
    root = _member_root(path, "word/document.xml")
    body = root.find(f"{_W}body")
    if body is None:
        raise DocError("malformed .docx: no document body")
    out: list[str] = []
    for el in body:
        if el.tag == f"{_W}p":
            text = "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()
            if text:
                out.append(text)
        elif el.tag == f"{_W}tbl":
            rows = [["".join(t.text or "" for t in tc.iter(f"{_W}t")).strip()
                     for tc in tr.findall(f"{_W}tc")]
                    for tr in el.findall(f"{_W}tr")]
            out.append(_table_md(rows))
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# .xlsx
# --------------------------------------------------------------------------- #

def _xlsx_sheets(path: Path) -> list[tuple[str, str]]:
    """[(sheet name, zip member path)] in workbook order."""
    with _zip(path) as zf:
        rels = {rel.get("Id"): rel.get("Target", "")
                for rel in _root(zf, "xl/_rels/workbook.xml.rels").iter(f"{_PR}Relationship")}
        sheets = []
        for sh in _root(zf, "xl/workbook.xml").iter(f"{_S}sheet"):
            target = rels.get(sh.get(f"{_R}id"), "").lstrip("/")
            member = target if target.startswith("xl/") else f"xl/{target}"
            sheets.append((sh.get("name", "?"), member))
    if not sheets:
        raise DocError("malformed .xlsx: no sheets")
    return sheets


def _col_index(cell_ref: str) -> int:
    n = 0
    for ch in cell_ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(n - 1, 0)


def _sheet_row_count(zf: zipfile.ZipFile, member: str) -> int:
    """Row count from the sheet's ``<dimension ref="A1:F5000"/>`` header, read
    incrementally so a huge sheet isn't fully parsed just for ``info()``. 0 when
    the writer omitted the element (rare) or anything goes wrong."""
    try:
        with zf.open(member) as fh:
            for _, el in ElementTree.iterparse(fh):
                if el.tag == f"{_S}dimension":
                    m = re.search(r"(\d+)\s*$", el.get("ref") or "")
                    return int(m.group(1)) if m else 0
                if el.tag == f"{_S}row":   # data reached — no dimension element
                    return 0
    except Exception:  # noqa: BLE001 — a probe, never worth failing info() over
        pass
    return 0


# numFmtIds Excel reserves for date/time formats (14–22 dates, 45–47 durations).
_BUILTIN_DATE_NUMFMTS = frozenset(range(14, 23)) | frozenset(range(45, 48))


def _xlsx_date_styles(zf: zipfile.ZipFile) -> frozenset[int]:
    """Cell-style (``s`` attribute) indexes whose number format is a date/time.

    Custom formats are sniffed by their code: after stripping quoted literals,
    ``[...]`` sections and escapes, any y/m/d/h/s means date or time — the same
    heuristic openpyxl uses. Best-effort: no styles part → empty set."""
    if "xl/styles.xml" not in zf.namelist():
        return frozenset()
    try:
        styles = _root(zf, "xl/styles.xml")
    except DocError:
        return frozenset()
    date_ids = set(_BUILTIN_DATE_NUMFMTS)
    for fmt in styles.iter(f"{_S}numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", fmt.get("formatCode") or "")
        if re.search(r"[ymdhs]", code, re.IGNORECASE):
            try:
                date_ids.add(int(fmt.get("numFmtId") or ""))
            except ValueError:
                pass
    cell_xfs = styles.find(f"{_S}cellXfs")
    if cell_xfs is None:
        return frozenset()
    out = set()
    for i, xf in enumerate(cell_xfs.findall(f"{_S}xf")):
        try:
            if int(xf.get("numFmtId") or 0) in date_ids:
                out.add(i)
        except ValueError:
            pass
    return frozenset(out)


def _xlsx_epoch(zf: zipfile.ZipFile) -> datetime:
    """Serial-date epoch: the 1900 system unless workbookPr says date1904."""
    try:
        pr = _root(zf, "xl/workbook.xml").find(f"{_S}workbookPr")
        if pr is not None and (pr.get("date1904") or "").lower() in ("1", "true"):
            return datetime(1904, 1, 1)
    except DocError:
        pass
    return datetime(1899, 12, 30)


def _fmt_serial(raw: str, epoch: datetime) -> str:
    """An Excel date/time serial → ISO text ('2025-09-03', '…14:30:00', or a
    bare time for sub-day serials). Non-numeric or out-of-range → unchanged."""
    try:
        serial = float(raw)
    except ValueError:
        return raw
    if not 0 <= serial < 2_958_466:   # year 9999 in the 1900 system
        return raw
    dt = epoch + timedelta(days=serial)
    dt = (dt + timedelta(seconds=0.5)).replace(microsecond=0)   # round to seconds
    if serial < 1:
        return dt.strftime("%H:%M:%S")
    if dt.hour == dt.minute == dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_ROWS_HINT = " — pass rows='<from>-<to>' to page through"


def _xlsx_text(path: Path, sheet: str, rows_spec: str = "") -> str:
    sheets = _xlsx_sheets(path)
    if sheet:
        match = next(((n, m) for n, m in sheets if n.lower() == sheet.strip().lower()), None)
        if match is None:
            raise DocError(f"no sheet named {sheet!r} — sheets: "
                           f"[{', '.join(n for n, _ in sheets)}]")
        chosen = [match]
    else:
        chosen = sheets[:1]
    with _zip(path) as zf:
        strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            strings = ["".join(t.text or "" for t in si.iter(f"{_S}t"))
                       for si in _root(zf, "xl/sharedStrings.xml").iter(f"{_S}si")]
        date_styles = _xlsx_date_styles(zf)
        epoch = _xlsx_epoch(zf)
        out = []
        for name, member in chosen:
            grid: list[list[str]] = []
            for row in _root(zf, member).iter(f"{_S}row"):
                cells: dict[int, str] = {}
                for c in row.findall(f"{_S}c"):
                    idx = _col_index(c.get("r", ""))
                    if c.get("t") == "s":
                        v = c.find(f"{_S}v")
                        i = int(v.text) if v is not None and v.text else -1
                        cells[idx] = strings[i] if 0 <= i < len(strings) else ""
                    elif c.get("t") == "inlineStr":
                        cells[idx] = "".join(t.text or "" for t in c.iter(f"{_S}t"))
                    else:
                        v = c.find(f"{_S}v")
                        raw = v.text or "" if v is not None else ""
                        if raw and c.get("t") in (None, "n") and date_styles:
                            try:
                                if int(c.get("s") or -1) in date_styles:
                                    raw = _fmt_serial(raw, epoch)
                            except ValueError:
                                pass
                        cells[idx] = raw
                width = max(cells, default=-1) + 1
                grid.append([cells.get(i, "") for i in range(width)])
            total = len(grid)
            lo, hi = _parse_rows(rows_spec, total)
            # A window past the top keeps row 1 as the table header for context;
            # hi is clamped so header + window always fit the table row cap.
            header = grid[:1] if lo > 1 else []
            hi = min(hi, lo + MAX_TABLE_ROWS - 1 - len(header))
            label = f", rows {lo}–{hi} of {total}" if rows_spec else f", {total} rows"
            table = _table_md(header + grid[lo - 1:hi])
            if hi < total:
                table += f"\n…(+{total - hi} more rows{_ROWS_HINT})"
            hint = "" if sheet else f" (of [{', '.join(n for n, _ in sheets)}] — pass sheet= for another)"
            out.append(f"## Sheet: {name}{label}{hint}\n\n{table}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# .pptx
# --------------------------------------------------------------------------- #

def _pptx_slides(path: Path) -> list[str]:
    with _zip(path) as zf:
        slides = [n for n in zf.namelist()
                  if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(slides, key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))


def _pptx_text(path: Path) -> str:
    out = []
    with _zip(path) as zf:
        for i, member in enumerate(_pptx_slides(path), 1):
            texts = [t.text for t in _root(zf, member).iter(f"{_A}t") if (t.text or "").strip()]
            out.append(f"## Slide {i}\n\n" + ("\n".join(texts) if texts else "(no text)"))
    if not out:
        raise DocError("malformed .pptx: no slides")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# .csv / .tsv
# --------------------------------------------------------------------------- #

def _csv_text(path: Path, delimiter: str = "", rows_spec: str = "") -> str:
    sample = path.read_text(encoding="utf-8", errors="replace")[:8192]
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    lo, hi = _parse_rows(rows_spec, None)
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        # A window past the top keeps row 1 as the table header for context;
        # one sentinel row past the window tells us the file continues.
        header = [next(reader, None)] if lo > 1 else []
        header = [h for h in header if h is not None]
        take = min(hi - lo + 1, MAX_TABLE_ROWS - len(header))
        skip = lo - 1 - len(header)
        got = list(itertools.islice(reader, skip, skip + take + 1))
    table = _table_md(header + got[:take])
    if len(got) > take:
        table += f"\n…(more rows in the file{_ROWS_HINT})"
    return table


# --------------------------------------------------------------------------- #
# .pdf (pypdf — the one non-stdlib parser, imported lazily)
# --------------------------------------------------------------------------- #

def _pdf_reader(path: Path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocError("PDF support needs the 'pypdf' package — "
                       "install backend requirements and retry.") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocError(f"{path.name} is password-protected — ask for an unlocked copy.")
        return reader
    except DocError:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf raises many parse error types
        raise DocError(f"cannot parse {path.name} as PDF: {exc}") from exc


def _parse_pages(spec: str, count: int) -> list[int]:
    """'1-3,7' → 0-based page indexes, bounds-checked; empty spec → all pages."""
    if not (spec or "").strip():
        return list(range(count))
    picked: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise DocError(f"bad pages spec {spec!r} — use e.g. '1-3,7'")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi > count or lo > hi:
            raise DocError(f"pages {part!r} out of range — the PDF has {count} pages")
        picked.extend(range(lo - 1, hi))
    return picked


def _pdf_text(path: Path, pages: str) -> str:
    reader = _pdf_reader(path)
    idxs = _parse_pages(pages, len(reader.pages))
    out = []
    for i in idxs:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = f"(page {i + 1}: extraction failed: {exc})"
        out.append(f"--- page {i + 1} ---\n{text.strip() or '(no extractable text — scanned page?)'}")
    return "\n\n".join(out)
