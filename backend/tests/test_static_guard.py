"""The SPA-fallback file server must confine reads to the built-frontend root
(no path traversal) and the CORS layer must allowlist origins, not wildcard."""

import importlib

from app import _safe_static_file


def _build_static(tmp_path):
    root = tmp_path / "frontend" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return root.resolve()


def test_serves_real_asset_under_root(tmp_path):
    root = _build_static(tmp_path)
    f = _safe_static_file(root, "assets/app.js")
    assert f is not None and f.name == "app.js"


def test_blocks_dot_dot_traversal(tmp_path):
    root = _build_static(tmp_path)
    # An encoded ../ that escapes the root must resolve to None (→ index.html).
    assert _safe_static_file(root, "../secret.txt") is None
    assert _safe_static_file(root, "../../etc/passwd") is None


def test_blocks_absolute_path_escape(tmp_path):
    root = _build_static(tmp_path)
    assert _safe_static_file(root, str(tmp_path / "secret.txt")) is None


def test_empty_and_missing_fall_through(tmp_path):
    root = _build_static(tmp_path)
    assert _safe_static_file(root, "") is None
    assert _safe_static_file(root, "does-not-exist.js") is None


def test_cors_is_local_only_not_wildcard(monkeypatch):
    # The API fronts real tools; a "*" origin would allow browser drive-by.
    import re
    import config
    monkeypatch.delenv("VW_CORS_ORIGINS", raising=False)
    cfg = importlib.reload(config)
    assert "*" not in cfg.CORS_ORIGINS
    rx = re.compile(cfg.CORS_ORIGIN_REGEX)
    assert rx.match("http://localhost:5173") and rx.match("http://127.0.0.1:8888")
    assert not rx.match("https://evil.example") and not rx.match("http://localhost.evil.example")


def test_cors_origins_env_override(monkeypatch):
    import config
    monkeypatch.setenv("VW_CORS_ORIGINS", "https://a.example, https://b.example")
    cfg = importlib.reload(config)
    assert cfg.CORS_ORIGINS == ["https://a.example", "https://b.example"]
    monkeypatch.delenv("VW_CORS_ORIGINS", raising=False)
    importlib.reload(config)
