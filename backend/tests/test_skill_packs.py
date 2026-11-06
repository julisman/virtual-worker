"""Repo skill packs (/skills) — spec compliance + loader round-trip.

Gates the drop-in packs shipped under ``skills/`` at the repo root: frontmatter
parses, folder names match the derived slug, referenced resources exist, the
packs install and read back through ``storage.skill_store``, and every bundled
python script passes its own ``--self-test`` (stdlib-only, no network).
"""

import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import config
from storage import skill_store

REPO_SKILLS = Path(__file__).resolve().parents[2] / "skills"
# The five curated category packs added by the 50-skill drop; held to the
# strictest checks. Older packs are only spec-parse checked.
NEW_CATEGORIES = ["productivity", "research", "writing", "data", "ops"]


def _packs(root: Path) -> list[Path]:
    """Every skill pack dir (contains SKILL.md) under ``root``."""
    if not root.exists():
        return []
    return sorted(p.parent for p in root.rglob("SKILL.md"))


def _new_packs() -> list[Path]:
    out: list[Path] = []
    for cat in NEW_CATEGORIES:
        out.extend(_packs(REPO_SKILLS / cat))
    return out


def _meta_body(pack: Path) -> tuple[dict, str]:
    return skill_store.parse_frontmatter((pack / "SKILL.md").read_text(encoding="utf-8"))


# ─── spec compliance ─────────────────────────────────────────────────────────

def test_curated_pack_count():
    # The deliverable: 10 packs per category, 50 total.
    for cat in NEW_CATEGORIES:
        assert len(_packs(REPO_SKILLS / cat)) == 10, f"{cat}: expected 10 packs"
    assert len(_new_packs()) == 50


def test_all_packs_frontmatter_parses():
    packs = _packs(REPO_SKILLS)
    assert packs, "no skill packs found in repo"
    for pack in packs:
        meta, body = _meta_body(pack)
        assert body.strip(), f"{pack}: empty body"
        if not meta:
            continue  # legacy bare-markdown pack — the store falls back to the slug
        assert meta.get("name"), f"{pack}: frontmatter missing name"
        assert meta.get("description"), f"{pack}: frontmatter missing description"


def test_new_packs_spec_strict():
    for pack in _new_packs():
        meta, body = _meta_body(pack)
        when = meta.get("when_to_use") or meta.get("when")
        assert when, f"{pack}: missing when_to_use"
        # one-line values (the flat reader folds continuations — forbid them here)
        raw = (pack / "SKILL.md").read_text(encoding="utf-8")
        fm = raw.split("---", 2)[1]
        for line in fm.strip().splitlines():
            assert re.match(r"^[A-Za-z0-9_-]+\s*:\s*\S", line), \
                f"{pack}: non-flat frontmatter line: {line!r}"
        # folder name is the slug derived from name
        assert pack.name == skill_store._slugify(meta["name"]), \
            f"{pack}: folder != slug of {meta['name']!r}"


def test_slugs_unique_repo_wide():
    seen: dict[str, Path] = {}
    for pack in _packs(REPO_SKILLS):
        meta, _ = _meta_body(pack)
        slug = skill_store._slugify(meta.get("name") or pack.name)
        assert slug not in seen, f"slug collision: {pack} vs {seen[slug]}"
        seen[slug] = pack


def test_new_packs_resources_consistent():
    """Every resource path mentioned in a body exists; every bundled file is
    mentioned in the body (no orphan or phantom L3 resources)."""
    pat = re.compile(r"(?:references|scripts|assets)/[A-Za-z0-9._/-]+")
    for pack in _new_packs():
        _, body = _meta_body(pack)
        mentioned = set(pat.findall(body))
        bundled = {str(f.relative_to(pack)) for f in pack.rglob("*")
                   if f.is_file() and f.name != "SKILL.md" and not f.name.startswith(".")}
        for path in mentioned:
            assert (pack / path).is_file(), f"{pack}: body mentions missing {path}"
        for path in bundled:
            assert path in mentioned, f"{pack}: bundled {path} never mentioned in body"


# ─── loader round-trip (storage.skill_store) ─────────────────────────────────

@pytest.fixture()
def installed(tmp_path, monkeypatch):
    """All curated packs copied into a scratch VW_HOME skills dir."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    dest = tmp_path / "skills"
    dest.mkdir()
    for pack in _new_packs():
        shutil.copytree(pack, dest / pack.name)
    return [p.name for p in _new_packs()]


def test_store_reads_all_packs(installed):
    listed = {s["slug"] for s in skill_store.list_skills()}
    assert set(installed) <= listed
    for slug in installed:
        skill = skill_store.get_skill(slug)
        assert skill and skill["description"] and skill["whenToUse"]
        assert skill_store.skill_body(slug)
        for res in skill_store.list_resources(slug):
            text = skill_store.read_resource(slug, res["path"])
            assert text, f"{slug}: unreadable resource {res['path']}"


def test_catalog_and_load_skill_tool(installed):
    from agent import skills as agent_skills
    catalog = agent_skills.build_skill_context(installed)
    for slug in installed:
        assert f"`{slug}`" in catalog
    tool = agent_skills.make_load_skill_tool(installed)["fn"]
    out = asyncio.run(tool(slug=installed[0]))
    assert "# Skill:" in out and "## " in out


def test_zip_roundtrip(installed):
    slug = installed[0]
    blob = skill_store.export_zip(slug)
    assert blob
    before = {r["path"] for r in skill_store.list_resources(slug)}
    skill_store.delete_skill(slug)
    restored = skill_store.save_zip(f"{slug}.zip", blob)
    assert restored["slug"] == slug
    assert {r["path"] for r in skill_store.list_resources(slug)} == before


# ─── bundled scripts ─────────────────────────────────────────────────────────

def test_bundled_scripts_selftest():
    scripts = [f for pack in _new_packs() for f in sorted(pack.glob("scripts/*.py"))]
    assert scripts, "expected at least one bundled script in the curated packs"
    for script in scripts:
        proc = subprocess.run([sys.executable, str(script), "--self-test"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"{script}: self-test failed\n{proc.stdout}{proc.stderr}"
        assert "SELF-TEST OK" in proc.stdout, f"{script}: no SELF-TEST OK marker"
