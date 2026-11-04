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

import fnmatch
import logging
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


# --------------------------------------------------------------------------- #
# Tool implementations (closures bound to a working folder)
# --------------------------------------------------------------------------- #

def _make(root: Path):
    async def read_file(path: str = "", max_bytes: int = MAX_READ_BYTES) -> str:
        target = _safe(root, path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_dir():
            return f"{path} is a directory — use list_dir."
        cap = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
        data = target.read_bytes()[: cap + 1]
        text = data.decode("utf-8", errors="replace")
        if len(data) > cap:
            text = text[:cap] + f"\n…(truncated at {cap} bytes)"
        return text

    async def write_file(path: str = "", content: str = "") -> str:
        target = _safe(root, path)
        if target.is_dir():
            return f"{path} is a directory."
        body = content or ""
        if len(body.encode("utf-8")) > MAX_WRITE_BYTES:
            return f"refused: content exceeds {MAX_WRITE_BYTES} bytes."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return f"wrote {len(body)} chars to {_rel(root, target)}"

    async def edit_file(path: str = "", old: str = "", new: str = "") -> str:
        target = _safe(root, path)
        if not target.is_file():
            return f"not found: {path}"
        if not old:
            return "edit_file needs a non-empty 'old' string to replace."
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            return f"'old' string not found in {path} — read_file first to copy it exactly."
        if count > 1:
            return f"'old' string is not unique in {path} ({count} matches) — include more context."
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {_rel(root, target)} (1 replacement)"

    async def list_dir(path: str = ".") -> str:
        target = _safe(root, path)
        if not target.exists():
            return f"not found: {path}"
        if target.is_file():
            return f"{path} is a file ({target.stat().st_size} bytes)."
        out = []
        for e in sorted(target.iterdir())[:MAX_LIST_ENTRIES]:
            if e.is_dir():
                out.append(f"{_rel(root, e)}/")
            else:
                try:
                    out.append(f"{_rel(root, e)} ({e.stat().st_size} b)")
                except OSError:
                    out.append(_rel(root, e))
        if not out:
            return f"{path}: (empty)"
        return "\n".join(out)

    async def glob(pattern: str = "") -> str:
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

    async def grep(pattern: str = "", glob: str = "") -> str:
        if not pattern:
            return "grep needs a 'pattern' (plain substring, case-insensitive)."
        needle = pattern.lower()
        hits = []
        for f in _iter_files(root):
            rel = _rel(root, f)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f.name, glob)):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_MATCHES:
                        return "\n".join(hits) + f"\n…(stopped at {MAX_MATCHES} matches)"
        if not hits:
            return f"no matches for {pattern!r}"
        return "\n".join(hits)

    return {
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "list_dir": list_dir,
        "glob": glob,
        "grep": grep,
    }


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
        "read_file", "Read a text file from the working folder.",
        {"path": {"type": "string", "description": "path relative to the working folder"}},
        ["path"]),
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
        "Returns path:line: text matches.",
        {"pattern": {"type": "string", "description": "substring to search for"},
         "glob": {"type": "string", "description": "optional file filter, e.g. *.py"}},
        ["pattern"]),
}

# Tool names the working folder grants, in catalog order.
FS_TOOL_NAMES = ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep"]


def make_fs_tools(root: str | Path) -> dict[str, dict]:
    """Build a registry ``{name: {schema, fn}}`` of file tools confined to ``root``.

    Returns an empty dict if ``root`` is blank. The folder is created if missing
    so a fresh agent can start writing into it immediately."""
    if not root or not str(root).strip():
        return {}
    base = _resolve_root(root)
    base.mkdir(parents=True, exist_ok=True)
    fns = _make(base)
    return {name: {"fn": _guard(fns[name]), "schema": _SCHEMAS[name]} for name in FS_TOOL_NAMES}


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
