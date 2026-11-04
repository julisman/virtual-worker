# Security

## Model

Virtual Worker is **local-first**. The backend binds to `127.0.0.1` and is meant to be
used from the same machine. Everything that leaves the machine is explicit and opt-in:
a connected chat channel, a model provider you configured, web search, a webhook
target, a remote MCP server.

What the code enforces:

- **Browser isolation.** CORS allows local origins only (plus `VW_CORS_ORIGINS`), never
  `*`. The Host header is checked against `localhost`, `127.0.0.1` and
  `VW_ALLOWED_HOSTS` to defeat DNS rebinding.
- **Inbound surfaces.** The only inbound endpoints are webhooks, and each carries its own
  proof: WhatsApp Cloud API events are HMAC-verified against the app secret, trigger
  webhooks use an unguessable per-trigger token. Everything else is outbound.
- **Secrets are write-only.** Channel and provider credentials are stored once in
  `settings.json`, never returned by the API, and credential-shaped strings are
  scrubbed from log lines.
- **Agent guardrails in code.** File tools are confined to the working folder, the shell
  deny-list refuses destructive, privileged, network and publish commands, iteration and
  daily caps bound runaway loops, and the duty switch is a kill switch.
- **Chat channels.** Unknown senders are gated by the DM policy (pairing by default),
  groups answer only when mentioned, reasoning is never sent externally, the account
  never messages itself, and every outbound message is audited.
- **Gateway tokens.** Once the first API token exists, the model gateway requires one;
  only hashes are stored.

What it does **not** do (yet):

- Solo mode has no login. Anyone with shell access to the machine can use the API.
  Team mode (accounts and roles) exists behind a flag and has not had its own
  hardening pass; do not expose it to the internet.
- Exposing the backend beyond localhost (for the Cloud API webhook, for example) is your
  responsibility: put it behind a reverse proxy with TLS and set `VW_ALLOWED_HOSTS`.
- The WhatsApp linked-device transport uses an unofficial client library. Use a
  dedicated number.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Email the maintainer listed
in the repository profile, or use GitHub's private vulnerability reporting if it is
enabled. Include steps to reproduce and the version or commit. You will get an
acknowledgement within a few days.
