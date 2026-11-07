#!/usr/bin/env python3
"""CI lint for portable Virtual Worker template manifests.

This file intentionally loads ``backend/storage/template_manifest.py`` by
filename.  The manifest module is the portable contract and must remain usable
without importing the Virtual Worker application, configuration, or storage package.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


TOOL_VERSION = "0.1.0"
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _load_manifest_module():
    """Load only the pure manifest implementation, with no package imports."""
    path = Path(__file__).resolve().parents[2] / "backend" / "storage" / "template_manifest.py"
    spec = importlib.util.spec_from_file_location("vw_pure_template_manifest", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load manifest validator at {path}")
    module = importlib.util.module_from_spec(spec)
    # Register the temporary name for normal importlib module semantics.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


template_manifest = _load_manifest_module()
ManifestError = template_manifest.ManifestError


class Finding:
    def __init__(self, severity: str, path: str, message: str, fingerprint: str):
        self.severity = severity
        self.path = path
        self.message = message
        self.fingerprint = fingerprint


class LintResult:
    def __init__(
        self,
        path: Path,
        manifest: dict[str, Any] | None,
        digest: str | None,
        findings: list[Finding],
        errors: list[str],
        warnings: list[str],
        source_id: str | None = None,
    ):
        self.path = path
        self.manifest = manifest
        self.digest = digest
        self.findings = findings
        self.errors = errors
        self.warnings = warnings
        self.source_id = source_id

    @property
    def blocking(self) -> bool:
        return bool(self.errors or any(f.severity == "BLOCKING" for f in self.findings))


def _semver(value: Any) -> tuple[int, int, int, str] | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value.strip())
    if not match:
        return None
    major, minor, patch, prerelease = match.group(1), match.group(2), match.group(3), match.group(4)
    return int(major), int(minor), int(patch), prerelease or ""


def _semver_key(value: str) -> tuple[Any, ...]:
    parsed = _semver(value)
    if parsed is None:
        return (-1,)
    major, minor, patch, prerelease = parsed
    if not prerelease:
        return (major, minor, patch, 1)
    parts: list[Any] = []
    for part in prerelease.split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return (major, minor, patch, 0, parts)


def _walk_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk_strings(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_strings(nested, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _finding_fingerprint(rule: str, path: str, value: str) -> str:
    # Do not put the potentially sensitive value into the suppression file.
    raw = f"{rule}\0{path}\0{value}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_suppressions(start: Path, explicit: Path | None = None) -> set[str]:
    candidates = [explicit] if explicit else []
    if explicit is None:
        for parent in (start, *start.parents):
            candidates.append(parent / ".template-lint-ignore")
    for candidate in candidates:
        if candidate and candidate.is_file():
            return {
                line.strip().split()[0]
                for line in candidate.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
    return set()


def scan_secret_values(manifest: dict[str, Any], suppressions: set[str] | None = None) -> list[Finding]:
    """Find likely committed credentials while allowing reviewed false positives."""
    suppressions = suppressions or set()
    findings: list[Finding] = []
    patterns: list[tuple[str, str, str]] = [
        ("private-key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", "private key material"),
        ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b", "AWS access-key-shaped value"),
        ("gitlab-pat", r"\bglpat-[A-Za-z0-9_-]{12,}\b", "GitLab personal access token-shaped value"),
        ("github-pat", r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b", "GitHub token-shaped value"),
        ("bearer-token", r"\bBearer\s+(?!\{\{)[A-Za-z0-9._~+/=-]{16,}", "Bearer credential"),
        ("secret-assignment", r"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[A-Za-z0-9._~+/=-]{16,}", "credential assignment"),
    ]
    for path, value in _walk_strings(manifest):
        if "{{" in value and "}}" in value:
            # Placeholders are reviewed by the manifest validator.  Do not
            # call a deployment placeholder a real credential.
            scrubbed = re.sub(r"\{\{.*?\}\}", "", value)
        else:
            scrubbed = value
        for rule, pattern, label in patterns:
            match = re.search(pattern, scrubbed)
            if match:
                fingerprint = _finding_fingerprint(rule, path, value)
                if fingerprint not in suppressions:
                    findings.append(Finding("BLOCKING", path, label, fingerprint))
                break
        else:
            is_header = ".headers." in f".{path}"
            if re.search(r"(?i)\b[0-9a-f]{32,}\b", scrubbed) or re.search(
                r"\b[A-Za-z0-9+/]{40,}={0,2}\b", scrubbed
            ):
                rule = "long-secret-shaped-string"
                fingerprint = _finding_fingerprint(rule, path, value)
                if fingerprint not in suppressions:
                    findings.append(
                        Finding(
                            "BLOCKING" if is_header else "WARNING",
                            path,
                            "long hex/base64 secret-shaped string",
                            fingerprint,
                        )
                    )
    return findings


def _marker_warnings(path: Path) -> list[str]:
    warnings: list[str] = []
    for parent in (path.parent, *path.parents):
        marker = parent / "VERSION"
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip()
            if value and value != TOOL_VERSION:
                warnings.append(
                    f"scaffold version marker {marker} is {value!r}; lint tool is {TOOL_VERSION}"
                )
            break
    for parent in (path.parent, *path.parents):
        docs = parent / "docs" / "schema.md"
        if docs.is_file():
            match = re.search(r"template-lint-version:\s*([^\s<]+)", docs.read_text(encoding="utf-8"))
            if match and match.group(1) != TOOL_VERSION:
                warnings.append(
                    f"schema marker {docs} is {match.group(1)!r}; lint tool is {TOOL_VERSION}"
                )
            break
    return warnings


def lint_file(path: Path, ignore_file: Path | None = None) -> LintResult:
    errors: list[str] = []
    warnings = _marker_warnings(path)
    findings: list[Finding] = []
    manifest: dict[str, Any] | None = None
    source_id: str | None = None
    digest: str | None = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ManifestError("template must be a JSON object")
        source_id = str(raw.get("sourceId")) if raw.get("sourceId") is not None else None
        manifest = template_manifest.validate_and_normalize(raw)
        version = manifest.get("version")
        if _semver(version) is None:
            raise ManifestError(f"template version {version!r} is not valid semver (expected MAJOR.MINOR.PATCH)")
        digest = template_manifest.canonical_digest(manifest)
        findings = scan_secret_values(manifest, _load_suppressions(path.parent, ignore_file))
    except (OSError, json.JSONDecodeError, ManifestError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return LintResult(path, manifest, digest, findings, errors, warnings, source_id)


def _git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *cmd], cwd=cwd, text=True, capture_output=True, check=False)


def _git_root(path: Path) -> Path | None:
    result = _git(["rev-parse", "--show-toplevel"], path.parent)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def resolve_git_merge_base(path: Path) -> tuple[Path | None, str | None]:
    """Resolve a target merge-base, reporting shallow-history failures loudly."""
    root = _git_root(path)
    if root is None:
        return None, None
    git_dir_result = _git(["rev-parse", "--git-dir"], root)
    git_dir = Path(git_dir_result.stdout.strip()) if git_dir_result.returncode == 0 else root / ".git"
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    # GitHub Actions exposes the PR base as GITHUB_BASE_REF; GitLab uses the CI_* names.
    target = (os.environ.get("TEMPLATE_LINT_TARGET_BRANCH")
              or os.environ.get("GITHUB_BASE_REF")
              or os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
              or os.environ.get("CI_DEFAULT_BRANCH") or "main")
    refs = [f"origin/{target}", target, "FETCH_HEAD"]
    for ref in refs:
        if _git(["rev-parse", "--verify", ref], root).returncode == 0:
            merge = _git(["merge-base", "HEAD", ref], root)
            if merge.returncode == 0 and merge.stdout.strip():
                return root, merge.stdout.strip()
    shallow = git_dir / "shallow"
    if shallow.exists():
        return None, (
            "cannot run the version-bump check: this git checkout is shallow and no merge-base "
            f"for target branch {target!r} is available; fetch the target branch or run "
            "git fetch --unshallow before running template-lint"
        )
    return None, None


def _manifest_from_bytes(data: bytes, label: str) -> tuple[dict[str, Any], str | None] | None:
    try:
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            return None
        source_id = str(raw.get("sourceId")) if raw.get("sourceId") is not None else None
        return template_manifest.validate_and_normalize(raw), source_id
    except (json.JSONDecodeError, UnicodeDecodeError, ManifestError, TypeError, ValueError):
        return None


def _baseline_files(baseline: Path) -> list[tuple[Path, dict[str, Any], str | None]]:
    output: list[tuple[Path, dict[str, Any], str | None]] = []
    for path in sorted(baseline.rglob("template.json")):
        try:
            parsed = _manifest_from_bytes(path.read_bytes(), str(path))
        except OSError:
            parsed = None
        if parsed is not None:
            output.append((path, parsed[0], parsed[1]))
    return output


def _find_prior(current_path: Path, current: dict[str, Any], source: str | None, candidates: list[tuple[Path, dict[str, Any], str | None]]) -> tuple[Path, dict[str, Any], str | None] | None:
    template_id = current.get("templateId")
    exact = [item for item in candidates if source and item[2] == source and item[1].get("templateId") == template_id]
    if exact:
        return exact[0]
    folder = current_path.parent.name
    fallback = [item for item in candidates if item[1].get("templateId") == template_id and item[0].parent.name == folder]
    if fallback:
        return fallback[0]
    fallback = [item for item in candidates if item[1].get("templateId") == template_id]
    return fallback[0] if fallback else None


def _git_baseline_files(root: Path, commit: str) -> list[tuple[Path, dict[str, Any], str | None]]:
    listing = _git(["ls-tree", "-r", "--name-only", commit, "--", "templates"], root)
    output: list[tuple[Path, dict[str, Any], str | None]] = []
    if listing.returncode != 0:
        return output
    for name in listing.stdout.splitlines():
        if not name.endswith("template.json"):
            continue
        show = _git(["show", f"{commit}:{name}"], root)
        if show.returncode == 0:
            parsed = _manifest_from_bytes(show.stdout.encode("utf-8"), name)
            if parsed is not None:
                output.append((root / name, parsed[0], parsed[1]))
    return output


def check_version_bumps(results: list[LintResult], baseline: Path | None) -> list[str]:
    valid = [(result.path, result.manifest, result.source_id) for result in results if result.manifest is not None]
    if not valid:
        return []
    prior: list[tuple[Path, dict[str, Any], str | None]] = []
    if baseline is not None:
        prior = _baseline_files(baseline)
    else:
        root, commit_or_error = resolve_git_merge_base(results[0].path)
        if commit_or_error and root is None:
            return [commit_or_error]
        if root is not None and commit_or_error:
            prior = _git_baseline_files(root, commit_or_error)
    if not prior:
        return []
    errors: list[str] = []
    for path, current, source_id in valid:
        old = _find_prior(path, current, source_id, prior)
        if old is None:
            continue  # first publication, or a new lineage
        old_path, previous, _old_source_id = old
        current_digest = template_manifest.canonical_digest(current)
        previous_digest = template_manifest.canonical_digest(previous)
        if current_digest != previous_digest and _semver_key(str(current["version"])) <= _semver_key(str(previous["version"])):
            errors.append(
                f"{path}: canonical digest changed from {old_path} ({previous['version']}) "
                f"to {current['version']}; republish with a version bump"
            )
    return errors


def generate_docs() -> str:
    fields = template_manifest.schema_fields()
    lines = [
        "# Virtual Worker Template Manifest Schema",
        "",
        f"<!-- template-lint-version: {TOOL_VERSION} -->",
        "",
        "Generated from `backend/storage/template_manifest.py`; do not edit by hand.",
        "",
        "```json",
        json.dumps(fields, indent=2, ensure_ascii=False, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def _print_result(result: LintResult) -> None:
    if result.errors:
        print(f"BLOCKING {result.path}")
        for error in result.errors:
            print(f"  ERROR: {error}")
        return
    label = "BLOCKING" if any(f.severity == "BLOCKING" for f in result.findings) else "PASS"
    print(f"{label} {result.path} digest={result.digest}")
    for finding in result.findings:
        print(f"  {finding.severity}: {finding.message} at {finding.path} [{finding.fingerprint}]")
    for warning in result.warnings:
        print(f"  WARNING: {warning}")


def _docs_option(value: str | None) -> int:
    content = generate_docs()
    if value and value != "-":
        Path(value).write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate portable Virtual Worker template manifests")
    parser.add_argument("--docs", nargs="?", const="-", metavar="OUTFILE", help="generate schema docs")
    sub = parser.add_subparsers(dest="command")
    lint_parser = sub.add_parser("lint", help="validate template.json files")
    lint_parser.add_argument("paths", nargs="*", type=Path)
    lint_parser.add_argument("--baseline", type=Path, help="directory containing the prior registry checkout")
    lint_parser.add_argument("--ignore", type=Path, help="suppression file (default: nearest .template-lint-ignore)")
    lint_parser.add_argument("--docs", nargs="?", const="-", metavar="OUTFILE", help="also generate schema docs")
    docs_parser = sub.add_parser("docs", help="generate schema.md from the frozen schema description")
    docs_parser.add_argument("outfile", nargs="?", const="-", type=str)
    args = parser.parse_args(argv)
    if args.command == "docs":
        return _docs_option(args.outfile or args.docs)
    if args.command is None and args.docs is not None:
        return _docs_option(args.docs)
    if args.command != "lint":
        parser.print_help()
        return 2
    if args.docs is not None:
        _docs_option(args.docs)
    if not args.paths:
        print("ERROR: lint requires at least one template.json path", file=sys.stderr)
        return 2
    results = [lint_file(path, args.ignore) for path in args.paths]
    for result in results:
        _print_result(result)
    version_errors = check_version_bumps(results, args.baseline)
    for error in version_errors:
        print(f"BLOCKING: {error}")
    return 1 if version_errors or any(result.blocking for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
