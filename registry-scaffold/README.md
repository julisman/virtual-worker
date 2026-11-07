# {{TEAM_NAME}} Virtual Worker template registry

Copy this directory into a new git repository and replace `{{TEAM_NAME}}`.
The registry convention is `templates/<template-id>/template.json`; deployable
JSON contains no comments or secret values.

For a **dedicated** template repository, keep this layout at the root (as the
scaffold ships). If the repository holds other things too, put the registry
content (`templates/` + `skills/`) under a `vw-registry/` directory — Virtual Worker
auto-detects that name — or under any directory of your choosing, which each
member then enters as the registry's *Subdirectory* when adding it in Virtual Worker.

## Minimum Virtual Worker build

This scaffold's example uses `schemaVersion: 2` and the `customTools` and
`connectors` features. Schema 2 also supports these portable sections:

- `wikis`: reference a wiki by name and repository URL, with its checkout path
  supplied through a folder variable.
- `knowledgeBases`: publish a build recipe while keeping local ingestion,
  chunks, and embeddings on the deploying machine.
- `mcpServers`: declare a requirement matched by server name; secrets and
  machine-specific setup remain manual.

Therefore the minimum Virtual Worker build is the first build that supports manifest
schema 2 plus the features used by the manifest. It is derived from the
manifest itself, not from the CI lint tool pin. If a future template uses a
newer schema or feature, update this statement from its `schemaVersion` and
`requiresFeatures` values.

The committed `VERSION` file is the scaffold/lint compatibility marker. A
mismatch is a warning during lint and can be updated as part of the lint-pin
bump checklist.

## Publishing

See [docs/publishing.md](docs/publishing.md). `.github/workflows/lint.yml` calls
the Virtual Worker lint workflow at a pinned ref; replace the placeholder owner and
ref with your fork and an immutable tag or full SHA before enabling the repository.
On another CI system, run `tools/template-lint/lint.py` from a pinned Virtual Worker
checkout the same way.
