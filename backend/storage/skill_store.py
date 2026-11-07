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

import hashlib
import io
import logging
import os
import shutil
import zipfile
from pathlib import Path

import config
from storage import skill_lib

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return config.ensure_dir(config.APP_HOME / "skills")


def _slugify(name: str) -> str:
    return skill_lib.slugify(name)


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---`` frontmatter block from the markdown body.

    Delegates to :func:`skill_lib.parse_frontmatter_text` — the shared parser
    (scalar values, plus ``list[str]`` for inline/block lists like
    ``requires_tools``). Keys are lowercased; quotes stripped.
    No frontmatter -> ``({}, text)``.
    """
    return skill_lib.parse_frontmatter_text(text)


def _unquote(val: str) -> str:
    return skill_lib._strip_quotes(val)


def _meta_to_skill(slug: str, meta: dict, body: str) -> dict:
    when = meta.get("when_to_use") or meta.get("whentouse") or meta.get("when")
    # Dual-mode skills (docs/notes/log-skill-dual-mode-plan.md): the parser
    # yields list[str] for inline lists and str for scalars — normalize both.
    req = meta.get("requires_connectors")
    requires_connectors = req if isinstance(req, list) else ([req] if req else [])
    web_auth_domains = skill_lib.as_list(meta.get("web_auth_domains"))
    connector_native = str(meta.get("connector_native") or "").lower() in ("true", "1", "yes")
    return {
        "slug": slug,
        "name": meta.get("name") or slug,
        "description": meta.get("description") or "",
        "whenToUse": when or "",
        "version": meta.get("version") or "1.0.0",
        "requiresConnectors": requires_connectors,
        "connectorNative": connector_native,
        "webAuthDomains": web_auth_domains,
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


def _scan(base: Path) -> list[dict]:
    """Every ``<base>/<slug>/SKILL.md`` pack's metadata, sorted by name. Shared by
    the Virtual Worker library scan and the read-only repo-local scan."""
    out: list[dict] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        out.append(_meta_to_skill(d.name, meta, body))
    out.sort(key=lambda s: s["name"].lower())
    return out


def list_skills() -> list[dict]:
    """Scan the skills dir and return each skill's metadata (sorted by name)."""
    out = _scan(_skills_dir())
    for s in out:
        s["source"] = "user"
    return out


# --------------------------------------------------------------------------- #
# Read-only repo-local skills (linked-wiki revamp §7) — a linked repo can ship
# `.claude/skills/*/SKILL.md` / `.vw/skills/*/SKILL.md`. These are surfaced,
# never copied (they update with `git pull`), so only read variants exist.
# --------------------------------------------------------------------------- #

def list_skills_in(root: Path) -> list[dict]:
    """Skill metadata for packs under an arbitrary ``root`` (e.g. a linked repo's
    ``.claude/skills``). Read-only; never writes."""
    return _scan(Path(root))


