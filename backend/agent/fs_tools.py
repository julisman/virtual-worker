"""Filesystem toolkit — the harness's "Your files" pillar.

An agent with a **working folder** is automatically handed a set of file tools
(read / write / edit / list / glob / grep) scoped to that folder, so it can read
and change real files on your machine without anything leaving it. This mirrors
the osaurus harness: pick a working folder and the agent gets file + search tools
for free.

Every path is resolved and confined to the working folder (``root``) — a
``../`` escape, an absolute path outside it, or a symlink that points out is
rejected before any I/O happens. ``make_fs_tools(root)`` returns a per-request
registry ``{name: {schema, fn}}`` matching the rest of the agent loop's tools.
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Guardrails so a tool call can't read/write a giant file or walk the whole disk.
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_LIST_ENTRIES = 500
MAX_MATCHES = 200
# Directories we never descend into when globbing/grepping (noise + huge).
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", ".cache", ".mypy_cache", ".pytest_cache"}


class FsError(Exception):
    """A filesystem-tool problem worth reporting back to the model verbatim."""


def _resolve_root(root: str | Path) -> Path:
    p = Path(root).expanduser()
    try:
        return p.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - exotic FS errors
        raise FsError(f"bad working folder: {exc}") from exc


def _safe(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse anything that escapes it."""
    rel = (rel or "").strip()
    if rel in ("", "."):
        return root
    candidate = (root / rel) if not Path(rel).is_absolute() else Path(rel)
    resolved = candidate.expanduser().resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise FsError(f"path escapes the working folder: {rel!r}")
    return resolved


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root)) or "."
    except ValueError:
        return str(p)


@functools.lru_cache(maxsize=1)
def _rg_binary() -> str | None:
    """Path to the ``rg`` (ripgrep) binary if installed, else None. Cached — the
    lookup is stable for the process lifetime. ``rg`` is an optional external
    accelerator; every rg path degrades to the Python scan when it is absent."""
    return shutil.which("rg")


