"""Read-only linked Wiki repository support (PRD-21).

The repository is deliberately *not* copied or indexed by Virtual Worker.  This module is
the small, confined boundary around a locally checked out Markdown repository:
probe it, read it, directly scan it when no repository operation is trusted,
and run an explicitly registered argv operation.  It never accepts shell text.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE_BYTES = 1_000_000
MAX_SCAN_FILES = 2_000
MAX_SCAN_BYTES = 8_000_000
_EXCLUDED_PARTS = {".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", ".cache", "__pycache__", ".venv", "venv"}
# Instruction files injected into the agent context and mined for operations.
# AGENTS.md first when both exist (it is the Virtual Worker-native convention); CLAUDE.md
# often carries the critical "use the index first" rule. Neither appears in the
# markdown page catalog.
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


_OPERATION_KINDS = {"setup", "sync", "maintenance"}


class LinkedWikiError(ValueError):
    pass


def _inside(root: Path, candidate: Path) -> Path:
    root = root.expanduser().resolve(strict=True)
    resolved = candidate.expanduser().resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise LinkedWikiError("path escapes the linked repository")
    return resolved


def resolve_root(repo_path: str, wiki_root: str = ".") -> tuple[Path, Path]:
    repo = Path(repo_path).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise LinkedWikiError("repository path must be a directory")
    root_rel = (wiki_root or ".").strip() or "."
    if Path(root_rel).is_absolute():
        raise LinkedWikiError("wiki root must be relative to the repository")
    root = _inside(repo, repo / root_rel)
    if not root.is_dir():
        raise LinkedWikiError("wiki root must be an existing directory")
    return repo, root


def _is_eligible(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    if path.name in _INSTRUCTION_FILES or path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
        return False
    if any(part in _EXCLUDED_PARTS or part.startswith(".env") for part in rel.parts):
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES and _inside(root, path) == path.resolve()
    except (OSError, LinkedWikiError):
        return False


# page_files() is the walk that costs — every direct_search / list_pages call
# re-walks the whole checkout. Cache the result per root with a short TTL plus a
# coarse directory-mtime check: on a cache miss we scan directory mtimes only
# (cheap) and rebuild the file list only when a directory changed since the last
# build. NOTE: an in-place edit of a file inside an existing directory does not
# change that directory's mtime, so it may not invalidate the cache within the
# TTL — this is coarse by design (adding/removing a file does bump the dir mtime).
_PAGE_CACHE: dict[str, tuple[float, list[Path]]] = {}
_PAGE_CACHE_LOCK = threading.Lock()
_PAGE_CACHE_TTL = 2.0


def _invalidate_page_cache(root: Path | str | None = None) -> None:
    """Drop the page_files cache — the whole cache when ``root`` is None, else the
    one entry. For tests and for callers that just mutated a checkout."""
    with _PAGE_CACHE_LOCK:
        if root is None:
            _PAGE_CACHE.clear()
        else:
            _PAGE_CACHE.pop(str(Path(root).expanduser().resolve(strict=False)), None)


def _scan_page_files(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*") if _is_eligible(p, root)), key=lambda p: str(p))[:MAX_SCAN_FILES]


def _dirs_changed_since(root: Path, since: float) -> bool:
    """True if any (non-excluded) directory under ``root`` has an mtime newer than
    ``since`` — a coarse signal that files were added/removed."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            if d.stat().st_mtime > since:
                return True
            entries = list(d.iterdir())
        except OSError:
            return True
        for e in entries:
            try:
                if e.is_dir() and e.name not in _EXCLUDED_PARTS and not e.name.startswith(".env"):
                    stack.append(e)
            except OSError:
                return True
    return False


