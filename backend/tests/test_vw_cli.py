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