def _rg_grep(root: Path, patterns: list[str], glob: str) -> list[str] | None:
    """Run ``rg`` for a case-insensitive fixed-string search under ``root``,
    returning formatted ``path:line: text`` matches (same shape as the Python
    scan). ``patterns`` is the effective pattern set — one ``-e <p>`` per
    pattern (a line matching ANY of them is a hit). Returns None on ANY failure
    — no binary, an error exit (2), a timeout, or an unparseable line — so the
    caller falls back to the Python scan. An exit code of 1 (no matches) is
    success with an empty list."""
    rg = _rg_binary()
    if not rg:
        return None
    # --max-count is per-file; use the global cap (a low per-file limit silently
    # hides occurrences), the MAX_MATCHES total cap below is the real bound.
    args = [rg, "-n", "--no-heading", "-i", "--fixed-strings",
            "--max-count", str(MAX_MATCHES)]
    if glob:
        args += ["-g", glob]
    for p in patterns:
        args += ["-e", p]
    args += ["--", "."]
    try:
        proc = subprocess.run(args, cwd=str(root), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 1:          # rg convention: 1 == no matches
        return []
    if proc.returncode != 0:          # 2 (error) or anything else → fall back
        return None
    hits: list[str] = []
    text = proc.stdout.decode("utf-8", errors="replace")
    for raw in text.splitlines():
        if not raw:
            continue
        parts = raw.split(":", 2)     # path:line:text (text may contain colons)
        if len(parts) != 3:
            return None               # unexpected format → fall back to Python
        rel, lineno, body = parts
        if rel.startswith("./"):      # rg prefixes paths relative to cwd with ./
            rel = rel[2:]
        hits.append(f"{rel}:{lineno}: {body.strip()[:200]}")
        if len(hits) >= MAX_MATCHES:
            break
    return hits


def _iter_files(root: Path):
    """Walk ``root`` yielding files, skipping noisy/huge directories."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in _SKIP_DIRS:
                    stack.append(e)
            elif e.is_file():
                yield e


def _parse_ref(ref: str) -> tuple[str, int | None, int | None]:
    """Split a batch/ranged read reference into ``(path, start, end)``.

    A ref is ``"path"``, ``"path:LINE"``, or ``"path:START-END"`` — the range is
    taken off the RIGHTMOST colon so filenames may contain colons/spaces. If the
    suffix after the last colon doesn't parse as a line number or a digit range,
    the whole string is treated as a path. ``(path, None, None)`` = whole file."""
    ref = ref.strip()
    if ":" in ref:
        head, _, tail = ref.rpartition(":")
        if head and tail:
            if tail.isdigit():
                n = int(tail)
                return head, n, n
            a, _, b = tail.partition("-")
            if a.isdigit() and b.isdigit():
                return head, int(a), int(b)
    return ref, None, None


# --------------------------------------------------------------------------- #
# Tool implementations (closures bound to a working folder)
# --------------------------------------------------------------------------- #

def _make(root: Path):
    # All bodies are SYNC and run via asyncio.to_thread (see the bottom of this
    # function): they do real filesystem I/O — a big read or a grep over a large
    # working folder would otherwise block the whole event loop (every request
    # and SSE stream in the process) for its duration.
    def _render_ref(ref_path: str, start: int | None, end: int | None,
                    remaining: int) -> tuple[str, int]:
        """Render one ref as a ``== label ==`` block with ``N\\t`` line-numbered
        content, consuming at most ``remaining`` bytes. Returns
        ``(block, consumed_bytes)``. Per-ref errors go inline in the block (so a
        batch's other refs still return) and consume nothing."""
        try:
            target = _safe(root, ref_path)
        except FsError as exc:
            return f"== {ref_path} ==\n{exc}", 0
        if not target.exists():
            return f"== {ref_path} ==\nnot found: {ref_path}", 0
        if target.is_dir():
            return f"== {ref_path} ==\n{ref_path} is a directory — use list_dir.", 0
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"== {ref_path} ==\nfile error: {exc}", 0
        lines = raw.splitlines()
        total = len(lines)
        whole = start is None and end is None
        if whole:
            s, e = 1, total
        else:
            s = 1 if (start is None or start < 1) else start
            if s > total:
                return f"== {ref_path} ==\nfile has {total} lines", 0
            e = total if (end is None or end > total) else end
            if e < s:
                e = s
        label = ref_path if whole else f"{ref_path}:{s}-{e}"
        out: list[str] = []
        consumed = 0
        truncated = False
        for n in range(s, e + 1):
            piece = f"{n}\t{lines[n - 1]}"
            size = len(piece.encode("utf-8")) + 1  # + newline
            if consumed + size > remaining:
                truncated = True
                break
            out.append(piece)
            consumed += size
        body = "\n".join(out)
        if truncated:
            body += ("\n" if body else "") + "…(truncated — read budget exhausted)"
        return f"== {label} ==\n{body}", consumed

    def _read_batch(refs: list[str], cap: int) -> str:
        clean = [str(r) for r in refs if str(r).strip()]
        if len(clean) > 8:
            return f"too many files: {len(clean)} refs — read at most 8 per call."
        if not clean:
            return "read_file 'files' is empty."
        blocks: list[str] = []
        remaining = cap
        for i, ref in enumerate(clean):
            if remaining <= 0:
                blocks.append(f"…(read budget of {cap} bytes exhausted — "
                              f"{len(clean) - i} more refs not shown)")
                break
            p, s, e = _parse_ref(ref)
            text, used = _render_ref(p, s, e, remaining)
            blocks.append(text)
            remaining -= used
        return "\n\n".join(blocks)

    def read_file(path: str = "", max_bytes: int = MAX_READ_BYTES,
                  start_line: int = 0, end_line: int = 0,
                  files: list[str] | None = None) -> str:
        cap = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
        if files:
            return _read_batch(files, cap)
        if start_line or end_line:
            s = int(start_line) if start_line else None
            e = int(end_line) if end_line else None
            text, _ = _render_ref(path, s, e, cap)
            return text
        # Plain read (no range, no batch): raw output, byte-exact — edit_file's
        # verbatim-copy contract depends on this path's shape. Do not change it.
        target = _safe(root, path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"{path} is a directory — use list_dir."
        with target.open("rb") as f:  # read only what the cap allows, not the whole file
            data = f.read(cap + 1)
        text = data.decode("utf-8", errors="replace")
        if len(data) > cap:
            text = text[:cap] + f"\n…(truncated at {cap} bytes)"
        return text

    def write_file(path: str = "", content: str = "") -> str:
        target = _safe(root, path)
        if target.is_dir():
            return f"{path} is a directory."
        body = content or ""
        if len(body.encode("utf-8")) > MAX_WRITE_BYTES:
            return f"refused: content exceeds {MAX_WRITE_BYTES} bytes."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return f"wrote {len(body)} chars to {_rel(root, target)}"

    def edit_file(path: str = "", old: str = "", new: str = "") -> str:
        target = _safe(root, path)
        if not target.is_file():
            return f"not found: {path}"
        if not old:
            return "edit_file needs a non-empty 'old' string to replace."
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # errors="replace" here would silently write U+FFFD back over the
            # original bytes on save — refuse instead of corrupting the file.
            return f"{path} is not valid UTF-8 text — refusing to edit it."

        count = text.count(old)
        if count == 0:
            return f"'old' string not found in {path} — read_file first to copy it exactly."
        if count > 1:
            return f"'old' string is not unique in {path} ({count} matches) — include more context."
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {_rel(root, target)} (1 replacement)"

    def list_dir(path: str = ".") -> str:
        target = _safe(root, path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_file():
            return f"{path} is a file ({target.stat().st_size} bytes)."
        entries = sorted(target.iterdir())
        out = []
        for e in entries[:MAX_LIST_ENTRIES]:
            if e.is_dir():
                out.append(f"{_rel(root, e)}/")
            else:
                try:
                    out.append(f"{_rel(root, e)} ({e.stat().st_size} b)")
                except OSError:
                    out.append(_rel(root, e))
        if not out:
            return f"{path}: (empty)"
        if len(entries) > MAX_LIST_ENTRIES:
            # Say the listing is clipped — a silent cut reads as "that's all".
            out.append(f"…(+{len(entries) - MAX_LIST_ENTRIES} more entries not shown)")
        return "\n".join(out)

    def glob(pattern: str = "") -> str:
        if not pattern:
            return "glob needs a 'pattern', e.g. **/*.py or src/*.ts"
        hits = []
        for f in _iter_files(root):
            rel = _rel(root, f)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                hits.append(rel)
                if len(hits) >= MAX_MATCHES:
                    break
        if not hits:
            return f"no files match {pattern!r}"
        return "\n".join(hits)

    def _grep_python(patterns: list[str], glob: str) -> list[str]:
        needles = [p.lower() for p in patterns]
        hits: list[str] = []
        for f in _iter_files(root):
            rel = _rel(root, f)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f.name, glob)):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                if any(nd in low for nd in needles):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_MATCHES:
                        return hits
        return hits

    def grep(pattern: str = "", glob: str = "", patterns: list[str] | None = None) -> str:
        # Effective set = pattern ∪ patterns, order-preserving dedupe, empties out.
        pats: list[str] = []
        for p in ([pattern] if pattern else []) + list(patterns or []):
            p = (p or "").strip()
            if p and p not in pats:
                pats.append(p)
        if not pats:
            return "grep needs a 'pattern' (plain substring, case-insensitive)."
        # rg is the fast path; it returns None on any failure so we fall back to
        # the Python scan below (same output format either way).
        hits = _rg_grep(root, pats, glob)
        if hits is None:
            hits = _grep_python(pats, glob)
        if not hits:
            return "no matches for " + ", ".join(repr(p) for p in pats)
        if len(hits) >= MAX_MATCHES:
            return "\n".join(hits[:MAX_MATCHES]) + f"\n…(stopped at {MAX_MATCHES} matches)"
        return "\n".join(hits)

    def _threaded(fn):
        async def wrapped(**kwargs):
            return await asyncio.to_thread(fn, **kwargs)
        return wrapped

    return {name: _threaded(fn) for name, fn in {
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "list_dir": list_dir,
        "glob": glob,
        "grep": grep,
    }.items()}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_SCHEMAS = {
    "read_file": _schema(
        "read_file",
        "Read text from the working folder. Three modes: (1) a whole file — "
        "`path` alone (raw, unnumbered). (2) a line range of one file — `path` "
        "plus `start_line`/`end_line` (1-based, inclusive; omit `end_line` to "
        "read to the end). (3) a batch — `files`, a list of up to 8 refs where "
        "each ref is \"path\", \"path:LINE\", or \"path:START-END\" (the range "
        "is the same path:line shape grep prints, so you can paste a grep hit "
        "straight in). Ranged and batch reads print `N\\t`-numbered lines under "
        "a `== path:start-end ==` header and share one read budget.",
        {"path": {"type": "string", "description": "path relative to the working folder"},
         "start_line": {"type": "integer", "description": "first line to read (1-based, inclusive)"},
         "end_line": {"type": "integer", "description": "last line to read (1-based, inclusive; omit for end-of-file)"},
         "files": {"type": "array", "items": {"type": "string"},
                   "description": "batch: up to 8 refs, each \"path\", \"path:LINE\", or \"path:START-END\""}},
        []),
    "write_file": _schema(
        "write_file", "Create or overwrite a file in the working folder.",
        {"path": {"type": "string", "description": "path relative to the working folder"},
         "content": {"type": "string", "description": "the full new file contents"}},
        ["path", "content"]),
    "edit_file": _schema(
        "edit_file", "Replace one exact unique occurrence of 'old' with 'new' in a file. "
        "Read the file first so 'old' matches verbatim.",
        {"path": {"type": "string", "description": "path relative to the working folder"},
         "old": {"type": "string", "description": "exact text to replace (must be unique)"},
         "new": {"type": "string", "description": "replacement text"}},
        ["path", "old", "new"]),
    "list_dir": _schema(
        "list_dir", "List files and folders in a directory of the working folder.",
        {"path": {"type": "string", "description": "directory path (default: the working folder root)"}},
        []),
    "glob": _schema(
        "glob", "Find files by name pattern (e.g. **/*.py, src/*.ts) within the working folder.",
        {"pattern": {"type": "string", "description": "a glob pattern"}},
        ["pattern"]),
    "grep": _schema(
        "grep", "Search file contents for a substring (case-insensitive) within the working folder. "
        "Returns path:line: text matches. Pass `patterns` (a list) to match ANY of several "
        "substrings in one pass; `pattern` and `patterns` are unioned.",
        {"pattern": {"type": "string", "description": "substring to search for"},
         "patterns": {"type": "array", "items": {"type": "string"},
                      "description": "match a line if ANY of these substrings occurs (unioned with `pattern`)"},
         "glob": {"type": "string", "description": "optional file filter, e.g. *.py"}},
        []),
}

# Tool names the working folder grants, in catalog order.
FS_TOOL_NAMES = ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep"]
# The read-only subset (no write_file/edit_file) — used for someone else's repo
# (a linked Wiki), which Virtual Worker must never create or write to.
FS_READONLY_TOOL_NAMES = ["read_file", "list_dir", "glob", "grep"]


def make_fs_tools(root: str | Path, *, readonly: bool = False) -> dict[str, dict]:
    """Build a registry ``{name: {schema, fn}}`` of file tools confined to ``root``.

    Returns an empty dict if ``root`` is blank. In the default (read-write) mode
    the folder is created if missing so a fresh agent can start writing into it
    immediately. In ``readonly`` mode only read tools are granted
    (``FS_READONLY_TOOL_NAMES``) and the root is **never** created — it must
    already exist as a directory (someone else's repo), else ``FsError``."""
    if not root or not str(root).strip():
        return {}
    base = _resolve_root(root)
    # A working folder must be a directory. If it points at an existing *file*,
    # `base.mkdir(exist_ok=True)` still raises FileExistsError ([Errno 17]) —
    # exist_ok only swallows the error for an existing *directory*. Unguarded,
    # that cryptic error crashes agent compose (HTTP 500 on chat, "scheduled run
    # failed" in the background). Fail with a clear, actionable message instead so
    # the caller can surface it or degrade gracefully.
    if base.exists() and not base.is_dir():
        raise FsError(f"working folder is a file, not a directory: {base} — "
                      "point the agent's working folder at a folder")
    if readonly:
        # Never create someone else's repository; require it to exist already.
        if not base.is_dir():
            raise FsError(f"linked folder does not exist: {base}")
    else:
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FsError(f"cannot use working folder {base}: {exc}") from exc
    fns = _make(base)
    names = FS_READONLY_TOOL_NAMES if readonly else FS_TOOL_NAMES
    return {name: {"fn": _guard(fns[name]), "schema": _SCHEMAS[name],
                   **({"kind": "read"} if name == "read_file" else {})}
            for name in names}


def _guard(fn):
    """Turn a refused-path (or unexpected OS) error into a string the model reads,
    so a bad path is a normal tool result rather than a crashed turn."""
    async def wrapped(**kwargs):
        try:
            return await fn(**kwargs)
        except FsError as exc:
            return str(exc)
        except OSError as exc:
            return f"file error: {exc}"
    return wrapped
