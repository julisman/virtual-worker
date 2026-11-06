"""Knowledge Wiki (PRD-13) — OKF storage, agent tools, and compose wiring."""

import asyncio
import io
import zipfile

import pytest

import config
from agent import compose, wiki
from storage import wiki_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


# --- OKF frontmatter --------------------------------------------------------- #

def test_split_frontmatter_scalars_and_lists():
    meta, body = wiki_store.split_frontmatter(
        "---\ntype: Vendor Fact\ntitle: X\ntags: [a, b]\nsources:\n  - u1\n  - u2\n---\nBody here.")
    assert meta["type"] == "Vendor Fact" and meta["title"] == "X"
    assert meta["tags"] == ["a", "b"] and meta["sources"] == ["u1", "u2"]
    assert body.strip() == "Body here."


def test_split_frontmatter_none():
    meta, body = wiki_store.split_frontmatter("no frontmatter here")
    assert meta == {} and body == "no frontmatter here"


def test_ensure_okf_adds_type_and_timestamp():
    out = wiki_store.ensure_okf("just a body, no frontmatter")
    meta, body = wiki_store.split_frontmatter(out)
    assert meta["type"] == "Note" and meta.get("timestamp")
    assert body.strip() == "just a body, no frontmatter"


def test_ensure_okf_keeps_existing_type():
    out = wiki_store.ensure_okf("---\ntype: Playbook\n---\nsteps")
    meta, _ = wiki_store.split_frontmatter(out)
    assert meta["type"] == "Playbook"


# --- store CRUD + pages ------------------------------------------------------ #

def test_create_scaffolds_bundle(home):
    w = wiki_store.create_wiki("Docs", "team docs")
    assert w["name"] == "Docs" and w["pageCount"] == 0
    base = home / "wikis" / w["id"]
    assert (base / "index.md").is_file() and (base / "log.md").is_file()


def test_write_page_forces_okf_and_logs(home):
    w = wiki_store.create_wiki("W")
    res = wiki_store.write_page(w["id"], "vendor-x", "Vendor X: 99.9% uptime.")  # no .md, no frontmatter
    assert res["path"] == "vendor-x.md"
    content = wiki_store.read_page(w["id"], "vendor-x.md")
    meta, _ = wiki_store.split_frontmatter(content)
    assert meta["type"] == "Note" and meta.get("timestamp")   # OKF enforced
    assert "updated vendor-x.md" in wiki_store.read_log(w["id"])
    assert wiki_store.get_wiki(w["id"])["pageCount"] == 1


def test_write_page_rejects_reserved_and_escape(home):
    w = wiki_store.create_wiki("W")
    with pytest.raises(wiki_store.WikiError):
        wiki_store.write_page(w["id"], "index.md", "x")
    with pytest.raises(wiki_store.WikiError):
        wiki_store.write_page(w["id"], "../escape.md", "x")


def test_search_ranks_by_keyword(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "a", "---\ntitle: Uptime SLA\n---\nVendor uptime commitment 99.9%.")
    wiki_store.write_page(w["id"], "b", "Unrelated content about pricing.")
    hits = wiki_store.search_pages(w["id"], "uptime", k=5)
    assert hits and hits[0]["path"] == "a.md"
    assert all(h["path"] != "index.md" for h in hits)   # reserved files excluded


def test_delete_removes_bundle(home):
    w = wiki_store.create_wiki("W")
    base = home / "wikis" / w["id"]
    assert base.exists()
    assert wiki_store.delete_wiki(w["id"]) is True
    assert not base.exists() and wiki_store.get_wiki(w["id"]) is None


# --- agent tools + compose --------------------------------------------------- #

def test_make_wiki_tools_empty_without_attachment(home):
    assert wiki.make_wiki_tools({"id": "a", "wikis": []}) == {}


def test_wiki_tools_read_write_search(home):
    w = wiki_store.create_wiki("Brain")
    agent = {"id": "a1", "wikis": [w["id"]]}
    tools = wiki.make_wiki_tools(agent)
    assert set(tools) == {"wiki_search", "wiki_read", "wiki_write", "wiki_list", "wiki_lint"}
    assert "wrote note.md" in _call(tools["wiki_write"], path="note.md", content="Alpha beta gamma.")
    assert "Alpha beta gamma" in _call(tools["wiki_read"], path="note.md")
    assert "note.md" in _call(tools["wiki_list"])
    assert "note.md" in _call(tools["wiki_search"], query="beta")


def test_compose_wires_wiki_tools_and_context(home):
    w = wiki_store.create_wiki("KB2")
    agent = {"id": "a2", "persona": "x", "wikis": [w["id"]]}
    system, registry, *_ = compose.compose(agent)
    assert "wiki_search" in registry and "wiki_write" in registry and "wiki_lint" in registry
    assert "knowledge wiki" in (system or "").lower()


