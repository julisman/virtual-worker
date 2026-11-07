"""Social lead-gen skill packs: the bundled social.py guardrail script (self-test
plus targeted checks), pack frontmatter and loader round-trip, and the
Social Lead-Gen built-in template."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
from storage import skill_store, template_store

REPO = Path(__file__).resolve().parents[2]
SOCIAL = REPO / "skills" / "social"
SCRIPT = SOCIAL / "social-core" / "scripts" / "social.py"
PACKS = ["social-core", "social-prospect", "social-engage", "social-dm", "social-comments",
         "social-health", "social-insight", "social-watch"]


def test_script_self_test():
    p = subprocess.run([sys.executable, str(SCRIPT), "--self-test"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "self-test OK" in p.stdout


def _run(env, cwd, *args, stdin=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env={**os.environ, **env}, cwd=cwd, input=stdin, timeout=60)


def test_config_lives_outside_the_working_folder_and_ramp_gates_replies(tmp_path):
    env = {"VW_HOME": str(tmp_path / "vwhome")}       # the service passes VW_HOME to the shell tool
    work = tmp_path / "work"; work.mkdir()
    p = _run(env, work, "init", "--brand", "acme")
    assert p.returncode == 0, p.stderr
    cfg_path = tmp_path / "vwhome" / "social" / "config.json"
    assert cfg_path.exists() and not (work / "config.json").exists()   # not reachable by file tools confined to work/
    assert (work / "brands" / "acme" / "BRAND.md").exists() and (work / "data").is_dir()
    cfg = json.loads(cfg_path.read_text())
    cfg["brands"]["acme"]["wa_number"] = "6281200000000"
    cfg["brands"]["acme"]["accounts"]["threads"].update(enabled=True, handle="@acme")
    cfg_path.write_text(json.dumps(cfg))
    g = json.loads(_run(env, work, "guard", "acme", "threads").stdout)
    assert g["status"] == "ok" and g["replies_remaining_today"] == 0      # read-only until a person sets ramp_start
    # the worker-side kill switch also works
    (work / "PAUSED").write_text("owner says stop")
    p = _run(env, work, "guard", "acme", "threads")
    assert p.returncode == 1 and "PAUSED" in p.stdout
    (work / "PAUSED").unlink()
    # stdin JSON form for ledgers
    p = _run(env, work, "log", "--json", "-", stdin=json.dumps({"brand": "acme", "platform": "threads", "kind": "run", "skill": "social-dm"}))
    assert p.returncode == 0
    assert "social-dm acme" not in _run(env, work, "due").stdout        # just ran → not due
    assert "social-prospect acme" in _run(env, work, "due").stdout or "outside working hours" in _run(env, work, "due").stderr


def test_packs_parse_and_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    for slug in PACKS:
        md = (SOCIAL / slug / "SKILL.md").read_text(encoding="utf-8")
        meta, body = skill_store.parse_frontmatter(md)
        assert meta.get("name") and meta.get("description") and meta.get("when_to_use") and meta.get("version"), slug
        assert body.strip() and "social.py" in body, slug
        if slug != "social-core":
            assert "social-core" in str(meta.get("requires_skills")), slug
        if slug in ("social-engage", "social-comments"):
            assert "No link" in body or "NO LINKS" in body, slug
    core = SOCIAL / "social-core"
    assert (core / "scripts" / "social.py").exists()
    assert (core / "references" / "platform-notes.md").exists() and (core / "references" / "safety.md").exists()
    # install the core pack through the store and read its script back as a resource
    skill_store.install_from_dir(core, "social-core")
    res = {r["path"] for r in skill_store.list_resources("social-core")}
    assert {"scripts/social.py", "references/platform-notes.md", "references/safety.md"} <= res


def test_social_lead_gen_template_deploys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    tpl = next(t for t in template_store.BUILTIN_TEMPLATES if t["id"] == "social-lead-gen")
    assert set(PACKS) <= set(tpl["skills"])
    assert {"browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_screenshot"} <= set(tpl["tools"])
    assert tpl["guardrails"]["shell"] == "workspace"
    agent = template_store.deploy_template("social-lead-gen", {"business_name": "Acme Rentals", "sells": "scooter rental"})
    assert "Acme Rentals" in agent["name"] or "Acme Rentals" in agent["persona"]
    assert "social-engage" in agent["skills"] and "browser_open" in agent["tools"]
