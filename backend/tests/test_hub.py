"""Unit tests for the Hub — pure logic only (no network calls)."""

import asyncio

import pytest

import routes.hub as hub


@pytest.mark.parametrize("filename,expected", [
    ("gemma-4-12B-it-qat-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
    ("SmolLM2-135M-Instruct-Q2_K.gguf", "Q2_K"),
    ("Qwen-AgentWorld-35B-A3B-Q5_K_S.gguf", "Q5_K_S"),
    ("model-IQ2_XXS.gguf", "IQ2_XXS"),
    ("model-BF16-00002-of-00002.gguf", "BF16"),
    ("no-quant-here.gguf", None),
])
def test_quant_label(filename, expected):
    assert hub._quant_label(filename) == expected


def test_repo_cache_dir_layout():
    d = hub._repo_cache_dir("unsloth/SmolLM2-135M-Instruct-GGUF")
    assert d.name == "models--unsloth--SmolLM2-135M-Instruct-GGUF"


def test_fit_verdict(monkeypatch):
    # Pretend the box has exactly 10 GiB usable.
    monkeypatch.setattr(hub, "_vram_bytes", lambda: (10 * hub.GIB, "test"))
    assert hub._fit_for(None) == "unknown"
    assert hub._fit_for(2 * hub.GIB) == "fits"          # 2*1.25=2.5 <= 8.5
    assert hub._fit_for(9 * hub.GIB) == "tight"         # >85% budget, still <= budget
    assert hub._fit_for(20 * hub.GIB) == "too_large"    # bigger than budget


def test_download_validation(monkeypatch):
    # missing filename for an HF download -> 400
    from fastapi import HTTPException
    req = hub.DownloadRequest(source="hf", repo="unsloth/x")
    with pytest.raises(HTTPException) as e:
        asyncio.run(hub.start_download(req))
    assert e.value.status_code == 400


def test_cancel_unknown_job():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(hub.cancel_download("nope"))
    assert e.value.status_code == 404


def test_job_public_shape():
    job = hub.DownloadJob("j1", "hf", "a/b", "f.gguf")
    job.total, job.received = 200, 50
    pub = job.public()
    assert pub["percent"] == 25.0
    assert pub["status"] == "starting"
    assert pub["repo"] == "a/b"
