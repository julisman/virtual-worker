# PyInstaller spec for the frozen Virtual Worker backend (run from the repo root).
#   PYTHONPATH=backend pyinstaller --clean --noconfirm desktop/backend.spec
# Produces <distpath>/vw-backend/  (a one-dir bundle: vw-backend + shared libs).
import os
from PyInstaller.utils.hooks import collect_submodules, collect_all

# Paths are resolved relative to this spec's directory (desktop/), so anchor to
# the repo root explicitly. SPECPATH is injected by PyInstaller.
ROOT = os.path.dirname(SPECPATH)
BACKEND = os.path.join(ROOT, "backend")

# Our own packages use some lazy `from X import Y` imports inside functions —
# pull in every submodule so nothing is missed by static analysis.
hidden = []
for pkg in ("routes", "runtime", "storage", "agent", "gateway", "middleware"):
    hidden += collect_submodules(pkg)
hidden += collect_submodules("uvicorn")

# MCP: pin the transports the backend actually uses. (Don't collect_all('mcp') —
# it drags in the optional `mcp.cli`, which needs `typer` we don't ship.)
hidden += ["mcp", "mcp.client", "mcp.client.session", "mcp.client.stdio",
           "mcp.client.streamable_http", "mcp.types"]

datas, binaries = [], []
# certifi: TLS cert bundle for httpx. playwright: its bundled node "driver" +
# package data must ride along or the browser tools can't launch a browser.
for pkg in ("certifi", "playwright"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

# Builtin skill packs — shipped read-only inside the bundle, installable from
# the Skills tab (config.builtin_skills_dir() finds them under _MEIPASS/skills).
datas += [(os.path.join(ROOT, "skills"), "skills")]

a = Analysis(
    [os.path.join(BACKEND, "desktop_main.py")],
    pathex=[BACKEND],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    # Excluded on purpose — optional/heavy; the backend import-guards each one.
    excludes=["mlx", "mlx_lm", "mlx_vlm", "mlx_audio",
              "torch", "transformers", "tokenizers", "tkinter", "pytest",
              "matplotlib", "pandas", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vw-backend",
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="vw-backend")
