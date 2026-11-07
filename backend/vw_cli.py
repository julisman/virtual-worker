"""``vw`` — run Virtual Worker as a supervised background service.

    vw start [--port 8888] [--home ~/.virtual-worker]   detach, auto-restart, built UI
    vw stop                                              stop the service (+ managed model server)
    vw restart
    vw status                                            pid, health, ports, uptime
    vw logs [-n 200] [-f]                                service log
    vw run <agent> "<prompt>" [--session id] [--json]    headless one-shot run via the API
    vw install-service [--port 8888]                     start at login/boot (launchd / systemd --user)
    vw uninstall-service
    vw supervise                                         (internal) foreground supervisor loop

Service mode runs ``uvicorn app:app`` **without --reload** and serves the built
frontend from the same origin (``VW_SERVE_UI=1``), so a WhatsApp/Telegram worker
keeps running after the terminal closes. The supervisor restarts the backend
with exponential backoff if it exits, and is itself detached in its own session
with a pid file under ``<VW_HOME>/service.json``. ``./dev.sh`` (hot reload, Vite)
uses separate default ports so both can run side by side.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
DEFAULT_HOME = Path(os.environ.get("VW_HOME") or (Path.home() / ".virtual-worker"))
DEFAULT_PORT = int(os.environ.get("VW_PORT") or 8888)
DEFAULT_LLAMA_PORT = int(os.environ.get("LLAMA_PORT") or 8080)


def _home(args) -> Path:
    h = Path(args.home).expanduser() if getattr(args, "home", None) else DEFAULT_HOME
    h.mkdir(parents=True, exist_ok=True)
    return h


def _pid_file(home: Path) -> Path:
    return home / "service.json"


def _log_file(home: Path) -> Path:
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return home / "logs" / "service.log"


def _read_state(home: Path) -> dict | None:
    try:
        return json.loads(_pid_file(home).read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _health(port: int, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _python() -> str:
    venv = BACKEND_DIR / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _ui_built() -> bool:
    return (ROOT / "frontend" / "dist" / "index.html").exists()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_start(args) -> int:
    home = _home(args)
    st = _read_state(home)
    if st and pid_alive(st.get("pid")):
        print(f"already running (pid {st['pid']}) on http://127.0.0.1:{st.get('port')}")
        return 0
    if not _ui_built():
        print("building the frontend (first run)…")
        r = subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend")
        if r.returncode != 0:
            print("frontend build failed — fix it, or run with the dev launcher", file=sys.stderr)
            return 1
    log = open(_log_file(home), "ab")
    env = {**os.environ, "VW_HOME": str(home), "VW_SERVE_UI": "1",
           "LLAMA_PORT": str(args.llama_port)}
    proc = subprocess.Popen(
        [_python(), str(BACKEND_DIR / "vw_cli.py"), "supervise", "--port", str(args.port),
         "--home", str(home), "--llama-port", str(args.llama_port)],
        cwd=BACKEND_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _pid_file(home).write_text(json.dumps({"pid": proc.pid, "port": args.port, "home": str(home),
                                           "llamaPort": args.llama_port, "startedAt": time.time()}))
    for _ in range(60):
        time.sleep(0.5)
        if _health(args.port):
            print(f"Virtual Worker is running → http://127.0.0.1:{args.port}  (pid {proc.pid})")
            print(f"logs: {_log_file(home)}")
            return 0
        if not pid_alive(proc.pid):
            break
    print(f"started supervisor (pid {proc.pid}) but the API isn't healthy yet — check: vw logs", file=sys.stderr)
    return 1


def _stop_llama(home: Path) -> None:
    try:
        st = json.loads((home / "llama-server.json").read_text())
        pid = st.get("pid")
        if pid_alive(pid):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


def cmd_stop(args) -> int:
    home = _home(args)
    _kind, unit = service_paths()
    if unit.exists():
        print(f"note: a login service is installed ({unit}); it will restart the backend. "
              f"Use `vw uninstall-service` to stop it for good.", file=sys.stderr)
    st = _read_state(home)
    if not st or not pid_alive(st.get("pid")):
        print("not running")
        _pid_file(home).unlink(missing_ok=True)
        return 0
    pid = st["pid"]
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(40):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not getattr(args, "keep_model", False):
        _stop_llama(home)
    _pid_file(home).unlink(missing_ok=True)
    print("stopped")
    return 0


def cmd_status(args) -> int:
    home = _home(args)
    st = _read_state(home)
    port = (st or {}).get("port") or args.port
    running = bool(st and pid_alive(st.get("pid")))
    health = _health(port)
    print(f"service : {'running' if running else 'stopped'}" + (f" (pid {st['pid']})" if running else ""))
    print(f"api     : {'healthy' if health else 'unreachable'} → http://127.0.0.1:{port}")
    if st and st.get("startedAt"):
        up = int(time.time() - float(st["startedAt"]))
        print(f"uptime  : {up // 3600}h {(up % 3600) // 60}m")
    print(f"home    : {home}")
    print(f"logs    : {_log_file(home)}")
    return 0 if running and health else 1


def cmd_logs(args) -> int:
    home = _home(args)
    f = _log_file(home)
    if not f.exists():
        print("no log yet")
        return 0
    if args.follow:
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", str(f)])
    lines = f.read_text(errors="replace").splitlines()[-args.lines:]
    print("\n".join(lines))
    return 0


def cmd_supervise(args) -> int:
    """Foreground loop: run uvicorn, restart it with backoff when it exits."""
    home = _home(args)
    stopping = {"flag": False}
    child: dict = {"proc": None}

    def _term(signum, frame):
        stopping["flag"] = True
        p = child["proc"]
        if p and p.poll() is None:
            p.terminate()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    delay = 1.0
    env = {**os.environ, "VW_HOME": str(home), "VW_SERVE_UI": "1", "LLAMA_PORT": str(args.llama_port)}
    while not stopping["flag"]:
        started = time.time()
        print(f"[vw] starting backend on :{args.port}", flush=True)
        child["proc"] = subprocess.Popen(
            [_python(), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(args.port),
             "--no-access-log"],
            cwd=BACKEND_DIR, env=env)
        rc = child["proc"].wait()
        if stopping["flag"]:
            break
        ran = time.time() - started
        delay = 1.0 if ran > 60 else min(30.0, delay * 2)
        print(f"[vw] backend exited (code {rc}) after {int(ran)}s — restarting in {delay:.0f}s", flush=True)
        time.sleep(delay)
    print("[vw] supervisor stopped", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# vw run — headless one-shot agent run through the running service
# --------------------------------------------------------------------------- #

def _api(port: int, path: str, body: dict | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                               headers={"Content-Type": "application/json"},
                               method="POST" if body is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "null")


def resolve_agent(agents: list[dict], ref: str) -> dict | None:
    """Match by id, exact name, or unique case-insensitive name prefix."""
    ref = (ref or "").strip()
    for a in agents:
        if a.get("id") == ref:
            return a
    exact = [a for a in agents if (a.get("name") or "").lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]
    prefix = [a for a in agents if (a.get("name") or "").lower().startswith(ref.lower())]
    return prefix[0] if len(prefix) == 1 else None


def cmd_run(args) -> int:
    port = args.port
    try:
        agents = _api(port, "/api/agents").get("agents", [])
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach Virtual Worker on :{port} ({exc}) — start it with `vw start`, "
              f"or pass --port 8889 for a ./dev.sh instance", file=sys.stderr)
        return 2
    agent = resolve_agent(agents, args.agent)
    if not agent:
        names = ", ".join(a.get("name") or a.get("id") for a in agents) or "(none)"
        print(f"no agent matches {args.agent!r}; agents: {names}", file=sys.stderr)
        return 2
    prompt = " ".join(args.prompt).strip() or sys.stdin.read().strip()
    if not prompt:
        print("prompt is required (argument or stdin)", file=sys.stderr)
        return 2
    session = args.session or f"chat:cli-{int(time.time())}"
    body = json.dumps({"agentId": agent["id"], "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-vw-session": session})
    rc = 0
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if args.json:
                    print(json.dumps(ev), flush=True)
                    if ev.get("type") == "error":
                        rc = 1
                    continue
                t = ev.get("type")
                if t == "text":
                    sys.stdout.write(ev.get("delta", "")); sys.stdout.flush()
                elif t == "text_reset":
                    sys.stdout.write("\n")
                elif t == "tool_call":
                    print(f"[tool] {ev.get('name')} {json.dumps(ev.get('args') or {})[:160]}", file=sys.stderr, flush=True)
                elif t == "error":
                    print(f"\n[error] {ev.get('message')}", file=sys.stderr, flush=True); rc = 1
            if not args.json:
                sys.stdout.write("\n")
    except Exception as exc:  # noqa: BLE001
        print(f"run failed: {exc}", file=sys.stderr)
        return 1
    return rc


# --------------------------------------------------------------------------- #
# vw install-service — start at login / boot and survive reboots
# --------------------------------------------------------------------------- #

LAUNCHD_LABEL = "dev.virtualworker.service"
SYSTEMD_UNIT = "virtual-worker.service"


def render_launchd(python: str, cli: str, home: Path, port: int, llama_port: int, log: Path) -> str:
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local" / "bin")
    args = [python, cli, "supervise", "--home", str(home), "--port", str(port), "--llama-port", str(llama_port)]
    items = "".join(f"\n      <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{items}
  </array>
  <key>WorkingDirectory</key><string>{Path(cli).parent}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path_env}</string>
    <key>HOME</key><string>{Path.home()}</string>
    <key>VW_HOME</key><string>{home}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def render_systemd(python: str, cli: str, home: Path, port: int, llama_port: int) -> str:
    exec_start = f"{python} {cli} supervise --home {home} --port {port} --llama-port {llama_port}"
    return f"""[Unit]
