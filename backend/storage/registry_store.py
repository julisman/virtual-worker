"""Git-backed, read-only template registries.

Registries are deliberately small records in ``settings.json``.  Git remains the
source of truth for URL registries; a local registry is simply rescanned when
it is synchronized.  All git invocations are bounded and non-interactive so a
bad URL or credential can never leave the backend waiting for input.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import config
from storage import settings, template_manifest


_SETTINGS_KEY = "registries"
_lock = threading.Lock()
_reserved_slugs: set[str] = set()
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
}
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class RegistryError(ValueError):
    def __init__(self, message, code="registry_error", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _all() -> dict[str, dict]:
    records = settings.get(_SETTINGS_KEY, {}) or {}
    return records if isinstance(records, dict) else {}


def _save(records: dict[str, dict]) -> None:
    settings.set(_SETTINGS_KEY, records)


def _git_timeout() -> float:
    try:
        value = float(os.environ.get("VW_REGISTRY_GIT_TIMEOUT", "60"))
    except (TypeError, ValueError):
        value = 60.0
    return max(0.01, value)


def _max_bytes() -> int:
    try:
        value = int(os.environ.get("VW_REGISTRY_MAX_BYTES", str(100 * 1024 * 1024)))
    except (TypeError, ValueError):
        value = 100 * 1024 * 1024
    return max(0, value)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return env


def _git(args: list[str], *, action: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            timeout=_git_timeout(),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RegistryError(f"git operation timed out ({action})", code="timeout") from exc
    except OSError as exc:
        raise RegistryError(f"could not run git ({action}): {exc}") from exc
    return result


def _git_failure(result: subprocess.CompletedProcess[str], action: str,
                 branch: str | None = None) -> RegistryError:
    detail = (result.stderr or result.stdout or "git returned a non-zero status").strip()
    detail = re.sub(r"\s+", " ", detail)
    if len(detail) > 500:
        detail = detail[:497] + "..."
    ref_error = any(
        marker in detail.lower()
        for marker in (
            "remote branch",
            "couldn't find remote ref",
            "could not find remote branch",
            "invalid ref",
            "did not match any",
        )
    )
    code = "bad_ref" if branch and ref_error else "registry_error"
    if code == "bad_ref":
        message = f"invalid or unavailable branch {branch!r}: {detail}"
    else:
        message = f"git {action} failed: {detail}"
    return RegistryError(message, code=code)


def _validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    branch = str(branch).strip()
    if not branch or not _BRANCH_RE.fullmatch(branch) or branch.startswith("-") or ".." in branch:
        raise RegistryError("invalid branch name", code="bad_ref")
    return branch


def _slugify(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return value or "registry"


def _repo_name(url: str) -> str:
    raw = url.rstrip("/")
    if raw.startswith("git@") and ":" in raw:
        name = raw.rsplit(":", 1)[1].rsplit("/", 1)[-1]
    else:
        parsed = urlparse(raw)
        name = Path(parsed.path if parsed.scheme else raw).name
    return name.removesuffix(".git") or "registry"


def _unique_slug(base: str, records: dict[str, dict], *, include_reserved: bool = True) -> str:
    root = config.APP_HOME / "registries"
    slug = base
    index = 2
    while (slug in records or (root / slug).exists()
           or (include_reserved and slug in _reserved_slugs)):
        slug = f"{base}-{index}"
        index += 1
    return slug


def _reserve_slug(base: str) -> str:
    """Reserve a destination slug without holding the lock over git work."""
    with _lock:
        slug = _unique_slug(base, _all())
        _reserved_slugs.add(slug)
        return slug


def _managed_root() -> Path:
    return config.APP_HOME / "registries"


def _managed_path(slug: str) -> Path:
    root = _managed_root().resolve()
    path = (root / slug).resolve()
    if not path.is_relative_to(root):
        raise RegistryError("invalid registry slug")
    return path


def registry_clone_dir(slug: str) -> Path:
    """Return the checkout path for a registry slug."""
    record = _all().get(slug) or {}
    if record.get("kind") == "local":
        return Path(record.get("path") or record.get("url") or "")
    return _managed_root() / slug


# The distinctive default content directory (FR-12 amendment): a repo that
# holds more than Virtual Worker templates keeps them under <root>/vw-registry/ so the
# name can't collide with a generic "templates" folder used for other things.
DEFAULT_SUBDIR = "vw-registry"


def _validate_subdir(subdir: str | None) -> str | None:
    """Normalize an optional content subdirectory ('' -> None, 'a/b' kept).

    Relative, forward-slash paths only; no absolute paths, no '..', no
    backslashes, no empty/dot parts — the same containment spirit as FR-12a.
    """
    value = str(subdir or "").strip()
    if not value:
        return None
    if "\\" in value or value.startswith("/"):
        raise RegistryError("subdirectory must be a relative path", code="bad_subdir")
    value = value.strip("/")
    if not value:
        raise RegistryError("subdirectory must be a relative path", code="bad_subdir")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RegistryError("subdirectory must not contain '..' or empty parts",
                            code="bad_subdir")
    return "/".join(parts)


def _content_root(root: Path, record: dict) -> Path:
    """Where templates/ and skills/ live inside a checkout.

    Explicit per-registry ``subdir`` wins; otherwise the distinctive default
    ``vw-registry/`` is used when present, falling back to the repo root (the
    dedicated-template-repo / scaffold layout).
    """
    subdir = record.get("subdir")
    if subdir:
        return root / Path(subdir)
    default = root / DEFAULT_SUBDIR
    if _path_is_safe(root, default / "templates") and (default / "templates").is_dir():
        return default
    return root


def _path_is_safe(root: Path, candidate: Path) -> bool:
    """Ensure candidate is contained and has no symlink in its path."""
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve(strict=False)
        relative = candidate_resolved.relative_to(root_resolved)
        # Check the original lexical path as well as the resolved path.  A
        # symlink pointing back *inside* the root is still disallowed by the
        # registry hygiene contract.
        root_lexical = root.absolute()
        candidate_lexical = candidate.absolute()
        lexical_relative = candidate_lexical.relative_to(root_lexical)
    except (OSError, ValueError):
        return False
    current = root_lexical
    for part in lexical_relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
    return True


def _tree_size(root: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _remove_managed_clone(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_relative_to(_managed_root().resolve()):
            return
    except (OSError, ValueError):
        return
    if path.exists():
        shutil.rmtree(path)


def _scan_registry(slug: str, record: dict) -> tuple[list[dict], int]:
    # During add, the record is not persisted yet; use its path directly so a
    # local registry is scanned before the first settings write as well.
    root = (Path(record.get("path") or record.get("url") or "")
            if record.get("kind") == "local" else registry_clone_dir(slug))
    if not root.is_dir():
        return [], 0
    content = _content_root(root, record)
    templates_root = content / "templates"
    out: list[dict] = []
    if _path_is_safe(root, templates_root) and templates_root.is_dir():
        try:
            children = sorted(templates_root.iterdir(), key=lambda path: path.name)
        except OSError:
            children = []
        for folder in children:
            manifest = folder / "template.json"
            if not folder.is_dir() or not _path_is_safe(root, manifest):
                continue
            item = _normalize_registry_template(slug, folder.name, manifest)
            if item:
                out.append(item)

    skills_count = 0
    skills_root = content / "skills"
    if _path_is_safe(root, skills_root) and skills_root.is_dir():
        try:
            skill_dirs = sorted(skills_root.iterdir(), key=lambda path: path.name)
        except OSError:
            skill_dirs = []
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            if (skill_dir.is_dir() and _path_is_safe(root, skill_md)
                    and skill_md.is_file()):
                skills_count += 1
    return out, skills_count


def _normalize_registry_template(slug: str, folder: str, manifest: Path) -> dict | None:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        pure = template_manifest.validate_and_normalize(data)
    except (OSError, ValueError, template_manifest.ManifestError):
        return None
    # _display_normalize applies the same guardrail floor as user templates.
    from storage import template_store
    norm = template_store._display_normalize(pure, source="registry")
    norm["id"] = f"reg:{slug}:{folder}"
    norm["templateId"] = str(data.get("templateId") or folder)
    norm["sourceId"] = str(data.get("sourceId") or f"registry:{slug}:{folder}")
    norm["schemaVersion"] = int(data.get("schemaVersion", pure.get("schemaVersion", 1)) or 1)
    norm["version"] = str(data.get("version") or pure.get("version") or "1.0.0")
    norm["digest"] = str(data.get("digest") or template_manifest.canonical_digest(pure))
    norm["source"] = "registry"
    norm["registry"] = slug
    return norm


def _commit_record(slug: str, base_slug: str, record: dict,
                   clone_dir: Path | None = None) -> tuple[str, dict]:
    """Persist one record after slow work, re-reading settings under ``_lock``."""
    with _lock:
        records = _all()
        final_slug = slug
        if final_slug in records:
            # This should only be possible if another process edited settings.json;
            # in-process adds reserve slugs before starting their slow work.
            _reserved_slugs.discard(final_slug)
            final_slug = _unique_slug(base_slug, records)
            _reserved_slugs.add(final_slug)
            if clone_dir is not None:
                final_dir = _managed_path(final_slug)
                clone_dir.replace(final_dir)
                clone_dir = final_dir
                record["path"] = str(final_dir)
        record["slug"] = final_slug
        records[final_slug] = record
        _save(records)
        return final_slug, dict(record)


def list_registries() -> list[dict]:
    return [dict(record) for _, record in sorted(_all().items())]


def add_registry(url: str, branch: str | None = None,
                 subdir: str | None = None) -> dict:
    value = str(url or "").strip()
    if not value:
        raise RegistryError("registry URL or local path is required")
    branch = _validate_branch(branch)
    subdir = _validate_subdir(subdir)
    local_path = Path(value)
    if local_path.is_dir():
        base_slug = _slugify(local_path.name)
        slug = _reserve_slug(base_slug)
        try:
            record = {
                "slug": slug,
                "url": value,
                "kind": "local",
                "branch": branch,
                "lastSyncedAt": None,
                "lastError": None,
                "templateCount": 0,
                "skillPackCount": 0,
                "path": value,
                "subdir": subdir,
            }
            templates, skills = _scan_registry(slug, record)
            record["templateCount"] = len(templates)
            record["skillPackCount"] = skills
            record["lastSyncedAt"] = _now()
            committed_slug, committed = _commit_record(slug, base_slug, record)
            slug = committed_slug
            return committed
        finally:
            with _lock:
                _reserved_slugs.discard(slug)

    base_slug = _slugify(_repo_name(value))
    slug = _reserve_slug(base_slug)
    clone_dir = _managed_path(slug)
    try:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags"]
        if branch:
            args.extend(["--branch", branch])
        args.extend([value, str(clone_dir)])
        result = _git(args, action="clone")
        if result.returncode:
            _remove_managed_clone(clone_dir)
            raise _git_failure(result, "clone", branch)
        if _tree_size(clone_dir) > _max_bytes():
            raise RegistryError("registry clone exceeds the configured size limit", code="too_large")
        actual_branch = branch
        if actual_branch is None:
            branch_result = _git(
                ["-C", str(clone_dir), "symbolic-ref", "--short", "HEAD"],
                action="read branch",
            )
            if branch_result.returncode == 0 and branch_result.stdout.strip():
                actual_branch = branch_result.stdout.strip()
        record = {
            "slug": slug,
            "url": value,
            "kind": "url",
            "branch": actual_branch,
            "lastSyncedAt": None,
            "lastError": None,
            "templateCount": 0,
            "skillPackCount": 0,
            "path": str(clone_dir),
            "subdir": subdir,
        }
        templates, skills = _scan_registry(slug, record)
        record["templateCount"] = len(templates)
        record["skillPackCount"] = skills
        record["lastSyncedAt"] = _now()
        committed_slug, committed = _commit_record(slug, base_slug, record, clone_dir)
        slug = committed_slug
        return committed
    except RegistryError:
        _remove_managed_clone(clone_dir)
        raise
    except Exception as exc:
        _remove_managed_clone(clone_dir)
        raise RegistryError(f"could not scan registry: {exc}") from exc
    finally:
        with _lock:
            _reserved_slugs.discard(slug)


def sync_registry(slug: str) -> dict | None:
    with _lock:
        records = _all()
        record = records.get(slug)
    if not record:
        return None
    try:
        if record.get("kind") == "url":
            clone_dir = registry_clone_dir(slug)
            branch = _validate_branch(record.get("branch"))
            fetch_args = ["-C", str(clone_dir), "fetch", "--depth", "1", "--no-tags", "origin"]
            if branch:
                fetch_args.append(branch)
            result = _git(fetch_args, action="sync fetch")
            if result.returncode:
                raise _git_failure(result, "sync fetch", branch)
            target = f"origin/{branch}" if branch else "FETCH_HEAD"
            result = _git(["-C", str(clone_dir), "reset", "--hard", target], action="sync reset")
            if result.returncode:
                raise _git_failure(result, "sync reset", branch)
            if _tree_size(clone_dir) > _max_bytes():
                _remove_managed_clone(clone_dir)
                raise RegistryError("registry clone exceeds the configured size limit", code="too_large")
        templates, skills = _scan_registry(slug, record)
        with _lock:
            records = _all()
            current = records.get(slug)
            if not current:
                return None
            current["templateCount"] = len(templates)
            current["skillPackCount"] = skills
            current["lastSyncedAt"] = _now()
            current["lastError"] = None
            records[slug] = current
            _save(records)
            return dict(current)
    except RegistryError as exc:
        with _lock:
            records = _all()
            current = records.get(slug)
            if current:
                current["lastError"] = str(exc)
                records[slug] = current
                _save(records)
        raise


def remove_registry(slug: str) -> bool:
    with _lock:
        records = _all()
        record = records.get(slug)
    if not record:
        return False
    if record.get("kind") == "url":
        _remove_managed_clone(_managed_path(slug))
    with _lock:
        records = _all()
        if slug not in records:
            return True
        del records[slug]
        _save(records)
        return True


def list_registry_templates() -> list[dict]:
    out: list[dict] = []
    for slug, record in sorted(_all().items()):
        if record.get("kind") in {"url", "local"}:
            templates, _ = _scan_registry(slug, record)
            out.extend(templates)
    return out


def get_registry_template(template_id: str) -> dict | None:
    prefix = "reg:"
    if not template_id.startswith(prefix):
        return None
    parts = template_id.split(":", 2)
    if len(parts) != 3:
        return None
    slug, folder = parts[1:]
    for template in list_registry_templates():
        if template.get("id") == template_id:
            return template
    return None


def download_registry_template(template_id: str) -> dict | None:
    template = get_registry_template(template_id)
    if not template:
        return None
    slug = template["registry"]
    folder = template_id.rsplit(":", 1)[-1]
    record = _all().get(slug)
    if not record:
        return None
    root = registry_clone_dir(slug)
    manifest = _content_root(root, record) / "templates" / folder / "template.json"
    if not _path_is_safe(root, manifest) or not manifest.is_file():
        return None
    try:
        content = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    return {
        "content": content,
        "filename": f"{folder}-v{template['version']}.json",
        "version": template["version"],
    }


def find_registry_skill_dir(registry_slug: str, skill_slug: str) -> Path | None:
    record = _all().get(registry_slug)
    if not record:
        return None
    if not skill_slug or Path(skill_slug).name != skill_slug or skill_slug in {".", ".."}:
        return None
    root = registry_clone_dir(registry_slug)
    skill_dir = _content_root(root, record) / "skills" / skill_slug
    skill_md = skill_dir / "SKILL.md"
    if (not _path_is_safe(root, skill_dir) or not _path_is_safe(root, skill_md)
            or not skill_dir.is_dir() or not skill_md.is_file()):
        return None
    return skill_dir
