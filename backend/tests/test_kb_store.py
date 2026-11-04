"""Knowledge Base — chunking, storage, and pure-Python cosine retrieval.

Embedding is monkeypatched to deterministic fake vectors so these tests run with
no live embedding model (CI-safe + offline), exercising the chunk → embed →
store → cosine-rank path end to end.
"""

import asyncio

import pytest

import config
from agent import knowledge as kn
from storage import kb_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return kb_store


# --------------------------------------------------------------------------- #
# Deterministic fake embedder — a tiny bag-of-words vector over a fixed vocab.
# Texts sharing words get similar vectors, so cosine ranking is predictable
# without any model server.
# --------------------------------------------------------------------------- #
_VOCAB = ["refund", "window", "shipping", "policy", "cat", "dog", "blockchain", "token"]


def _fake_vec(text: str) -> list[float]:
    low = text.lower()
    return [float(low.count(w)) for w in _VOCAB]


async def _fake_embed(texts, model=""):
    return [_fake_vec(t) for t in texts]


def test_chunk_text_overlap():
    text = "x" * 2000
    chunks = kb_store.chunk_text(text, size=800, overlap=100)
    assert len(chunks) >= 2
    assert all(len(c) <= 800 for c in chunks)
    # Tiny doc stays a single chunk.
    assert kb_store.chunk_text("short") == ["short"]
    assert kb_store.chunk_text("   ") == []


def test_cosine_ranking():
    a = [1.0, 0.0, 0.0]
    assert kb_store.cosine(a, a) == pytest.approx(1.0)
    assert kb_store.cosine(a, [0.0, 1.0, 0.0]) == pytest.approx(0.0)
    assert kb_store.cosine([], a) == 0.0
    assert kb_store.cosine(a, [1.0, 1.0]) == 0.0  # length mismatch is safe


def test_create_list_delete_kb(store):
    kb = store.create_kb("Handbook", "company docs")
    assert kb["name"] == "Handbook"
    assert kb["chunkCount"] == 0
    assert any(k["id"] == kb["id"] for k in store.list_kbs())
    assert store.delete_kb(kb["id"]) is True
    assert store.get_kb(kb["id"]) is None


def test_ingest_and_search_ranks_most_similar_first(store, monkeypatch):
    monkeypatch.setattr(kn, "embed", _fake_embed)
    kb = store.create_kb("Policies")

    doc = (
        "Our refund window is 30 days from purchase.\n\n"
        "Shipping policy: orders ship within two business days.\n\n"
        "The office cat is named Pixel and likes the dog."
    )
    # Small chunk size so each paragraph becomes its own chunk.
    monkeypatch.setattr(kb_store, "CHUNK_SIZE", 60)
    monkeypatch.setattr(kb_store, "CHUNK_OVERLAP", 10)
    count = asyncio.run(kn.ingest_text(kb["id"], "policies.md", doc))
    assert count >= 3

    # Stored + counted.
    refreshed = store.get_kb(kb["id"])
    assert refreshed["chunkCount"] == count
    assert refreshed["sourceCount"] == 1
    assert refreshed["embedDim"] == len(_VOCAB)

    hits = asyncio.run(kn.search_kbs([kb["id"]], "what is the refund window?", k=3))
    assert hits, "expected at least one hit"
    assert "refund" in hits[0]["text"].lower()
    assert hits[0]["score"] >= hits[-1]["score"]
    assert hits[0]["kbId"] == kb["id"]


def test_reingest_source_is_idempotent(store, monkeypatch):
    monkeypatch.setattr(kn, "embed", _fake_embed)
    kb = store.create_kb("KB")
    asyncio.run(kn.ingest_text(kb["id"], "a.md", "refund window policy"))
    first = store.get_kb(kb["id"])["chunkCount"]
    asyncio.run(kn.ingest_text(kb["id"], "a.md", "refund window policy"))
    assert store.get_kb(kb["id"])["chunkCount"] == first  # replaced, not duplicated

    sources = store.list_sources(kb["id"])
    assert len(sources) == 1 and sources[0]["source"] == "a.md"
    assert store.delete_source(kb["id"], "a.md") == first
    assert store.get_kb(kb["id"])["chunkCount"] == 0


def test_kb_search_tool(store, monkeypatch):
    monkeypatch.setattr(kn, "embed", _fake_embed)
    kb = store.create_kb("KB")
    asyncio.run(kn.ingest_text(kb["id"], "doc.md", "the refund window is 30 days"))

    tools = kn.make_kb_tools({"knowledgeBases": [kb["id"]]})
    assert set(tools) == {"kb_search"}
    out = asyncio.run(tools["kb_search"]["fn"](query="refund window"))
    assert "refund" in out.lower()
    assert "doc.md" in out

    # No query / no KB attached → graceful messages, not crashes.
    assert "needs" in asyncio.run(tools["kb_search"]["fn"](query=""))
    no_kb = kn.make_kb_tools({"knowledgeBases": []})
    assert "No knowledge base" in asyncio.run(no_kb["kb_search"]["fn"](query="x"))


def test_embedding_unavailable_degrades(store, monkeypatch):
    async def _boom(texts, model=""):
        raise kn.EmbeddingUnavailable("no embedding model active")

    monkeypatch.setattr(kn, "embed", _boom)
    kb = store.create_kb("KB")
    # ingest surfaces the error
    with pytest.raises(kn.EmbeddingUnavailable):
        asyncio.run(kn.ingest_text(kb["id"], "x.md", "hello"))
    # the tool catches it and returns a clear message
    tools = kn.make_kb_tools({"knowledgeBases": [kb["id"]]})
    out = asyncio.run(tools["kb_search"]["fn"](query="hello"))
    assert "unavailable" in out.lower()
