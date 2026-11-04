"""``vw`` — run Virtual Worker as a supervised background service.

    vw start [--port 8888] [--home ~/.virtual-worker]   detach, auto-restart, built UI
    vw stop                                              stop the service (+ managed model server)
    vw restart
    vw status                                            pid, health, ports, uptime
    vw logs [-n 200] [-f]                                service log
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