def read_skill_in(root: Path, slug: str) -> dict | None:
    """Full skill (metadata + body + resource file names) from ``<root>/<slug>``.
    ``None`` if it has no SKILL.md."""
    base = Path(root) / slug
    try:
        text = (base / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    skill = _meta_to_skill(slug, meta, body)
    skill["body"] = body
    skill["raw"] = text
    skill["files"] = [str(f.relative_to(base)) for f in sorted(base.rglob("*"))
                      if f.is_file() and f.name != "SKILL.md"]
    return skill


def read_resource_in(root: Path, slug: str, path: str) -> str | None:
    """Read one bundled resource of a repo-local skill. Path-traversal is rejected
    (same guard as :func:`read_resource`); SKILL.md is not a resource."""
    base = (Path(root) / slug).resolve()
    target = (base / (path or "")).resolve()
    try:
        target.relative_to(base)
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
# Builtin skill packs (shipped with the app) — discover + install.
#
# Packs live under ``config.builtin_skills_dir()`` (repo ``skills/`` in dev, the
# frozen bundle in the packaged app) and are NEVER mutated in place: installing
# copies the pack (plus its ``requires_skills`` closure) into the Virtual Worker library,
# so user edits/deletes stay safe and installs survive app upgrades.
# --------------------------------------------------------------------------- #

def _is_external_pack(d: Path) -> bool:
    """External-session packs (frontmatter ``audience: external``) are loaded
    by an outside coding session (Claude Code/Codex) to reach into Virtual Worker — never
    by a Virtual Worker agent. Keep them out of every builtin surface (gallery, install,
    update-all); manual ``.zip`` upload stays the deliberate escape hatch."""
    meta = skill_lib.parse_frontmatter(d / "SKILL.md")
    return str(meta.get("audience") or "").strip().lower() == "external"


def _builtin_packs() -> tuple[Path | None, dict[str, Path]]:
    root = config.builtin_skills_dir()
    packs = skill_lib.discover_skills(root) if root else {}
    return root, {name: d for name, d in packs.items()
                  if not _is_external_pack(d)}


def _pack_fingerprint(d: Path) -> str:
    """Content fingerprint of a skill pack tree: sha256 over sorted
    ``(relative posix path, file bytes)`` of every file, skipping hidden files
    and dirs (names starting with "."). A version-agnostic drift signal — a real
    fix can ship without bumping ``version``, so compare bytes, not frontmatter.
    Best-effort: any OSError raises to the caller, which treats it as "no update
    signal" (see :func:`list_builtin`)."""
    h = hashlib.sha256()
    files = [f for f in sorted(d.rglob("*"))
             if f.is_file() and not any(part.startswith(".") for part in f.relative_to(d).parts)]
    for f in files:
        h.update(f.relative_to(d).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def list_builtin() -> list[dict]:
    """Metadata for every shipped pack, with dependency + install state.

    Each entry: the standard skill meta (including ``webAuthDomains``) + ``category`` (the intermediate dir),
    ``requiresTools``/``requiresSkills`` from frontmatter, ``toolsAvailable``
    (per-tool PATH check), and ``installed`` (slug present in the Virtual Worker library).
    """
    root, packs = _builtin_packs()
    if not root:
        return []
    installed = {s["slug"] for s in _scan(_skills_dir())}
    out: list[dict] = []
    for name, d in packs.items():
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        skill = _meta_to_skill(name, meta, body)
        tools = skill_lib.as_list(meta.get("requires_tools"))
        skill["category"] = skill_lib.skill_category(root, d)
        skill["requiresTools"] = tools
        skill["requiresSkills"] = skill_lib.as_list(meta.get("requires_skills"))
        skill["toolsAvailable"] = {t: shutil.which(t) is not None for t in tools}
        skill["installed"] = name in installed
        skill["source"] = "builtin"
        if skill["installed"]:
            copy = _skills_dir() / name
            try:
                imeta, _ = parse_frontmatter((copy / "SKILL.md").read_text(encoding="utf-8"))
                skill["installedVersion"] = imeta.get("version") or "1.0.0"
                skill["updateAvailable"] = _pack_fingerprint(d) != _pack_fingerprint(copy)
            except OSError:
                # Best-effort: a read/hash failure is not an update signal.
                skill["installedVersion"] = None
                skill["updateAvailable"] = False
        out.append(skill)
    out.sort(key=lambda s: (s["category"], s["name"].lower()))
    return out


def install_builtin(slug: str) -> dict:
    """Copy a builtin pack (and its ``requires_skills`` closure) into the Virtual Worker
    skills library. Re-install overwrites (that's the update path after the
    shipped packs change). ``domainGrants`` reports the ``web_auth_domains``
    each installed pack declares — informational, for the install notice: the
    runtime scopes the webauth tools per-run from the attached skills, so no
    grant record is written anywhere. Returns
    ``{installed: [...], missingTools: [...], domainGrants: [...]}``.
    Raises ``ValueError`` on an unknown slug."""
    _root, packs = _builtin_packs()
    if slug not in packs:
        every = skill_lib.discover_skills(_root) if _root else {}
        if slug in every and _is_external_pack(every[slug]):
            raise ValueError(
                f"{slug!r} is an external-session skill (audience: external) — "
                "it is loaded by an outside coding session via external CLI, not "
                "by a Virtual Worker agent; see skills/README.md")
        raise ValueError(f"no builtin skill {slug!r}")
    try:
        plan = skill_lib.resolve_deps([slug], packs)
    except KeyError as exc:
        raise ValueError(f"builtin skill {slug!r} requires unknown skill "
                         f"{exc.args[0]!r}") from exc
    dest_root = _skills_dir()
    installed: list[str] = []
    missing: list[str] = []
    domain_grants: list[dict] = []
    for name in plan:
        src = packs[name]
        meta = skill_lib.parse_frontmatter(src / "SKILL.md")
        for t in skill_lib.as_list(meta.get("requires_tools")):
            if shutil.which(t) is None and t not in missing:
                missing.append(t)
        dest = dest_root / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        installed.append(name)
        domains = skill_lib.as_list(meta.get("web_auth_domains"))
        if domains:
            domain_grants.append({"skill": name, "domains": domains})
    logger.info("installed builtin skills %s (missing tools: %s)", installed, missing)
    return {"installed": installed, "missingTools": missing,
            "domainGrants": domain_grants}


def update_all_builtin() -> dict:
    """Re-install every installed builtin pack whose shipped copy has drifted
    (same fingerprint check as :func:`list_builtin`). Packs that are up to date
    are left untouched, so unrelated user edits aren't clobbered. Returns
    ``{updated: [...], missingTools: [...], domainGrants: [...]}``."""
    _root, packs = _builtin_packs()
    installed = {s["slug"] for s in _scan(_skills_dir())}
    updated: list[str] = []
    missing: list[str] = []
    domain_grants: list[dict] = []
    for name, src in packs.items():
        if name not in installed:
            continue
        try:
            if _pack_fingerprint(src) == _pack_fingerprint(_skills_dir() / name):
                continue
        except OSError:
            continue  # unreadable copy — no update signal, skip
        try:
            r = install_builtin(name)
        except ValueError:
            continue  # unresolvable requires_skills — skip, don't fail the batch
        for s in r["installed"]:
            if s not in updated:
                updated.append(s)
        for t in r["missingTools"]:
            if t not in missing:
                missing.append(t)
        for grant in r.get("domainGrants", []):
            if grant not in domain_grants:
                domain_grants.append(grant)
    return {"updated": updated, "missingTools": missing,
            "domainGrants": domain_grants}


def install_from_dir(src: Path, slug: str) -> dict:
    """Install a skill pack from a local directory.

    The source is expected to be a registry clone's ``skills/<slug>/`` pack.
    It is copied into the Virtual Worker library with the same overwrite semantics as a
    builtin install.  Source and destination paths are checked before copying
    so symlink escapes and unsafe slugs cannot cross either boundary.
    """
    if (not isinstance(slug, str) or not slug or os.sep in slug
            or (os.altsep and os.altsep in slug) or ".." in slug
            or any(part in ("", ".", "..") for part in Path(slug).parts)):
        raise ValueError(f"unsafe skill slug {slug!r}")

    source = Path(src)
    try:
        source = source.resolve()
    except OSError as exc:
        raise ValueError(f"unsafe skill source {src!r}") from exc
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise ValueError("skill directory has no SKILL.md")

    # Validate every entry, including symlinked directories (os.walk does not
    # follow those when followlinks=False). Internal links are safe to copy;
    # links resolving outside the pack are rejected before any destination is
    # removed or written.
    for current, dirs, files in os.walk(source, followlinks=False):
        for name in [*dirs, *files]:
            entry = Path(current) / name
            try:
                entry.resolve().relative_to(source)
            except (OSError, ValueError) as exc:
                raise ValueError(f"unsafe path in skill directory: {entry}") from exc

    dest_root = _skills_dir().resolve()
    dest = (dest_root / slug).resolve()
    try:
        dest.relative_to(dest_root)
    except ValueError as exc:
        raise ValueError(f"unsafe skill destination {slug!r}") from exc

    meta = skill_lib.parse_frontmatter(source / "SKILL.md")
    missing: list[str] = []
    for tool in skill_lib.as_list(meta.get("requires_tools")):
        if shutil.which(tool) is None and tool not in missing:
            missing.append(tool)

    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
    except (shutil.Error, OSError) as exc:
        # A registry clone can preserve dangling or non-regular symlinks. Do
        # not leave a half-installed pack behind when copytree rejects one.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise ValueError(f"could not install skill pack {slug!r}: {exc}") from exc
    logger.info("installed registry skill %s (missing tools: %s)", slug, missing)
    return {"installed": [slug], "missingTools": missing}


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


def export_zip(slug: str) -> bytes | None:
    """Pack a skill folder into an in-memory ``.zip`` for download / sharing.

    Files are nested under a top-level ``<slug>/`` folder so the archive unpacks
    tidily and round-trips straight back through :func:`save_zip` (which locates
    SKILL.md at any depth). Returns ``None`` if the slug isn't a real skill.
    Path-traversal in ``slug`` is rejected."""
    root = _skills_dir().resolve()
    base = (root / slug).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return None  # slug escaped the skills dir
    if base == root or not base.is_dir() or _read_skill_md(slug) is None:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(base.rglob("*")):
            if f.is_file():
                zf.write(f, f"{slug}/{f.relative_to(base).as_posix()}")
    return buf.getvalue()


def save_upload(filename: str, data: bytes) -> dict:
    """Dispatch by extension: ``.zip`` -> bundle, else treat as markdown."""
    if filename.lower().endswith(".zip"):
        return save_zip(filename, data)
    return save_markdown(filename, data)


def _fm_value(v: str) -> str:
    """Normalize a frontmatter value to a single line so it always round-trips
    through ``parse_frontmatter`` (collapses any newlines/runs of whitespace)."""
    return " ".join((v or "").split())


def _compose_skill_md(name: str, description: str, when_to_use: str,
                      version: str, body: str) -> str:
    """Build a spec-compliant SKILL.md (frontmatter + body) from fields."""
    lines = ["---", f"name: {_fm_value(name)}"]
    if description.strip():
        lines.append(f"description: {_fm_value(description)}")
    if when_to_use.strip():
        lines.append(f"when_to_use: {_fm_value(when_to_use)}")
    lines.append(f"version: {_fm_value(version) or '1.0.0'}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


MAX_SKILL_BODY = 40000


def write_skill(name: str, body: str, *, description: str = "", when_to_use: str = "",
                version: str = "1.0.0", slug: str | None = None,
                overwrite: bool = False) -> dict:
    """Programmatically author a skill pack (the code-path twin of an upload).

    Composes valid frontmatter + body and writes ``SKILL.md`` under
    ``<VW_HOME>/skills/<slug>/`` (slug from ``slug`` or ``name`` via
    ``_slugify``, so a crafted name can't escape the skills dir). Refuses to
    clobber an existing slug unless ``overwrite``. Raises ``ValueError`` on a
    missing name/body, an over-cap body, or a collision. Used by the agent-facing
    skill factory (PRD-14) — writes stay confined to the skills dir, no egress."""
    if not (name or "").strip():
        raise ValueError("skill name is required")
    if not (body or "").strip():
        raise ValueError("skill body is required")
    if len(body) > MAX_SKILL_BODY:
        raise ValueError(f"skill body too long ({len(body)} > {MAX_SKILL_BODY} chars)")
    slug = _slugify(slug or name)
    dest = _skills_dir() / slug
    if dest.exists() and not overwrite:
        raise ValueError(
            f"a skill with slug {slug!r} already exists — refine it with update_skill "
            "(or pass overwrite to replace it)")
    text = _compose_skill_md(name, description, when_to_use, version, body)
    config.ensure_dir(dest)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return get_skill(slug)  # type: ignore[return-value]


def delete_skill(slug: str) -> bool:
    d = _skills_dir() / slug
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
