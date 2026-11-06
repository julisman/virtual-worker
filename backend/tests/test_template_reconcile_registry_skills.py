"""Hermetic tests for registry skill installation during reconciliation."""

from pathlib import Path

import pytest

import config
from storage import registry_store, skill_store, template_reconcile


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves")
    return tmp_path


def _write_skill(root: Path, slug: str, *, requires_tools: str = "") -> Path:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True)
    tools = f"requires_tools: [{requires_tools}]\n" if requires_tools else ""
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\n{tools}---\n# Registry skill\n",
        encoding="utf-8",
    )
    return skill_dir


def test_install_from_dir_copies_pack_and_resources(isolated):
    source = isolated / "source"
    skill_dir = _write_skill(source, "registry-pack")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "notes.txt").write_text("hello", encoding="utf-8")

    result = skill_store.install_from_dir(skill_dir, "registry-pack")

    assert result["installed"] == ["registry-pack"]
    assert (config.APP_HOME / "skills/registry-pack/SKILL.md").is_file()
    assert (config.APP_HOME / "skills/registry-pack/references/notes.txt").read_text() == "hello"
    assert skill_store.get_skill("registry-pack")["slug"] == "registry-pack"


def test_install_from_dir_rejects_missing_skill_md_and_unsafe_slug(isolated):
    source = isolated / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="SKILL.md"):
        skill_store.install_from_dir(source, "registry-pack")

    skill_dir = _write_skill(isolated / "other", "registry-pack")
    with pytest.raises(ValueError, match="unsafe skill slug"):
        skill_store.install_from_dir(skill_dir, "../registry-pack")


def test_install_from_dir_rejects_symlink_escape(isolated):
    source = isolated / "source"
    skill_dir = _write_skill(source, "registry-pack")
    outside = isolated / "outside.txt"
    outside.write_text("must not copy", encoding="utf-8")
    (skill_dir / "escape.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe path"):
        skill_store.install_from_dir(skill_dir, "registry-pack")
    assert not (config.APP_HOME / "skills/registry-pack").exists()


def test_dangling_internal_symlink_degrades_to_unresolved(isolated, monkeypatch):
    registry_root = isolated / "registry"
    skill_dir = _write_skill(registry_root, "broken-registry-pack")
    (skill_dir / "broken.txt").symlink_to(skill_dir / "missing.txt")

    with pytest.raises(ValueError, match="could not install skill pack"):
        skill_store.install_from_dir(skill_dir, "broken-registry-pack")
    assert not (config.APP_HOME / "skills/broken-registry-pack").exists()

    record = registry_store.add_registry(str(registry_root))
    monkeypatch.setattr(skill_store, "list_builtin", lambda: [])
    template = {
        "source": "registry",
        "registry": record["slug"],
        "skills": ["broken-registry-pack"],
        "sourceId": "registry-test",
        "templateId": "registry-test",
        "version": "1.0.0",
    }
    _attached, _ledger, attention, summary = template_reconcile._reconcile_skills(template)

    assert summary == {"installed": 0, "attached": 1}
    assert {item["kind"] for item in attention} == {"skill-unresolved"}
    assert attention[0]["id"] == "broken-registry-pack"
    assert not (config.APP_HOME / "skills/broken-registry-pack").exists()


def test_reconcile_installs_registry_skill_and_reports_tool_and_unresolved(isolated, monkeypatch):
    registry_root = isolated / "registry"
    _write_skill(registry_root, "registry-only")
    _write_skill(registry_root, "needs-registry-cli", requires_tools="ves-tool-not-installed")
    record = registry_store.add_registry(str(registry_root))
    monkeypatch.setattr(skill_store, "list_builtin", lambda: [])
    monkeypatch.setattr(skill_store.shutil, "which", lambda _tool: None)

    template = {
        "source": "registry",
        "registry": record["slug"],
        "skills": ["registry-only", "needs-registry-cli", "missing-everywhere"],
        "sourceId": "registry-test",
        "templateId": "registry-test",
        "version": "1.0.0",
    }
    attached, ledger, attention, summary = template_reconcile._reconcile_skills(template)

    assert attached == template["skills"]
    assert len(ledger) == 3
    assert summary == {"installed": 2, "attached": 3}
    assert {item["kind"] for item in attention} == {"cli-tool-missing", "skill-unresolved"}
    assert not any(item["kind"] == "skill-unresolved" and item["id"] == "registry-only"
                   for item in attention)
    assert not any(item["kind"] == "skill-unresolved" and item["id"] == "needs-registry-cli"
                   for item in attention)
