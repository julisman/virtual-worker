# Publishing a registry template

1. Export the tuned agent from Virtual Worker and save the result as
   `templates/<template-id>/template.json`.
2. Save the template to the gallery / registry workspace and review the
   deployable JSON. Keep credential values out; secret variables are bindings,
   not secrets.
3. Open a pull request against this registry. Run the lint job and review the
   digest, warnings, and schema changes.
4. If the canonical digest changed for an existing `(sourceId, templateId)`
   lineage, increment `version` with valid semver. A new lineage is a first
   publication and does not need an old version.
5. Merge the pull request. Members sync, download the new gallery template, and apply the
   update locally.

## Lint-pin bump checklist

- Select a protected immutable Virtual Worker tag or full commit SHA; never use `main`.
- Update `lint_ref` (and the `@ref` on the `uses:` line) in
  `.github/workflows/lint.yml`.
- Read the new tool README and confirm the pure-manifest API remains frozen.
- Regenerate `docs/schema.md` with the pinned tool:
  `python -m lint docs ../../registry-scaffold/docs/schema.md` (from the tool directory).
- Update `registry-scaffold/VERSION` if the scaffold marker is intentionally
  moving, then run the example lint and the tool's own pytest suite.
- Review the generated schema diff and commit it with the pin bump.
