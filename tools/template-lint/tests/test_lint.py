import importlib.util
import json
import sys
from pathlib import Path

import pytest


TOOL_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("template_lint_under_test", TOOL_DIR / "lint.py")
lint = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["template_lint_under_test"] = lint
SPEC.loader.exec_module(lint)


def manifest(**overrides):
    value = {
        "schemaVersion": 2,
        "sourceId": "team",
        "templateId": "agent",
        "name": "Agent",
        "persona": "You help.",
        "version": "1.0.0",
        "variables": [],
        "customTools": [],
        "connectors": [],
    }
    value.update(overrides)
    return value


def write(path: Path, value: dict):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_valid_manifest_passes_and_has_digest(tmp_path):
    path = tmp_path / "template.json"
    write(path, manifest())
    result = lint.lint_file(path)
    assert not result.errors
    assert result.digest and len(result.digest) == 64


def test_new_v2_sections_pass_and_digest_deterministic(tmp_path):
    value = manifest(
        variables=[
            {"key": "wiki-folder", "type": "folder"},
            {"key": "kb-folder", "type": "folder"},
        ],
        wikis=[
            {
                "name": "Runbook",
                "repoUrl": "https://gitlab.example.invalid/team/runbook",
                "wikiRoot": "docs",
                "pathVariable": "wiki-folder",
            }
        ],
        knowledgeBases=[
            {
                "name": "Operations SOP",
                "description": "Team operating procedures",
                "embeddingModel": "local-default",
                "sourceFolderVariable": "kb-folder",
            }
        ],
        mcpServers=[
            {
                "name": "runbook-search",
                "transport": "stdio",
                "hint": "Search the local runbook server",
                "mcpToolAllow": ["search"],
            }
        ],
    )
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write(first_path, value)
    write(second_path, value)

    first = lint.lint_file(first_path)
    second = lint.lint_file(second_path)
    assert not first.errors
    assert not second.errors
    assert first.digest == second.digest


@pytest.mark.parametrize("secret_field", ["env", "command", "url", "token"])
def test_mcp_secret_or_machine_specific_fields_are_rejected(secret_field):
    with pytest.raises(lint.ManifestError, match="forbidden secret"):
        lint.template_manifest.validate_and_normalize(
            manifest(mcpServers=[{"name": "local", secret_field: "not-portable"}])
        )


def test_requires_features_derives_new_sections():
    normalized = lint.template_manifest.validate_and_normalize(
        manifest(
            wikis=[{"name": "Runbook", "repoUrl": "https://gitlab.example.invalid/runbook"}],
            knowledgeBases=[{"name": "SOP"}],
            mcpServers=[{"name": "search"}],
        )
    )
    assert normalized["requiresFeatures"] == ["knowledgeBases", "mcpServers", "wikis"]
    assert lint.template_manifest.derive_requires_features(normalized) == normalized["requiresFeatures"]


@pytest.mark.parametrize(
    "override, message",
    [({"name": ""}, "name"), ({"persona": ""}, "persona"), ({"version": "1.0"}, "semver")],
)
def test_required_fields_and_semver_block(tmp_path, override, message):
    path = tmp_path / "template.json"
    write(path, manifest(**override))
    result = lint.lint_file(path)
    assert result.errors
    assert message in result.errors[0]


def test_secret_placeholder_misplacement_blocks(tmp_path):
    path = tmp_path / "template.json"
    write(
        path,
        manifest(
            persona="Do not leak {{token}}",
            variables=[{"key": "token", "type": "secret"}],
        ),
    )
    result = lint.lint_file(path)
    assert result.errors and "not allowed" in result.errors[0]


def test_digest_changed_without_version_bump_blocks(tmp_path):
    baseline = tmp_path / "baseline" / "templates" / "agent"
    current = tmp_path / "current" / "templates" / "agent"
    baseline.mkdir(parents=True)
    current.mkdir(parents=True)
    write(baseline / "template.json", manifest())
    current_manifest = manifest(persona="Changed but not bumped")
    write(current / "template.json", current_manifest)
    result = lint.lint_file(current / "template.json")
    errors = lint.check_version_bumps([result], tmp_path / "baseline")
    assert errors and "republish with a version bump" in errors[0]


def test_value_scan_warning_and_blocking_and_suppression(tmp_path):
    warning_path = tmp_path / "warning.json"
    warning_value = "a" * 40
    write(warning_path, manifest(persona=f"Reference {warning_value}"))
    warning_result = lint.lint_file(warning_path)
    assert any(f.severity == "WARNING" for f in warning_result.findings)

    blocking_path = tmp_path / "blocking.json"
    token = "Bearer " + "A" * 24
    write(
        blocking_path,
        manifest(
            customTools=[
                {
                    "key": "tool",
                    "name": "Tool",
                    "headers": {"Authorization": token},
                }
            ]
        ),
    )
    blocking_result = lint.lint_file(blocking_path)
    finding = next(f for f in blocking_result.findings if f.severity == "BLOCKING")
    assert finding.fingerprint
    (tmp_path / ".template-lint-ignore").write_text(
        f"# reviewed test fixture\n{finding.fingerprint}\n", encoding="utf-8"
    )
    suppressed = lint.lint_file(blocking_path)
    assert not [f for f in suppressed.findings if f.severity == "BLOCKING"]


def test_docs_generates_schema(tmp_path):
    output = tmp_path / "schema.md"
    assert lint.main(["docs", str(output)]) == 0
    content = output.read_text(encoding="utf-8")
    assert "Virtual Worker Template Manifest Schema" in content
    assert "v2TopLevel" in content
    assert '"wiki"' in content
    assert '"knowledgeBase"' in content
    assert '"mcpServer"' in content
    assert len(content) > 100


def test_cli_import_is_pure(monkeypatch):
    code = (
        "import importlib.util, sys; "
        f"s=importlib.util.spec_from_file_location('fresh_lint', {str(TOOL_DIR / 'lint.py')!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "assert 'backend.config' not in sys.modules; "
        "assert not any(n == 'storage' or n.startswith('storage.') for n in sys.modules)"
    )
    import subprocess

    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
