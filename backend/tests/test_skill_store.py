"""Skill packs — frontmatter parsing + on-disk upload/list/get/delete."""

import io
import zipfile

import pytest

import config
import storage.skill_store as ss


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return ss


SAMPLE = """---
name: Refund Handler
description: Process customer refunds step by step.
when_to_use: When a customer asks for a refund.
---
# Refund procedure
1. Verify the order.
2. Issue the refund.
"""


def test_parse_frontmatter_basic():
    meta, body = ss.parse_frontmatter(SAMPLE)
    assert meta["name"] == "Refund Handler"
    assert meta["description"].startswith("Process customer refunds")
    assert meta["when_to_use"].startswith("When a customer")
    assert body.startswith("# Refund procedure")


def test_web_auth_domains_are_exposed_in_skill_metadata():
    md = "---\nname: Portal Reader\nweb_auth_domains:\n  - data.example.com\n  - '*.example.com'\n---\nbody"
    meta, _ = ss.parse_frontmatter(md)
    assert meta["web_auth_domains"] == ["data.example.com", "*.example.com"]
    skill = ss._meta_to_skill("portal-reader", meta, "body")
    assert skill["webAuthDomains"] == ["data.example.com", "*.example.com"]


def test_parse_frontmatter_quotes_and_none():
    meta, body = ss.parse_frontmatter('---\nname: "Quoted"\n---\nhi')
    assert meta["name"] == "Quoted"
    meta2, body2 = ss.parse_frontmatter("no frontmatter here")
    assert meta2 == {}
    assert body2 == "no frontmatter here"


def test_save_markdown_and_list_get(store):
    skill = store.save_markdown("refund.md", SAMPLE.encode())
    assert skill["slug"] == "refund-handler"
    assert skill["name"] == "Refund Handler"
    assert skill["whenToUse"].startswith("When a customer")

    listed = store.list_skills()
    assert [s["slug"] for s in listed] == ["refund-handler"]

    full = store.get_skill("refund-handler")
    assert "Refund procedure" in full["body"]
    assert store.skill_body("refund-handler").startswith("# Refund procedure")


def test_save_zip_with_resources(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mypack/SKILL.md", SAMPLE)
        zf.writestr("mypack/references/email.txt", "Dear customer")
    skill = store.save_zip("mypack.zip", buf.getvalue())
    assert skill["slug"] == "refund-handler"  # slug from frontmatter name
    assert "references/email.txt" in skill["files"]
    body = store.skill_body("refund-handler")
    assert "Refund procedure" in body


def test_version_parsed_with_default(store):
    store.save_markdown("refund.md", SAMPLE.encode())
    assert store.get_skill("refund-handler")["version"] == "1.0.0"
    md = "---\nname: Versioned\nversion: 2.3.1\n---\nbody"
    store.save_markdown("v.md", md.encode())
    assert store.get_skill("versioned")["version"] == "2.3.1"


def test_list_and_read_resources(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("p/SKILL.md", SAMPLE)
        zf.writestr("p/references/email.txt", "Dear customer")
    store.save_zip("p.zip", buf.getvalue())
    res = store.list_resources("refund-handler")
    assert any(r["path"] == "references/email.txt" and r["size"] > 0 for r in res)
    assert store.read_resource("refund-handler", "references/email.txt") == "Dear customer"


def test_read_resource_rejects_traversal_and_skill_md(store):
    store.save_markdown("refund.md", SAMPLE.encode())
    assert store.read_resource("refund-handler", "../../etc/passwd") is None
    assert store.read_resource("refund-handler", "SKILL.md") is None
    assert store.read_resource("refund-handler", "nope.txt") is None


def test_save_zip_without_skill_md_rejected(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nope/readme.txt", "hi")
    with pytest.raises(ValueError, match="SKILL.md"):
        store.save_zip("nope.zip", buf.getvalue())


def test_zip_path_traversal_blocked(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SAMPLE)
        zf.writestr("../escape.txt", "pwned")
    with pytest.raises(ValueError, match="unsafe path"):
        store.save_zip("evil.zip", buf.getvalue())


def test_export_zip_roundtrips(store):
    # Save a pack with a resource, export it, and re-import the exported bytes —
    # the skill (body + resources) must come back intact.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mypack/SKILL.md", SAMPLE)
        zf.writestr("mypack/references/email.txt", "Dear customer")
    store.save_zip("mypack.zip", buf.getvalue())

    data = store.export_zip("refund-handler")
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert "refund-handler/SKILL.md" in names          # nested under <slug>/
    assert "refund-handler/references/email.txt" in names

    store.delete_skill("refund-handler")
    reimported = store.save_zip("refund-handler.zip", data)
    assert reimported["slug"] == "refund-handler"
    assert "references/email.txt" in reimported["files"]
    assert "Refund procedure" in store.skill_body("refund-handler")


def test_export_zip_missing_or_unsafe(store):
    assert store.export_zip("does-not-exist") is None
    assert store.export_zip("../../etc") is None       # path traversal rejected


def test_delete(store):
    store.save_markdown("refund.md", SAMPLE.encode())
    assert store.delete_skill("refund-handler") is True
    assert store.get_skill("refund-handler") is None
    assert store.delete_skill("refund-handler") is False
