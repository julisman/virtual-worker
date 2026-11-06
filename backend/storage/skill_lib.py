"""Shared skill-pack core — format parsing, repo discovery, dependency resolution.

Single source of truth for the agentskills.io ``SKILL.md`` handling used by both
the Virtual Worker backend (:mod:`storage.skill_store`) and the standalone ``bin/ves-skill``
installer CLI (which imports this module by file path — keep it **stdlib-only**
and free of ``config``/app imports so the CLI runs under any Python).

Layout conventions:
  - a skill is a folder containing ``SKILL.md`` (YAML-ish frontmatter + body)
  - a repo ships packs at ``skills/<name>/`` or ``skills/<category>/<name>/``
  - frontmatter deps: ``requires_skills: [a, b]`` / ``requires_tools: [x]``
    (inline lists or block lists) — ``requires_tools`` names uv-installable
    packages under the repo's ``tools/`` dir
"""

from __future__ import annotations

import re
from pathlib import Path


# ------------------------------------------------------------------ frontmatter


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_frontmatter_text(text: str) -> tuple[dict, str]:
    """Split a ``---`` frontmatter block from the markdown body.

    Returns ``(meta, body)``. Values are strings, or ``list[str]`` for inline
    lists (``key: [a, b]``) and block lists (``key:`` followed by ``- item``
    lines). Folded continuation lines append to the previous scalar. ``>``/``|``
    block scalars are folded into one string. No frontmatter -> ``({}, text)``.
    """
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)

    data: dict = {}
    current_list_key: str | None = None
    block_key: str | None = None
    block_lines: list[str] = []

    def flush_block() -> None:
        nonlocal block_key, block_lines
        if block_key is not None:
            data[block_key] = " ".join(l.strip() for l in block_lines if l.strip())
        block_key, block_lines = None, []

    for raw in block.splitlines():
        line = raw.rstrip()
        if block_key is not None:
            if not line.strip() or raw[:1] in (" ", "\t"):
                block_lines.append(line)
                continue
            flush_block()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_list_key:
            data[current_list_key].append(_strip_quotes(stripped[2:]))
            continue
        mkv = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not mkv or raw[:1] in (" ", "\t"):
            # indented continuation of a scalar — fold into previous value
            if current_list_key is None and data:
                last = next(reversed(data))
                if isinstance(data[last], str):
                    data[last] = (data[last] + " " + stripped).strip()
            continue
        key = mkv.group(1).strip().lower()
        val = mkv.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            current_list_key = None
            block_key, block_lines = key, []
            continue
        if val == "":
            # possibly a block list follows
            current_list_key = key
            data[key] = []
            continue
        current_list_key = None
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            items = [_strip_quotes(p) for p in inner.split(",")] if inner else []
            data[key] = [i for i in items if i]
        else:
            data[key] = _strip_quotes(val)
    flush_block()
    return data, body


def parse_frontmatter(path: Path) -> dict:
    """Frontmatter dict for a SKILL.md file ({} on missing/unreadable)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    meta, _ = parse_frontmatter_text(text)
    return meta


def as_list(v) -> list[str]:
    """Normalize a frontmatter value to ``list[str]`` (list, ``[a, b]`` str, or
    comma-separated str all accepted)."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [p.strip() for p in s.split(",") if p.strip()]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "skill"


# ------------------------------------------------------------------- discovery


def discover_skills(root: Path) -> dict[str, Path]:
    """name -> skill dir, for packs at ``<root>/<name>/SKILL.md`` and
    ``<root>/<category>/<name>/SKILL.md`` (first match wins per name)."""
    root = Path(root)
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "SKILL.md").is_file():
            found.setdefault(child.name, child)
            continue
        for grand in sorted(child.iterdir()):
            if grand.is_dir() and (grand / "SKILL.md").is_file():
                found.setdefault(grand.name, grand)
    return found


def skill_category(root: Path, skill_dir: Path) -> str:
    """Category = the intermediate dir under ``root`` ("" for top-level packs)."""
    try:
        rel = Path(skill_dir).relative_to(Path(root))
    except ValueError:
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


# ------------------------------------------------------------------ dependency


def resolve_deps(names: list[str], skills: dict[str, Path],
                 skip_unknown: bool = False) -> list[str]:
    """Transitive ``requires_skills`` closure, dedup, cycle-safe, deps before
    dependents. Raises ``KeyError`` on an unknown name unless ``skip_unknown``
    (used for stale lock-file entries)."""
    order: list[str] = []
    seen: set[str] = set()
    stack: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in stack:  # cycle — stop descending
            return
        d = skills.get(name)
        if d is None:
            if skip_unknown:
                return
            raise KeyError(name)
        stack.add(name)
        fm = parse_frontmatter(d / "SKILL.md")
        for dep in as_list(fm.get("requires_skills")):
            visit(dep)
        stack.discard(name)
        seen.add(name)
        order.append(name)

    for n in names:
        visit(n)
    return order
