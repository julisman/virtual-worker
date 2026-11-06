# Example manifest annotations

This file documents the deployable `template.json`; comments are intentionally
kept out of the JSON artifact.

- `schemaVersion`: manifest format. `2` enables the additive v2 sections.
- `sourceId` and `templateId`: stable lineage identity. The registry source ID
  should remain stable across releases; the template ID matches its folder.
- `name`, `description`, `role`, and `persona`: the agent's presentation and
  operating instructions. Secret placeholders are forbidden in persona text.
- `version`: content semver. A canonical digest change requires a higher
  version in the same lineage.
- `requiresFeatures`: capabilities needed by this template. It is derived here
  from the `customTools` and `connectors` sections.
- `variables`: values requested at deploy time. A `secret` variable is bound
  locally and never written into the registry.
- `customTools`: an inline HTTP tool. Secret placeholders are permitted only in
  header values; this example binds the local PAT to its Authorization header.
- `connectors`: a reference to a Virtual Worker connector preset. `secretBindings` maps a
  preset secret slot to a declared secret variable.
- `wikis`: a name and repository URL, with the local checkout supplied through
  a folder variable.
- `knowledgeBases`: a build recipe; local ingestion, chunks, and embeddings
  stay on the deploying machine.
- `mcpServers`: a requirement matched by name; secrets and machine-specific
  setup remain manual.
- `skills`: skill slugs to attach when available on the deploying Virtual Worker build.
- `metadata`: non-behavioral team-owned annotations.
