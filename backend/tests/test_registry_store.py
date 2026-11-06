"""Hermetic tests for git-backed template registries."""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import config
from storage import registry_store, template_manifest


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves")
    return tmp_path


def _manifest(template_id="team-agent", version="1.2.3"):
    return {
        "templateId": template_id,
        "name": "Team Agent",
        "persona": "You help the team.",
        "version": version,
    }


def _git_repo(root: Path, *, with_skill=True) -> Path:
    repo = root / "ves-templates"
    (repo / "templates" / "team-agent").mkdir(parents=True)
    (repo / "templates" / "team-agent" / "template.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    if with_skill:
        (repo / "skills" / "team-skill").mkdir(parents=True)
        (repo / "skills" / "team-skill" / "SKILL.md").write_text(
            "# Team skill\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.com",
         "-c", "user.name=Registry Test", "commit", "-qm", "initial"],
        check=True, capture_output=True,
    )
    return repo


def test_git_registry_templates_and_download(isolated):
    repo = _git_repo(isolated)
    record = registry_store.add_registry(f"file://{repo}")

    assert record["kind"] == "url"
    assert record["templateCount"] == 1
    assert record["skillPackCount"] == 1
    templates = registry_store.list_registry_templates()
    assert len(templates) == 1
    template = templates[0]
    assert template["source"] == "registry"
    assert template["registry"] == record["slug"]
    assert template["id"] == f"reg:{record['slug']}:team-agent"
    assert template["version"] == "1.2.3"
    assert template["digest"] == template_manifest.canonical_digest(
        template_manifest.validate_and_normalize(_manifest())
    )
    assert registry_store.get_registry_template(template["id"])["id"] == template["id"]
    downloaded = registry_store.download_registry_template(template["id"])
    assert downloaded["content"] == (repo / "templates/team-agent/template.json").read_text()
    assert downloaded["version"] == "1.2.3"
    assert registry_store.find_registry_skill_dir(record["slug"], "team-skill").is_dir()


def test_local_registry_rescans_without_deleting_checkout(isolated):
    local = isolated / "local-registry"
    manifest_dir = local / "templates" / "local-agent"
    manifest_dir.mkdir(parents=True)
    manifest_data = _manifest("local-agent", "1.0.0")
    manifest_file = manifest_dir / "template.json"
    manifest_file.write_text(json.dumps(manifest_data))
    record = registry_store.add_registry(str(local))
    assert record["kind"] == "local"
    assert record["url"] == str(local)
    assert record["templateCount"] == 1
    (local / "templates" / "second").mkdir()
    (local / "templates" / "second" / "template.json").write_text(
        json.dumps(_manifest("second", "1.0.0"))
    )
    synced = registry_store.sync_registry(record["slug"])
    assert synced["templateCount"] == 2
    assert registry_store.remove_registry(record["slug"])
    assert local.is_dir()
    assert manifest_file.exists()


def test_symlink_escape_and_parent_symlink_are_skipped(isolated):
    repo = _git_repo(isolated, with_skill=False)
    outside = isolated / "outside.json"
    outside.write_text(json.dumps(_manifest("escape")))
    escaped = repo / "templates" / "escape"
    escaped.mkdir()
    (escaped / "template.json").symlink_to(outside)
    outside_dir = isolated / "outside-dir"
    (outside_dir / "template.json").parent.mkdir(parents=True)
    (outside_dir / "template.json").write_text(json.dumps(_manifest("parent")))
    (repo / "templates" / "parent").symlink_to(outside_dir, target_is_directory=True)
    # Add the symlinks to the local repo; git checkout preserves them.
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.com",
         "-c", "user.name=Registry Test", "commit", "-qm", "symlinks"],
        check=True, capture_output=True,
    )
    record = registry_store.add_registry(f"file://{repo}")
    ids = {item["templateId"] for item in registry_store.list_registry_templates()}
    assert ids == {"team-agent"}
    assert registry_store.find_registry_skill_dir(record["slug"], "../outside-dir") is None


def test_oversized_clone_is_removed_and_bad_refs_fail(isolated, monkeypatch):
    repo = _git_repo(isolated, with_skill=False)
    monkeypatch.setenv("VW_REGISTRY_MAX_BYTES", "1")
    with pytest.raises(registry_store.RegistryError) as oversized:
        registry_store.add_registry(f"file://{repo}")
    assert oversized.value.code == "too_large"
    assert not (config.APP_HOME / "registries").exists() or not any(
        (config.APP_HOME / "registries").iterdir()
    )
    monkeypatch.setenv("VW_REGISTRY_MAX_BYTES", str(100 * 1024 * 1024))
    with pytest.raises(registry_store.RegistryError) as missing:
        registry_store.add_registry(f"file://{repo}", branch="does-not-exist")
    assert missing.value.code == "bad_ref"
    with pytest.raises(registry_store.RegistryError) as malformed:
        registry_store.add_registry(f"file://{repo}", branch="../evil")
    assert malformed.value.code == "bad_ref"


def test_git_failure_is_non_interactive_and_bounded(isolated, monkeypatch):
    monkeypatch.setenv("VW_REGISTRY_GIT_TIMEOUT", "1")
    started = time.monotonic()
    with pytest.raises(registry_store.RegistryError):
        registry_store.add_registry("file:///definitely/not/a/registry")
    assert time.monotonic() - started < 5


def test_concurrent_local_adds_do_not_lose_registries(isolated):
    paths = []
    for index in range(8):
        local = isolated / f"registry-{index}"
        manifest_dir = local / "templates" / "agent"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "template.json").write_text(
            json.dumps(_manifest(f"agent-{index}", "1.0.0"))
        )
        paths.append(local)

    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        records = list(pool.map(lambda path: registry_store.add_registry(str(path)), paths))

    stored = registry_store.list_registries()
    assert len(records) == len(paths)
    assert len(stored) == len(paths)
    assert {record["slug"] for record in records} == {record["slug"] for record in stored}


# --------------------------------------------------------------------------- #
# Content subdirectory (optional override + distinctive default)
# --------------------------------------------------------------------------- #

def _local_registry_dir(root: Path, base: str = "") -> Path:
    repo = root / "monorepo"
    content = repo / base if base else repo
    (content / "templates" / "team-agent").mkdir(parents=True)
    (content / "templates" / "team-agent" / "template.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    return repo


def test_explicit_subdir_is_scanned(isolated):
    repo = _local_registry_dir(isolated, "tools/agent-templates")
    reg = registry_store.add_registry(str(repo), subdir="tools/agent-templates")
    assert reg["subdir"] == "tools/agent-templates"
    assert reg["templateCount"] == 1


def test_default_subdir_ves_registry_autodetected(isolated):
    repo = _local_registry_dir(isolated, registry_store.DEFAULT_SUBDIR)
    reg = registry_store.add_registry(str(repo))
    assert reg["subdir"] is None
    assert reg["templateCount"] == 1


def test_root_layout_still_works_without_subdir(isolated):
    repo = _local_registry_dir(isolated)
    reg = registry_store.add_registry(str(repo))
    assert reg["templateCount"] == 1


def test_subdir_with_dotdot_is_rejected(isolated):
    repo = _local_registry_dir(isolated)
    with pytest.raises(registry_store.RegistryError):
        registry_store.add_registry(str(repo), subdir="../escape")
    with pytest.raises(registry_store.RegistryError):
        registry_store.add_registry(str(repo), subdir="/absolute")
