"""Multi-protocol gateway.

Translates inbound Anthropic / OpenAI-Responses / Gemini requests into the
OpenAI Chat Completions shape the local upstream speaks, and translates the
responses (and streams) back into each caller's native wire format. So Claude
Code, Codex, and Gemini/Antigravity clients can all use one local model.

Pure translation lives in ``translate_*`` modules; the single upstream caller is
``upstream``. Routing is by URL path (the three surfaces never collide), so the
protocol is chosen by FastAPI's router, not by sniffing.
"""
