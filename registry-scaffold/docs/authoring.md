# Authoring a portable registry template

This guide is for the senior on-duty member who tunes an agent and publishes it
to a team registry. It explains the judgment behind a good manifest: what the
team should standardize, what each member must supply locally, and what should
be allowed to remain unfinished until deployment.

The [schema reference](schema.md) is the field contract. The
[publishing guide](publishing.md) covers the export, review, and versioning
mechanics. Use this document when deciding what belongs in the template in the
first place.

## The portability rule

Write a template as if it will be committed to a shared Git repository and
deployed on N different machines by N different members:

> A template carries definitions and references, never derived state or
> credentials. Anything machine- or member-specific becomes a typed variable or
> a Needs-attention item.

This is a safety rule, not merely a style preference. A committed manifest is
copied to every teammate's machine. It cannot safely contain your home paths,
your identity, your local database contents, your credential values, or an
index produced by a particular embedding model. Put the reusable recipe in the
manifest and let deployment resolve local inputs. If a local prerequisite
cannot be resolved automatically, make that visible as Needs attention rather
than pretending the template is complete.

| Section | Put in the template | Keep local or ask the member to finish |
| --- | --- | --- |
| `customTools` | Definition by value | Header secrets as secret variables |
| `connectors` | Preset reference and non-secret overrides | Credential slots as secret variables |
| `skills` | Slugs and their resolution path | Missing CLI tools as Needs attention |
| `mcpServers` | Requirement manifest matched by name | Environment, commands, and setup |
| `wikis` | Name and repository URL | Clone path through a folder variable |
| `knowledgeBases` | Build recipe | Local ingestion, chunks, and embeddings |

These choices also make future sections reviewable: a new section must obey the
same rule. A useful test is, “Would I be comfortable putting this value in a
team Git repository and asking a new teammate to use it?” If not, parameterize
it or leave a deliberate local action.

## Variable or hardcoded?

The central authoring decision is whether a value describes the shared agent or
the person and machine running it.

Make it a `{{variable}}` when it can differ between members or installations:

- member name, team or region, and other identity or routing choices;
- a working folder, wiki clone path, or SOP/source folder;
- a local model or skill choice when the fleet is not uniform;
- any credential, token, password, or API key.

Hardcode values that are genuinely part of the shared contract: the persona,
role, guardrails, stable descriptions, shared starter tasks, and a tool URL that
is the same for everyone. Hardcoding a value because it happens to be true on
the author's laptop is not a valid reason.

Give every variable a useful label and help text. Explain what the answer is
used for, not just its internal key. Keep defaults conservative: a default can
make a deployment look complete when the member should have made an explicit
choice. A variable is also the supported customization seam for updates; a
member's edit to a managed field is not.

### Choose the type deliberately

- `text` is for member- or team-specific text such as a name, region code, or
  short label. Do not use it to smuggle a path or credential into prose.
- `folder` is for a local directory, such as a wiki checkout or SOP folder.
  The member chooses the path on their machine; the template should not embed
  the author's path.
- `secret` is for credentials. Secrets are always variables of type `secret`
  and resolve through local bindings. Never place a literal secret in the
  manifest, metadata, examples, or a fixed parameter.

The other schema-supported types are useful when the choice itself has a
controlled shape; follow the field reference for their exact contract. Keep
the same portability test for all of them.

Secret placeholders are intentionally narrow. `{{secret_var}}` is legal only
in a declared secret-bearing leaf path, such as a connector slot or a custom
tool header. It is not legal in a persona, URL, description, arbitrary
free-text field, or fixed parameter. If a secret seems needed in one of those
places, redesign the integration or make the local setup explicit. The
binding records a local credential reference, never its value.

The worked example in [`templates/example-agent/template.json`](../templates/example-agent/template.json)
shows this pattern: `team-pat` is a required secret variable, and its
placeholder appears only in the tool's `Authorization` header and connector
binding. Its companion [annotations](../templates/example-agent/template.example.md)
explains the deployable JSON without putting comments into that artifact.

## Authoring each structural section

### `customTools`: copy the definition, parameterize credentials

Custom tools travel by value because the manifest must describe exactly what
the tool does: method, URL, parameters, fixed parameters, headers, and timeout.
Keep the definition small, stable, and readable. Use a distinct resource key
and display name that says what the operation is; vague names such as `search`
are hard to review and collide easily.