def page_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    key = str(root.expanduser().resolve(strict=False))
    # Wall-clock stamp (comparable with directory mtimes on the coarse check).
    now = time.time()
    with _PAGE_CACHE_LOCK:
        cached = _PAGE_CACHE.get(key)
    if cached and now - cached[0] < _PAGE_CACHE_TTL:
        return list(cached[1])
    # Beyond the TTL: keep the cached list if no directory changed since it was
    # built; otherwise rebuild.
    if cached and not _dirs_changed_since(root, cached[0]):
        with _PAGE_CACHE_LOCK:
            _PAGE_CACHE[key] = (now, cached[1])
        return list(cached[1])
    files = _scan_page_files(root)
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE[key] = (now, files)
    return list(files)


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def _rel(root: Path, path: Path) -> str:
    return PurePosixPath(path.relative_to(root)).as_posix()


def list_pages(root: Path) -> list[dict[str, str]]:
    out = []
    for path in page_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = _rel(root, path)
        out.append({"path": rel, "title": _title(text, rel), "type": "", "description": ""})
    return out


def list_pages_page(root: Path, prefix: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """A paginated slice of the page list.  The title is the filename stem — we
    deliberately do NOT read every file for a heading here (that is the perf bug
    for repositories with thousands of pages).  ``prefix`` filters by path,
    case-insensitively."""
    rels = [_rel(root, path) for path in page_files(root)]
    pref = (prefix or "").strip().lower()
    if pref:
        rels = [rel for rel in rels if pref in rel.lower()]
    total = len(rels)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), 500))
    window = rels[offset:offset + limit]
    items = [{"path": rel, "title": PurePosixPath(rel).stem} for rel in window]
    return {"items": items, "total": total, "hasMore": offset + limit < total}


