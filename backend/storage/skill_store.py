"""On-disk skill packs (agentskills.io ``SKILL.md`` spec), local-first.

A skill is a folder under ``<VW_HOME>/skills/<slug>/`` containing a
``SKILL.md`` (YAML frontmatter + markdown body) plus any bundled resource files.
Agents attach skills by slug; only each skill's *name/description/when_to_use*
go into the system prompt — the full body is pulled on demand via the
``load_skill`` tool (progressive disclosure, like the openAsetin engine).

Uploads accept either a bare ``.md`` (written as SKILL.md) or a ``.zip`` bundle
(must contain a SKILL.md; extra files are kept as resources). Frontmatter is
parsed with a tiny dependency-free reader — simple ``key: value`` lines, enough
for ``name``/``description``/``when_to_use``.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return config.ensure_dir(config.APP_HOME / "skills")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "skill"


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---`` YAML-ish frontmatter block from the markdown body.

    Returns ``(meta, body)``. Supports simple ``key: value`` pairs (the keys we
    need: name/description/when_to_use); unknown keys are kept as raw strings.
    Quotes around values are stripped. No frontmatter -> ``({}, text)``.
    """
    if not text.startswith("---"):
        return {}, text
    # Match an opening --- line, the block, and a closing --- line.
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    key = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        mkv = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if mkv and not raw.startswith((" ", "\t")):
            key = mkv.group(1).strip().lower()
            val = mkv.group(2).strip()
            meta[key] = _unquote(val)
        elif key is not None:
            # Folded continuation line for the previous key.
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, body


def _unquote(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def _meta_to_skill(slug: str, meta: dict, body: str) -> dict:
    when = meta.get("when_to_use") or meta.get("whentouse") or meta.get("when")
    return {
        "slug": slug,
        "name": meta.get("name") or slug,
        "description": meta.get("description") or "",
        "whenToUse": when or "",
        "version": meta.get("version") or "1.0.0",
    }


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

def _read_skill_md(slug: str) -> str | None:
    p = _skills_dir() / slug / "SKILL.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def list_skills() -> list[dict]:
    """Scan the skills dir and return each skill's metadata (sorted by name)."""
    out: list[dict] = []
    for d in sorted(_skills_dir().iterdir() if _skills_dir().exists() else []):
        if not d.is_dir():
            continue
        text = _read_skill_md(d.name)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        out.append(_meta_to_skill(d.name, meta, body))
    out.sort(key=lambda s: s["name"].lower())
    return out


def get_skill(slug: str) -> dict | None:
    """Full skill: metadata + the markdown body + bundled resource file names."""
    text = _read_skill_md(slug)
    if text is None:
        return None
    meta, body = parse_frontmatter(text)
    skill = _meta_to_skill(slug, meta, body)
    skill["body"] = body
    skill["raw"] = text
    files = []
    base = _skills_dir() / slug
    for f in sorted(base.rglob("*")):
        if f.is_file() and f.name != "SKILL.md":
            files.append(str(f.relative_to(base)))
    skill["files"] = files
    return skill


def skill_body(slug: str) -> str | None:
    """The markdown body only (what ``load_skill`` injects)."""
    text = _read_skill_md(slug)
    if text is None:
        return None
    _, body = parse_frontmatter(text)
    return body.strip()


def list_resources(slug: str) -> list[dict]:
    """L3 resources: every bundled file under the skill folder except SKILL.md,
    as ``{path, size}`` (path relative to the skill folder). By convention these
    live under ``references/`` / ``assets/`` / ``scripts/`` (like the openAsetin
    engine), but any bundled file is listed."""
    base = _skills_dir() / slug
    if not base.exists():
        return []
    out = []
    for f in sorted(base.rglob("*")):
        if f.is_file() and f.name != "SKILL.md":
            out.append({"path": str(f.relative_to(base)), "size": f.stat().st_size})
    return out


def read_resource(slug: str, path: str) -> str | None:
    """Read one L3 resource file's text. Path-traversal is rejected; SKILL.md is
    not a resource. Binary files return a short placeholder. ``None`` = missing."""
    base = (_skills_dir() / slug).resolve()
    target = (base / (path or "")).resolve()
    try:
        target.relative_to(base)  # raises if path escapes the skill folder
    except ValueError:
        return None
    if target.name == "SKILL.md" or not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return f"(binary file, {target.stat().st_size} bytes — not shown as text)"
        except OSError:
            return None


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

def save_markdown(filename: str, data: bytes) -> dict:
    """Save a bare ``.md`` upload as a skill. Slug from frontmatter name or file."""
    text = data.decode("utf-8", errors="replace")
    meta, _ = parse_frontmatter(text)
    slug = _slugify(meta.get("name") or Path(filename).stem)
    dest = _skills_dir() / slug
    config.ensure_dir(dest)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return get_skill(slug)  # type: ignore[return-value]


def save_zip(filename: str, data: bytes) -> dict:
    """Extract a ``.zip`` bundle. Must contain a SKILL.md (at any depth).

    Guards against path traversal. The directory holding SKILL.md becomes the
    skill root; its siblings are kept as resources. Slug from frontmatter name
    or the zip filename.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid zip: {exc}") from exc

    names = [n for n in zf.namelist() if not n.endswith("/")]
    skill_md = next((n for n in names if Path(n).name == "SKILL.md"), None)
    if not skill_md:
        raise ValueError("zip has no SKILL.md")
    root = str(Path(skill_md).parent)  # "" if SKILL.md at top level
    root_prefix = "" if root in ("", ".") else root.rstrip("/") + "/"

    md_text = zf.read(skill_md).decode("utf-8", errors="replace")
    meta, _ = parse_frontmatter(md_text)
    slug = _slugify(meta.get("name") or Path(filename).stem)
    dest = _skills_dir() / slug
    if dest.exists():
        shutil.rmtree(dest)
    config.ensure_dir(dest)

    for n in names:
        if root_prefix and not n.startswith(root_prefix):
            continue
        rel = n[len(root_prefix):] if root_prefix else n
        if not rel or rel.endswith("/"):
            continue
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest.resolve())):
            raise ValueError(f"unsafe path in zip: {n}")
        config.ensure_dir(target.parent)
        target.write_bytes(zf.read(n))
    return get_skill(slug)  # type: ignore[return-value]


def save_upload(filename: str, data: bytes) -> dict:
    """Dispatch by extension: ``.zip`` -> bundle, else treat as markdown."""
    if filename.lower().endswith(".zip"):
        return save_zip(filename, data)
    return save_markdown(filename, data)


def delete_skill(slug: str) -> bool:
    d = _skills_dir() / slug
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