Header secrets become secret variables and bindings. Do not put a token in a
URL, fixed parameter, description, or example. A name collision with an
existing local tool triggers explicit adoption (with affected agents shown) or
a disambiguated new tool; it is not permission to mutate a same-named tool.
Choose a name that is distinctive to the team's operation to reduce that
friction. On update, only the ledger-owned copy is eligible for an upsert.

### `connectors`: reference a preset, do not export a credential

Connectors are references, not portable copies of a local integration. Specify
the preset and only non-secret overrides that are part of the shared setup.
Map each credential slot to a declared secret variable. A member may adopt an
existing same-named connector only through explicit confirmation; otherwise the
deployment creates the preset-backed local record.

Expect a newly deployed connector to start untested. That is honest and useful:
the member must verify it locally, and the result appears in Needs attention.
Updates reconcile ledger-owned attachments and non-secret overrides while
retaining secret slots and their bindings.

### `skills`: name capabilities, not local installations

List skill slugs only. Deployment resolves each slug in this order: an
installed skill is attached; then a builtin pack is installed (including its
declared closure); then a pack under the registry's `skills/<slug>/` is used;
if none is available, the item becomes Needs attention. Missing command-line
tools reported by installation also remain visible there.

If the team needs reliable availability, co-host the pack under the registry's
`skills/<slug>/` directory. That guarantees the source is present in the
registry, but it does not make machine-level CLI dependencies magically
portable. Keep those dependencies explicit and reviewable.

### `mcpServers`: declare a requirement, never create one

An MCP entry says which locally configured server is required, by name, with
its transport and a useful hint (and any allowed tool names). It must not carry
secrets, environment values, or a machine-specific command. Deployment matches
the requirement against configured servers. An unmatched server becomes Needs
attention; all creation and consent remain manual, including HTTP servers.

### `wikis`: share the source, parameterize the checkout

Use the wiki name and repository URL as the shared reference. Resolve the local
checkout through a `folder` variable such as `runbook-folder`; never commit the
author's absolute path. A member may attach an existing wiki or, with the same
explicit consent used for other repository fetches, clone the repository.

Imported trust tiers cannot raise the member's local trust floor. Let the normal
approval flow decide what is safe; a template is not a shortcut around wiki
trust.

### `knowledgeBases`: publish a recipe, not an index

Describe the KB's name, purpose, embedding model, and optional source-folder
variable. Deploy can attach a matching local KB or create an empty one for the
member to populate. Content, chunks, and embeddings stay local: they can be
large, contain private material, and are only valid for the model that produced
them. An empty or un-ingested KB is an expected Needs-attention item, not a
reason to ship derived data in the manifest.

## Design for updates

An update intentionally overwrites the template-managed, ledger-owned fields:
persona, role, description, sampling settings, built-in tools, skills,
guardrails, and ledger-owned tool or connector attachments. This is how a
fleet gets one corrected configuration instead of N divergent forks.

It keeps memory, sessions, the working directory, duty status, secret values
and bindings, KB content, variable answers, and resources attached by hand
(outside the deployment ledger). Starter tasks are not re-seeded. A member who
edits a managed field locally should expect that edit to be replaced on the
next update.

Therefore, put anything members must be able to retain or tailor behind a
variable. Do not ask members to customize the persona or tool definition after
deployment; encode the shared policy in the managed template and expose only
the intended variation as variables.

### Plan variable migration

Adding a required variable is compatible when it is intentional: the next
update prompts for exactly the missing answer before applying. Renaming a
variable or changing its type can invalidate a stored answer, so plan that
change as a migration and provide clear labels and help text. Secret-typed
changes prompt for a new local binding; they never copy a credential into the
manifest or ask the template to re-export it.

## Before you publish

- Check every value against the portability rule. Search especially URLs,
  fixed parameters, prompts, metadata, paths, and examples for personal or
  secret material.
- Confirm member-specific values are typed variables, with useful help text;
  confirm secret placeholders occur only in declared secret-bearing leaves.
- Review each structural section as a definition, reference, requirement, or
  recipe. Ensure local prerequisites can become clear Needs-attention items.
- Deploy or update the worked-on template on your own agent and verify the
  result, including the update/restore behavior and any connector test.
- Follow [publishing.md](publishing.md) for the export → verify → Download from
  the gallery card → pull request → version-bump flow. Let CI perform the manifest and
  secret lint; do not treat a clean local intuition as a substitute for it.

This guide pairs with [schema.md](schema.md) for fields and
[publishing.md](publishing.md) for mechanics, and targets the scaffold marker
in `VERSION` (`0.1.0`).
