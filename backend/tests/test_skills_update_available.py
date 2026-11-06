"""Drift detection for installed builtin skill packs (updateAvailable)."""

from pathlib import Path

import pytest

import config
from storage import skill_store


def _write_pack(base: Path, name: str, fm: dict, body: str = "body") -> Path:
    d = base / name
    d.mkdir(parents=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    (d / "SKILL.md").write_text("\n".join(lines) + f"\n\n{body}\n", encoding="utf-8")
    return d


@pytest.fixture()
def builtin_env(tmp_path, monkeypatch):
    """Scratch VW_HOME + a scratch builtin dir holding one pack."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    root = tmp_path / "repo-skills"
    _write_pack(root / "inventory", "pack", {"name": "pack", "version": "1.2.0"})
    monkeypatch.setattr(config, "builtin_skills_dir", lambda: root)
    return root


def _entry(slug: str) -> dict:
    return {s["slug"]: s for s in skill_store.list_builtin()}[slug]


def test_not_installed_has_no_update_flag(builtin_env):
    s = _entry("pack")
    assert s["installed"] is False
    assert s.get("updateAvailable") in (None, False)


def test_installed_pack_matches_no_update(builtin_env):
    skill_store.install_builtin("pack")
    s = _entry("pack")
    assert s["installed"] is True
    assert s["updateAvailable"] is False
    assert s["installedVersion"] == "1.2.0"


def test_content_drift_flags_update_without_version_bump(builtin_env):
    skill_store.install_builtin("pack")
    # A real fix ships in the builtin pack, same version — a content-only change.
    (builtin_env / "inventory" / "pack" / "SKILL.md").write_text(
        "---\nname: pack\nversion: 1.2.0\n---\n\nbody v2 — a real fix\n",
        encoding="utf-8")
    s = _entry("pack")
    assert s["installed"] is True
    assert s["updateAvailable"] is True
    assert s["installedVersion"] == "1.2.0"


def test_update_all_refreshes_only_drifted_installed_packs(builtin_env):
    _write_pack(builtin_env / "inventory", "pack2", {"name": "pack2", "version": "1.0.0"})
    _write_pack(builtin_env / "inventory", "pack3", {"name": "pack3", "version": "1.0.0"})
    skill_store.install_builtin("pack")   # will drift
    skill_store.install_builtin("pack2")  # stays current
    # pack3 is never installed — update-all must not install it.
    (builtin_env / "inventory" / "pack" / "SKILL.md").write_text(
        "---\nname: pack\nversion: 1.2.0\n---\n\nbody v2\n", encoding="utf-8")
    r = skill_store.update_all_builtin()
    assert r["updated"] == ["pack"]
    assert _entry("pack")["updateAvailable"] is False
    assert _entry("pack3")["installed"] is False
    # Nothing drifted → no-op.
    assert skill_store.update_all_builtin()["updated"] == []


def test_reinstall_clears_update(builtin_env):
    skill_store.install_builtin("pack")
    (builtin_env / "inventory" / "pack" / "SKILL.md").write_text(
        "---\nname: pack\nversion: 1.2.0\n---\n\nbody v2\n", encoding="utf-8")
    assert _entry("pack")["updateAvailable"] is True
    skill_store.install_builtin("pack")  # re-install is the update path
    assert _entry("pack")["updateAvailable"] is False
