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

## Releasing

Releases are cut from `main` by a maintainer:

1. Move the entries under `[Unreleased]` in `CHANGELOG.md` into a new version
   section with today's date, and update the compare links at the bottom.
2. Bump the version in `frontend/package.json`, `backend/app.py` (the FastAPI
   `version`) and `backend/storage/backup.py` (`APP_VERSION`) so the UI, the
   API docs and backup manifests agree.
3. Run both gates (`backend` pytest, `frontend` build) and the template lint.
4. Commit as `chore(release): vX.Y.Z`, tag it (`git tag -a vX.Y.Z`), push the
   branch and the tag, and wait for CI to pass.
5. Create the GitHub release from the tag and paste the changelog section as
   its notes. Template registries pin the lint tool to this tag.

## Reporting issues

Open a GitHub issue with steps to reproduce, the platform and model you used, and the
relevant lines from the backend log. Never paste secrets, tokens, or chat transcripts
you would not want public.

## License

By contributing you agree that your contributions are licensed under the Apache License
2.0, the same license as the project.
