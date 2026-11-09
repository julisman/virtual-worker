"""vw CLI — service unit rendering, agent resolution, and `vw run` against a
scripted HTTP server that streams SSE like /api/chat does."""

import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import vw_cli


def test_render_units_contain_the_supervisor_command():
    home = pathlib.Path("/home/u/.virtual-worker")
    plist = vw_cli.render_launchd("/py", "/repo/backend/vw_cli.py", home, 8888, 8080, home / "logs" / "service.log")
    assert "<key>KeepAlive</key><true/>" in plist and "<key>RunAtLoad</key><true/>" in plist
    assert "<string>supervise</string>" in plist and "<string>--port</string>" in plist and "<string>8888</string>" in plist
    assert str(home / "logs" / "service.log") in plist and vw_cli.LAUNCHD_LABEL in plist
    unit = vw_cli.render_systemd("/py", "/repo/backend/vw_cli.py", home, 8890, 8081)
    assert "ExecStart=/py /repo/backend/vw_cli.py supervise --home /home/u/.virtual-worker --port 8890 --llama-port 8081" in unit
    assert "Restart=always" in unit and "WantedBy=default.target" in unit


def test_resolve_agent_by_id_name_or_unique_prefix():
    agents = [{"id": "a1", "name": "Support Rep"}, {"id": "a2", "name": "Sales"}, {"id": "a3", "name": "Sales EU"}]
    assert vw_cli.resolve_agent(agents, "a2")["id"] == "a2"
    assert vw_cli.resolve_agent(agents, "support rep")["id"] == "a1"
    assert vw_cli.resolve_agent(agents, "supp")["id"] == "a1"
    assert vw_cli.resolve_agent(agents, "sales")["id"] == "a2"          # exact name wins over prefix ambiguity
    assert vw_cli.resolve_agent(agents, "sal")is None                   # ambiguous prefix
    assert vw_cli.resolve_agent(agents, "nope") is None


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        body = json.dumps({"agents": [{"id": "a1", "name": "Helper"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append((self.path, json.loads(self.rfile.read(n) or b"{}"), self.headers.get("x-vw-session")))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for ev in ({"type": "tool_call", "name": "write_file", "args": {"path": "x"}},
                   {"type": "text", "delta": "Hello "}, {"type": "text", "delta": "world"}, {"type": "done"}):
            self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()


def test_vw_run_streams_text_and_sends_the_prompt(capsys):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        port = srv.server_address[1]
        rc = vw_cli.main(["run", "helper", "say", "hi", "--port", str(port), "--session", "chat:test"])
        out = capsys.readouterr()
        assert rc == 0 and out.out.strip() == "Hello world"
        assert "[tool] write_file" in out.err
        path, body, session = _Handler.seen[-1]
        assert path == "/api/chat" and body["agentId"] == "a1" and body["messages"][0]["content"] == "say hi"
        assert session == "chat:test"
        rc = vw_cli.main(["run", "helper", "again", "--port", str(port), "--json"])
        out = capsys.readouterr()
        assert rc == 0 and [json.loads(l)["type"] for l in out.out.strip().splitlines()] == ["tool_call", "text", "text", "done"]
        rc = vw_cli.main(["run", "nobody", "x", "--port", str(port)])
        assert rc == 2 and "no agent matches" in capsys.readouterr().err
    finally:
        srv.shutdown()


def test_vw_run_reports_unreachable_service(capsys):
    rc = vw_cli.main(["run", "helper", "x", "--port", "1"])
    assert rc == 2 and "cannot reach" in capsys.readouterr().err


def _frontend(tmp_path, built_at: float, src_at: float) -> pathlib.Path:
    import os
    fe = tmp_path / "frontend"
    (fe / "src" / "features").mkdir(parents=True)
    (fe / "dist").mkdir()
    src = fe / "src" / "features" / "page.tsx"
    src.write_text("x")
    idx = fe / "dist" / "index.html"
    idx.write_text("<html>")
    os.utime(src, (src_at, src_at))
    os.utime(idx, (built_at, built_at))
    return fe


def test_ui_stale_detects_newer_source_and_missing_build(tmp_path):
    assert not vw_cli.ui_stale(_frontend(tmp_path / "a", built_at=2000, src_at=1000))
    assert vw_cli.ui_stale(_frontend(tmp_path / "b", built_at=1000, src_at=2000))
    fe = _frontend(tmp_path / "c", built_at=2000, src_at=1000)
    (fe / "dist" / "index.html").unlink()
    assert vw_cli.ui_stale(fe)


def test_ensure_ui_builds_only_when_stale_and_keeps_old_build_on_failure(tmp_path, monkeypatch):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode = rc

    rc = {"v": 0}
    monkeypatch.setattr(vw_cli.subprocess, "run", lambda cmd, cwd: calls.append((cmd, cwd)) or R(rc["v"]))
    fresh = _frontend(tmp_path / "fresh", built_at=2000, src_at=1000)
    assert vw_cli.ensure_ui(fresh) and calls == []                     # up to date → no build
    stale = _frontend(tmp_path / "stale", built_at=1000, src_at=2000)
    assert vw_cli.ensure_ui(stale) and calls[-1] == (["npm", "run", "build"], stale)
    rc["v"] = 1                                                        # rebuild fails…
    assert vw_cli.ensure_ui(stale)                                     # …old build still served
    (stale / "dist" / "index.html").unlink()
    assert not vw_cli.ensure_ui(stale)                                 # nothing to serve → fail


def test_restart_builds_before_stopping(monkeypatch):
    order = []
    monkeypatch.setattr(vw_cli, "ensure_ui", lambda *a: order.append("build") or True)
    monkeypatch.setattr(vw_cli, "cmd_stop", lambda a: order.append("stop") or 0)
    monkeypatch.setattr(vw_cli, "_ui_built", lambda: True)
    monkeypatch.setattr(vw_cli, "cmd_start", lambda a: order.append(("start", a.no_build)) or 0)
    assert vw_cli.main(["restart"]) == 0
    assert order == ["build", "stop", ("start", True)]
    order.clear()
    assert vw_cli.main(["restart", "--no-build"]) == 0
    assert order == ["stop", ("start", True)]
