# Virtual Worker template-lint

`template-lint` is a Python-stdlib-only CI validator for registry manifests.
It loads the pure validator in `backend/storage/template_manifest.py` directly
by file path, so using the tool does not start Virtual Worker or read its configuration.

From this directory:

```bash
python -m lint lint ../../registry-scaffold/templates/*/template.json
python -m lint lint templates/*/template.json --baseline ../registry-before
python -m lint docs ../registry-scaffold/docs/schema.md
```

The same commands work with `python lint.py`. `lint` prints one result and the
canonical SHA-256 digest per file. A changed digest requires a higher semver
version for the same `(sourceId, templateId)` lineage. With `--baseline`, the
prior checkout is searched recursively. Without it, the tool resolves a git
merge-base against `origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` (or the
default branch / `main`). New templates pass; removed and moved templates are
not treated as republished versions. A shallow checkout that cannot resolve a
merge-base is a blocking, actionable error: fetch the target branch or run
`git fetch --unshallow`.

Secret-shaped values are blocking in credential contexts (private keys, AWS or
GitLab/GitHub tokens, real Bearer credentials, and long tokens in headers) and
warnings elsewhere (for example a long hex string in a persona). A reviewed
finding can be suppressed with its printed 16-character fingerprint in the
nearest `.template-lint-ignore`, one per line; keep a comment beside each
reviewed fingerprint explaining why it is safe.

`--docs [outfile]` (or the `docs` command) generates the schema from
`schema_fields()`. The generated marker records the lint version. `VERSION`
markers in a scaffold and schema-doc markers are warnings when they differ;
they never make a build fail.