# --- M3: lint + delete + graph ----------------------------------------------- #

def test_lint_orphans_and_broken(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "a.md", "Alpha → [[b.md]] and [[missing.md]].")
    wiki_store.write_page(w["id"], "b.md", "Beta.")
    wiki_store.write_page(w["id"], "orphan.md", "Nobody links here.")
    rep = wiki_store.lint(w["id"])
    assert rep["orphans"] == ["a.md", "orphan.md"]     # b.md is linked; a/orphan are not
    assert rep["brokenLinks"] == [{"from": "a.md", "to": "missing.md"}]
    assert rep["stale"] == []


def test_lint_flags_stale(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "old.md", "ancient")
    # Age the page by rewriting its timestamp far in the past.
    f = wiki_store._bundle(w["id"]) / "old.md"
    import re as _re
    f.write_text(_re.sub(r"timestamp: .*", "timestamp: 2000-01-01T00:00:00+00:00", f.read_text()), encoding="utf-8")
    stale = wiki_store.lint(w["id"])["stale"]
    assert stale and stale[0]["path"] == "old.md" and stale[0]["ageDays"] > 3000


def test_delete_page(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "gone.md", "x")
    assert wiki_store.delete_page(w["id"], "gone.md") is True
    assert wiki_store.read_page(w["id"], "gone.md") is None
    assert wiki_store.delete_page(w["id"], "gone.md") is False   # already gone
    with pytest.raises(wiki_store.WikiError):
        wiki_store.delete_page(w["id"], "log.md")               # reserved


def test_link_graph_resolved_flag(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "a.md", "See [[b.md]] and [[nope.md]].")
    wiki_store.write_page(w["id"], "b.md", "here")
    g = wiki_store.link_graph(w["id"])
    assert set(g["nodes"]) == {"a.md", "b.md"}
    edges = {(e["from"], e["to"]): e["resolved"] for e in g["edges"]}
    assert edges[("a.md", "b.md")] is True and edges[("a.md", "nope.md")] is False


def test_wiki_lint_tool(home):
    w = wiki_store.create_wiki("W")
    wiki_store.write_page(w["id"], "a.md", "orphan with [[gone.md]].")
    tools = wiki.make_wiki_tools({"id": "x", "wikis": [w["id"]]})
    out = _call(tools["wiki_lint"])
    assert "broken links" in out.lower() and "a.md" in out


# --- M4: export / import ----------------------------------------------------- #

def test_export_import_roundtrip(home):
    w = wiki_store.create_wiki("Original", "desc")
    wiki_store.write_page(w["id"], "topics/ai.md", "AI overview. [[topics/ml.md]]")
    wiki_store.write_page(w["id"], "topics/ml.md", "ML overview.")
    blob = wiki_store.export_bundle(w["id"])
    assert blob[:2] == b"PK"                                    # a real zip

    imported = wiki_store.import_bundle("Copy", blob)
    assert imported["id"] != w["id"] and imported["pageCount"] == 2
    paths = {p["path"] for p in wiki_store.list_pages(imported["id"])}
    assert paths == {"topics/ai.md", "topics/ml.md"}
    assert "AI overview" in wiki_store.read_page(imported["id"], "topics/ai.md")


def test_import_rejects_non_zip(home):
    with pytest.raises(wiki_store.WikiError):
        wiki_store.import_bundle("x", b"not a zip")


def test_import_text_document(home):
    imported = wiki_store.import_document("Meeting notes", "notes.txt", b"Budget approved for Q3")
    page = wiki_store.list_pages(imported["id"])[0]
    assert "Budget approved" in wiki_store.read_page(imported["id"], page["path"])


def test_import_csv_document(home):
    imported = wiki_store.import_document("Sales", "sales.csv", b"Region,Total\nEast,42\n")
    page = wiki_store.list_pages(imported["id"])[0]
    assert "| East | 42 |" in wiki_store.read_page(imported["id"], page["path"])


@pytest.mark.parametrize(("filename", "member", "xml", "expected"), [
    ("brief.docx", "word/document.xml", "<d xmlns:w='w'><w:p><w:t>Project brief</w:t></w:p></d>", "Project brief"),
    ("deck.pptx", "ppt/slides/slide1.xml", "<s xmlns:a='a'><a:t>Launch plan</a:t></s>", "Launch plan"),
    ("budget.xlsx", "xl/worksheets/sheet1.xml",
     "<s xmlns='x'><sheetData><row><c t='inlineStr'><is><t>Cost</t></is></c></row></sheetData></s>", "Cost"),
])
def test_import_office_document(home, filename, member, xml, expected):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(member, xml)
    imported = wiki_store.import_document("Imported", filename, buf.getvalue())
    page = wiki_store.list_pages(imported["id"])[0]
    assert expected in wiki_store.read_page(imported["id"], page["path"])
