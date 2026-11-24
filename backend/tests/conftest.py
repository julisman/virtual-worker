"""Keep the test suite away from the developer's real Virtual Worker.

``config`` resolves the data dir (``VW_HOME``, default ``~/.virtual-worker``) and the
model-server port (``LLAMA_PORT``, default 8080) once, at import. Without this file
the suite used the live ones: runtime tests wrote fake ``/m/x.gguf`` starts into the
real ``llama-server.log``, deleted the real ``llama-server.json``, and a ``force=True``
start freed port 8080 by killing whatever model server was serving there.

pytest imports this before any test module, so setting the environment here wins
over every ``import config`` in the suite.
"""

import atexit
import os
import shutil
import socket
import tempfile


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_home = tempfile.mkdtemp(prefix="vw-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)

os.environ["VW_HOME"] = _home
os.environ["LLAMA_PORT"] = str(_unused_port())   # nothing listens: no live server to take over
os.environ.pop("VW_UPSTREAM_URL", None)          # derive the upstream from that port
os.environ.pop("LLAMA_MODEL", None)              # no autostart of a real model