Description=Virtual Worker — local-first AI workers
After=network-online.target

[Service]
Type=simple
WorkingDirectory={Path(cli).parent}
Environment=VW_HOME={home}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:{Path.home() / ".local" / "bin"}
ExecStart={exec_start}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def service_paths() -> tuple[str, Path]:
    """(kind, unit file path) for this platform."""
    if sys.platform == "darwin":
        return "launchd", Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return "systemd", Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def cmd_install_service(args) -> int:
    home = _home(args)
    kind, unit = service_paths()
    unit.parent.mkdir(parents=True, exist_ok=True)
    log = _log_file(home)
    cli = str(BACKEND_DIR / "vw_cli.py")
    if kind == "launchd":
        unit.write_text(render_launchd(_python(), cli, home, args.port, args.llama_port, log))
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(unit)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(unit)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"wrote {unit} but launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"installed launchd agent {LAUNCHD_LABEL} → http://127.0.0.1:{args.port}")
        print(f"unit: {unit}\nlogs: {log}\nIt starts at login and restarts if it dies. Remove with: vw uninstall-service")
    else:
        unit.write_text(render_systemd(_python(), cli, home, args.port, args.llama_port))
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"wrote {unit} but `{' '.join(cmd)}` failed: {r.stderr.strip()}", file=sys.stderr)
                return 1
        print(f"installed systemd user unit {SYSTEMD_UNIT} → http://127.0.0.1:{args.port}")
        print(f"unit: {unit}\nTo survive logout too: loginctl enable-linger {os.environ.get('USER', '')}")
    return 0