def read_page(root: Path, path: str) -> str | None:
    rel = (path or "").strip()
    if not rel or Path(rel).is_absolute():
        raise LinkedWikiError("page path must be relative")
    target = _inside(root, root / rel)
    if not _is_eligible(target, root):
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def _snippet(text: str, terms: list[str], width: int = 180) -> str:
    low = text.lower()
    pos = min((low.find(t) for t in terms if t in low), default=-1)
    start = max(0, pos - width // 3) if pos >= 0 else 0
    piece = re.sub(r"\s+", " ", text[start:start + width]).strip()
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


# Duplicated locally (rather than imported from agent.fs_tools) to keep the
# storage layer from importing an agent module — a one-line lookup, no cycle.
@functools.lru_cache(maxsize=1)
def _rg_binary() -> str | None:
    return shutil.which("rg")


def _rg_candidates(root: Path, terms: list[str]) -> set[str] | None:
    """The set of repo-relative Markdown paths matching ANY query term, via
    ``rg -l`` (candidate filter only). Returns None on any rg failure so the
    caller falls back to the full scan. Empty set = no candidate matched."""
    rg = _rg_binary()
    if not rg:
        return None
    union: set[str] = set()
    for term in terms[:8]:
        args = [rg, "-l", "-i", "--fixed-strings", "--glob", "*.md", "--glob", "*.markdown", "--", term, "."]
        try:
            proc = subprocess.run(args, cwd=str(root), capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 1):      # 1 == no matches; anything else → fall back
            return None
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            union.add(line[2:] if line.startswith("./") else line)
    return union


def direct_search(root: Path, query: str, k: int = 5) -> list[dict[str, Any]]:
    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return []
    # rg narrows the eligible pages to a candidate set; the existing Python
    # term-frequency scoring then runs over just those (any rg failure → full
    # scan). The caps below still bound the scoring loop either way.
    files = page_files(root)
    candidates = _rg_candidates(root, terms)
    if candidates is None:
        scan_files = files
    else:
        scan_files = [p for p in files if _rel(root, p) in candidates]
    out: list[dict[str, Any]] = []
    read_bytes = 0
    for path in scan_files:
        if len(out) >= MAX_SCAN_FILES or read_bytes >= MAX_SCAN_BYTES:
            break
        text = path.read_text(encoding="utf-8", errors="replace")
        read_bytes += len(text.encode("utf-8", errors="replace"))
        title = _title(text, _rel(root, path))
        hay = f"{title}\n{text}".lower()
        score = sum(hay.count(term) for term in terms) + 3 * sum(term in title.lower() for term in terms)
        if score:
            out.append({"path": _rel(root, path), "title": title, "score": score, "snippet": _snippet(text, terms)})
    out.sort(key=lambda hit: (-hit["score"], hit["path"]))
    return out[:max(1, min(int(k or 5), 20))]


def instruction_paths(repo: Path, root: Path, page_path: str = "") -> list[Path]:
    paths: list[Path] = []
    def add(candidate: Path) -> None:
        if candidate.is_file() and candidate not in paths:
            paths.append(candidate)

    def add_scope(scope: Path) -> None:
        # AGENTS.md first, then CLAUDE.md, within a single directory scope.
        for name in _INSTRUCTION_FILES:
            add(scope / name)

    # Repository-level instructions apply to every linked Wiki.  A Wiki root
    # below the checkout can then add its own instruction files, followed by
    # directory-scoped instructions from shallow to deep.
    add_scope(repo)
    add_scope(root)
    if page_path:
        page = _inside(root, root / page_path)
        parent = page.parent
        scoped: list[Path] = []
        while parent != root and root in parent.parents:
            scoped.append(parent)
            parent = parent.parent
        for scope in reversed(scoped):
            add_scope(scope)
    return paths


def instructions(repo: Path, root: Path, page_path: str = "") -> list[dict[str, str]]:
    return [{"path": _rel(repo, p), "content": p.read_text(encoding="utf-8", errors="replace"),
             "hash": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in instruction_paths(repo, root, page_path)]


def _run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def current_revision(repo: Path) -> str | None:
    """The repo's current HEAD commit, or None if it isn't a Git checkout (public
    wrapper over the private git helper, for staleness checks)."""
    return _run_git(repo, "rev-parse", "HEAD") or None


def _manifest(repo: Path) -> tuple[dict[str, Any], str | None]:
    candidate = repo / ".virtual-worker" / "wiki.json"
    if not candidate.is_file():
        return {}, None
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "invalid .virtual-worker/wiki.json"
    return value if isinstance(value, dict) else {}, None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def probe(repo_path: str, wiki_root: str = ".") -> dict[str, Any]:
    repo, root = resolve_root(repo_path, wiki_root)
    manifest, manifest_error = _manifest(repo)
    candidate_root = manifest.get("wikiRoot") if wiki_root == "." else None
    if isinstance(candidate_root, str) and candidate_root.strip():
        _, root = resolve_root(str(repo), candidate_root)
    pages = page_files(root)
    revision = _run_git(repo, "rev-parse", "HEAD")
    dirty = bool(_run_git(repo, "status", "--porcelain")) if revision else False
    operation_names = sorted((manifest.get("operations") or {}).keys()) if isinstance(manifest.get("operations"), dict) else []
    return {"repoPath": str(repo), "wikiRoot": _rel(repo, root) or ".", "pageCount": len(pages),
            "revision": revision or None, "dirty": dirty, "hasSubmodules": (repo / ".gitmodules").is_file(),
            "instructions": [{"path": item["path"], "hash": item["hash"]} for item in instructions(repo, root)],
            "manifest": manifest, "manifestError": manifest_error, "operationNames": operation_names}


def normalize_operations(recipe: dict[str, Any], repo: Path) -> dict[str, dict[str, Any]]:
    raw = recipe.get("operations") if isinstance(recipe, dict) else {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not isinstance(value, dict):
            continue
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
            continue
        cwd = value.get("cwd", ".")
        try:
            if not isinstance(cwd, str) or Path(cwd).is_absolute():
                continue
            _inside(repo, repo / cwd)
        except LinkedWikiError:
            continue
        inputs = value.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        placeholders = {part[1:-1] for part in argv if re.fullmatch(r"\{[a-zA-Z][a-zA-Z0-9_]*\}", part)}
        normalized_inputs: dict[str, dict[str, Any]] = {}
        valid_inputs = True
        for input_name, spec in inputs.items():
            if (not isinstance(input_name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", input_name)
                    or not isinstance(spec, dict) or spec.get("type", "string") != "string"):
                valid_inputs = False
                break
            normalized_inputs[input_name] = {
                "type": "string",
                "maxLength": _bounded_int(spec.get("maxLength", 500), 500, 1, 10_000),
            }
        # Every declared input must be used in exactly one argv element.  This
        # makes the schema inspectable and prevents a caller from smuggling an
        # untemplated argument through a loosely specified operation.
        if not valid_inputs or set(normalized_inputs) != placeholders:
            continue
        mode = value.get("mode", "explicit_user")
        if mode not in {"assistant", "explicit_user"}:
            continue
        # Operation kind (linked-wiki revamp §4) — Virtual Worker attaches behavior per kind
        # (setup pending / sync surfaces). Kinds are setup/sync/maintenance;
        # anything unknown or omitted falls back to the general "maintenance".
        kind = value.get("kind", "maintenance")
        if kind not in _OPERATION_KINDS:
            kind = "maintenance"
        normalized = {"name": name, "description": str(value.get("description") or ""), "argv": argv,
                      "cwd": cwd, "inputs": normalized_inputs, "mode": mode, "kind": kind,
                      "timeoutSeconds": _bounded_int(value.get("timeoutSeconds", 30), 30, 1, 900),
                      "outputLimitBytes": _bounded_int(value.get("outputLimitBytes", 65536), 65536, 1024, 1_000_000)}
        normalized["configHash"] = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        out[name] = normalized
    return out


# --------------------------------------------------------------------------- #
# Read-only command discovery (PRD-21 auto-discover) — parse, never execute
# --------------------------------------------------------------------------- #

# Read-only verbs run in assistant mode; every other (write/network) or unknown
# verb defaults to explicit_user so the safe choice is the default.  Write verbs
# for reference: setup, sync, update, index, bootstrap, digest, fetch, promote,
# manifest, dictionary, source.
_READONLY_VERBS = {
    "query", "ask", "recall", "search", "status", "scope", "pending", "list",
    "show", "find", "sym", "table", "url", "page", "callers", "callees",
    "chain", "brief", "doctor", "lint",
}

# `./scripts/kb <sub> [<sub2>] ["free text"]` or `./kb …`, with or without the
# surrounding backticks.  One or two bare subcommand tokens, then an optional
# single-quoted / double-quoted free-text argument.
_KB_CMD_RE = re.compile(
    r"(\./(?:scripts/)?kb)"
    r"((?:\s+[a-z][a-zA-Z0-9_-]*){1,2})"
    r"""(?:\s+"([^"]*)"|\s+'([^']*)')?"""
)
_MAX_DOC_FILES = 12
_MAX_DOC_BYTES = 1_000_000


def _guess_mode(verb: str) -> str:
    return "assistant" if verb in _READONLY_VERBS else "explicit_user"


# Conventional lifecycle commands Virtual Worker suggests at link time so a user reviews a
# concrete argv instead of typing it.  Each well-known target/script name maps to
# the operation kind it should carry.  All run as `explicit_user` (side effects;
# the human who clicks Run is the approval).
_LIFECYCLE_KIND = {
    "setup": "setup", "bootstrap": "setup", "install": "setup",
    "sync": "sync", "update": "sync", "pull": "sync",
    "check": "maintenance", "lint": "maintenance", "test": "maintenance", "build": "maintenance",
}
_MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")
_MAKE_TARGET_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:")


def _convention_operations(repo: Path) -> dict[str, dict[str, Any]]:
    """Detect setup/sync/check-style operations from a repo's *conventions* —
    executable scripts, Makefile targets, and package.json scripts — without
    running anything.  Returns ``name -> {"source": "convention", "proposal"}``.
    First source to claim a name wins (scripts, then make, then npm)."""
    found: dict[str, dict[str, Any]] = {}

    def _claim(name: str, argv: list[str], detail: str) -> None:
        if name in _LIFECYCLE_KIND and name not in found:
            found[name] = {"source": "convention", "proposal": {
                "name": name, "description": f"Detected from {detail}", "argv": argv,
                "inputs": {}, "mode": "explicit_user", "kind": _LIFECYCLE_KIND[name]}}

    # 1. Executable scripts at conventional paths (./setup.sh, scripts/sync, …).
    for name in _LIFECYCLE_KIND:
        for rel in (f"{name}.sh", name, f"scripts/{name}", f"scripts/{name}.sh", f"bin/{name}"):
            path = repo / rel
            if not path.is_file():
                continue
            executable = os.access(path, os.X_OK)
            argv = [f"./{rel}"] if executable else ["sh", rel]
            _claim(name, argv, f"script {rel}")
            break

    # 2. Makefile targets.
    for mk in _MAKEFILE_NAMES:
        path = repo / mk
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_DOC_BYTES]
        except OSError:
            break
        for line in text.splitlines():
            match = _MAKE_TARGET_RE.match(line)
            if match:
                _claim(match.group(1), ["make", match.group(1)], f"Makefile target `{match.group(1)}`")
        break

    # 3. package.json scripts.
    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8", errors="replace")).get("scripts") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for key in scripts:
                if isinstance(key, str):
                    _claim(key, ["npm", "run", key], f"package.json script `{key}`")

    return found


def _command_description(line: str, matched: str) -> str:
    rest = line.replace(matched, " ").replace("`", " ")
    rest = re.sub(r"\s+", " ", rest).strip(" -*—:•\t")
    return rest.strip()[:160]


def discover_operations(repo: Path, root: Path) -> list[dict[str, Any]]:
    """Enumerate candidate operations for a linked repository without running it.

    Three sources, each tagged, in precedence order: operations already in
    ``.virtual-worker/wiki.json`` (``source="manifest"``), shell commands mentioned in the
    repository's AGENTS.md / CLAUDE.md / README docs (``source="instruction"``),
    and setup/sync/check-style commands inferred from repo conventions —
    scripts, Makefile targets, package.json scripts (``source="convention"``).
    Every proposal is round-tripped through ``normalize_operations`` and dropped
    if it does not survive, so callers can save one straight through
    ``upsert_local_operation``.  Earlier sources win on a name clash.
    """
    proposals: dict[str, dict[str, Any]] = {}   # name -> {"proposal", "source"}

    manifest, _err = _manifest(repo)
    manifest_ops = manifest.get("operations") if isinstance(manifest.get("operations"), dict) else {}
    for name, spec in manifest_ops.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        proposals[name] = {"source": "manifest", "proposal": {
            "name": name,
            "description": str(spec.get("description") or ""),
            "argv": spec.get("argv"),
            "inputs": spec.get("inputs") or {},
            "mode": spec.get("mode", "explicit_user"),
            "kind": spec.get("kind", "maintenance"),
        }}

    # instruction_paths collects AGENTS.md + CLAUDE.md at each scope; also mine
    # README-style docs, where setup/sync/bootstrap commands are documented far
    # more often than in the agent instruction files. The lifecycle filter
    # downstream keeps only setup/sync/check from whatever is mined here.
    doc_paths = list(instruction_paths(repo, root))
    seen_docs = {p.resolve() for p in doc_paths}
    for base in (root, repo):
        for fname in ("README.md", "Readme.md", "readme.md", "README"):
            cand = base / fname
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if cand.is_file() and resolved not in seen_docs:
                doc_paths.append(cand)
                seen_docs.add(resolved)
    read_bytes = 0
    for path in doc_paths[:_MAX_DOC_FILES]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        read_bytes += len(text.encode("utf-8", errors="replace"))
        if read_bytes > _MAX_DOC_BYTES:
            break
        for line in text.splitlines():
            for match in _KB_CMD_RE.finditer(line):
                exe = match.group(1)
                tokens = match.group(2).split()
                quoted = match.group(3) if match.group(3) is not None else match.group(4)
                if not tokens:
                    continue
                verb = tokens[0]
                name = "-".join(tokens)
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                    continue
                # Only lifecycle commands are worth registering (bootstrap→setup,
                # update→sync, lint→maintenance, …). Read-only doc commands (query,
                # recall, sym, …) are skipped — the agent runs those directly under
                # repo tooling; they need no registered operation.
                kind = _LIFECYCLE_KIND.get(name)
                if kind is None:
                    continue
                # Manifest is authoritative; never let a doc mention override it.
                if proposals.get(name, {}).get("source") == "manifest" or name in proposals:
                    continue
                argv = [exe, *tokens]
                inputs: dict[str, Any] = {}
                if quoted is not None:
                    placeholder = "question" if verb == "query" else "query"
                    argv.append("{" + placeholder + "}")
                    inputs = {placeholder: {"type": "string", "maxLength": 500}}
                proposals[name] = {"source": "instruction", "proposal": {
                    "name": name,
                    "description": _command_description(line, match.group(0)),
                    "argv": argv,
                    "inputs": inputs,
                    "mode": _guess_mode(verb),
                    "kind": kind,
                }}

    # Conventional lifecycle commands (scripts / Makefile / package.json) fill in
    # setup/sync/check the repo did not spell out — manifest and instruction win.
    for name, entry in _convention_operations(repo).items():
        proposals.setdefault(name, entry)

    out: list[dict[str, Any]] = []
    for name, entry in proposals.items():
        normalized = normalize_operations({"operations": {name: entry["proposal"]}}, repo)
        if name in normalized:
            out.append({**normalized[name], "source": entry["source"]})
    return out


def build_argv(operation: dict[str, Any], inputs: dict[str, Any]) -> list[str]:
    """Substitute typed inputs into an operation's argv template.  Each declared
    ``{name}`` placeholder becomes exactly one argv element carrying the caller's
    string value — never a shell fragment.  Shared by the synchronous
    ``run_operation`` and the async job runner so substitution lives in one
    place."""
    values = inputs if isinstance(inputs, dict) else {}
    argv: list[str] = []
    for piece in operation["argv"]:
        match = re.fullmatch(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", piece)
        if not match:
            argv.append(piece)
            continue
        key = match.group(1)
        spec = operation["inputs"].get(key) or {}
        value = values.get(key)
        if not isinstance(value, str) or not value or len(value) > int(spec.get("maxLength", 500)):
            raise LinkedWikiError(f"operation input {key!r} must be a non-empty string within its configured limit")
        argv.append(value)
    return argv


def resolve_cwd(repo: Path, operation: dict[str, Any]) -> Path:
    """The confined working directory for an operation (always under ``repo``)."""
    return _inside(repo, repo / operation["cwd"])


def run_operation(repo: Path, operation: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    argv = build_argv(operation, inputs)
    cwd = resolve_cwd(repo, operation)
    try:
        completed = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=operation["timeoutSeconds"], check=False)
        output = completed.stdout[:operation["outputLimitBytes"]]
        return {"argv": argv, "cwd": str(cwd), "exitCode": completed.returncode, "output": output,
                "truncated": len(completed.stdout) > len(output)}
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "")[:operation["outputLimitBytes"]]
        return {"argv": argv, "cwd": str(cwd), "exitCode": None, "output": output, "timedOut": True}
    except OSError as exc:
        raise LinkedWikiError(f"operation could not start: {exc}") from exc
