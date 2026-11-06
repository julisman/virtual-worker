"""Frozen-backend entry point for the packaged desktop app.

The Electron launcher runs this (as a PyInstaller-built binary — no system Python
needed) instead of `uvicorn app:app`. It reads the port the launcher picked, marks
packaged mode, and serves the app (API + built frontend) on 127.0.0.1.
"""

import os


def main() -> None:
    os.environ.setdefault("VW_PACKAGED", "1")
    port = int(os.environ.get("VW_BACKEND_PORT", "8888"))

    # Import the app object directly (not an import-string) — reload/import-string
    # resolution is unreliable inside a frozen bundle.
    from app import app
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