def cmd_uninstall_service(args) -> int:
    kind, unit = service_paths()
    if kind == "launchd":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(unit)], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], capture_output=True)
    if unit.exists():
        unit.unlink()
        print(f"removed {unit}")
    else:
        print("no service installed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vw", description="Virtual Worker service control")
    ap.add_argument("--home", help=f"data dir (default {DEFAULT_HOME})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    subs = {}
    for name in ("start", "restart", "supervise", "status"):
        p = subs[name] = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--llama-port", type=int, default=DEFAULT_LLAMA_PORT)
    p = subs["stop"] = sub.add_parser("stop")
    p.add_argument("--keep-model", action="store_true", help="leave the managed model server running")
    p = subs["logs"] = sub.add_parser("logs")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.add_argument("-f", "--follow", action="store_true")
    p = subs["run"] = sub.add_parser("run", help="headless one-shot agent run via the running service")
    p.add_argument("agent", help="agent id or name")
    p.add_argument("prompt", nargs="*", help="the prompt (or pipe it on stdin)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--session", help="chat session id to continue (default: a fresh cli session)")
    p.add_argument("--json", action="store_true", help="print raw loop events as JSON lines")
    p.add_argument("--timeout", type=float, default=900)
    p = subs["install-service"] = sub.add_parser("install-service", help="start at login/boot and keep alive")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--llama-port", type=int, default=DEFAULT_LLAMA_PORT)
    subs["uninstall-service"] = sub.add_parser("uninstall-service")
    for p in subs.values():  # --home is accepted before or after the subcommand
        p.add_argument("--home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not getattr(args, "home", None):
        args.home = None
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "restart":
        cmd_stop(argparse.Namespace(home=args.home, keep_model=True))
        return cmd_start(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "supervise":
        return cmd_supervise(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "install-service":
        return cmd_install_service(args)
    if args.cmd == "uninstall-service":
        return cmd_uninstall_service(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
