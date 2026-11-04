# Contributing to Virtual Worker

Thanks for helping build a local-first home for AI workers.

## Ground rules

- **Local-first.** Any network egress must be explicit, visible, and opt-in.
- **Guardrails in code, not in prompts.** Safety bounds are enforced by the runtime.
- **Dependency-light core.** New runtime dependencies need a clear reason in the PR.
- **Plugins at the edges.** Chat channels, model providers, tools and skills go behind
  the documented interfaces. No company-specific integrations in the core.
- **Secrets are write-only** and redacted from logs and traces.

## Getting started

```bash
./dev.sh            # bootstraps and starts backend + frontend
```

Read `AGENTS.md` for the repository layout, conventions, and the architecture of the
agent harness. Read `docs/PRD.md` for the product plan and milestones.

## Making a change

1. Branch from `main`: `git checkout -b feat/<scope>-<desc>`.
2. Keep the change focused. One feature or fix per PR.
3. Add or update tests under `backend/tests/`. Keep them hermetic (no real model).
4. Both gates must pass before you open a PR:
   ```bash
   cd backend  && .venv/bin/python -m pytest -q
   cd frontend && npm run build
   ```
5. Use conventional commit messages: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`.
6. Open a PR against `main` and describe what changed and how you verified it.

## Reporting issues

Open a GitHub issue with steps to reproduce, the platform and model you used, and the
relevant lines from the backend log. Never paste secrets, tokens, or chat transcripts
you would not want public.

## License

By contributing you agree that your contributions are licensed under the Apache License
2.0, the same license as the project.
