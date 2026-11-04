# Sample skills

Drop-in skill packs to test Virtual Worker's **Agents → Skills** upload.

Each `.md` file is a self-contained [agentskills.io](https://agentskills.io) skill:
YAML frontmatter (`name` / `description` / `when_to_use`) + a markdown body.

## Files here

| File | What it is |
|------|------------|
| `secret-handshake.md` / `.zip` | A verifiable skill — the model only knows the passphrase after loading it. Best for a first test. |
| `standup-notes.md` / `.zip` | Formats a standup update into a fixed structure. |
| `invoice-helper.zip` | A **bundle** demo: a `SKILL.md` plus a `template.txt` resource file. |

Both `.md` and `.zip` uploads are supported. A `.zip` must contain a `SKILL.md`
(at any depth); sibling files are kept as resources.

## Skill structure & leveling (progressive disclosure)

Skills follow the same 3-level model as the openAsetin engine — only what's
relevant reaches the model, so the prompt stays small:

```
data/skills/<slug>/
├── SKILL.md            ← L1 frontmatter (name/description/when_to_use/version)
│                          + L2 body (the full instructions)
├── references/         ← L3 resource files (markdown, text…)
├── assets/             ← L3 assets (configs, images…)
└── scripts/            ← L3 helper scripts
```

- **L1 — catalog**: every attached skill's `name`, `slug`, `description`
  (and `when_to_use`) are injected into the system prompt on every turn.
- **L2 — instructions**: the model calls `load_skill({slug})` to read the full
  `SKILL.md` body only when the skill becomes relevant.
- **L3 — resources**: `load_skill_resource({slug, path})` loads a bundled file
  (e.g. `references/template.txt`) on demand. `load_skill` lists what's available.

`invoice-helper.zip` demonstrates L3 — its `SKILL.md` tells the model to load
`references/template.txt` before drafting an invoice.

### Frontmatter fields

| Field | Required | Notes |
|-------|----------|-------|
| `name` | yes | display name; the slug is derived from it |
| `description` | yes | shown in the L1 catalog |
| `when_to_use` | no | hint for when the skill applies (shown in catalog) |
| `version` | no | defaults to `1.0.0`; shown in the L2 header |

## How to test

1. Go to **Agents → Skills** and upload `secret-handshake.md`.
2. Go to **Agents → Agents**, create an agent (or edit *Assistant*), and attach
   the **Secret Handshake** skill. Save.
3. Open **Chat**, pick that agent in the "as …" selector, and ask:
   > do the secret handshake
4. Watch the step trail show **Loading skill** — the model only knows the
   passphrase `AZURE-FALCON-2026` because it loaded the skill body on demand
   (progressive disclosure: the prompt only ever saw the description).

`.zip` bundles work too — zip a folder containing a `SKILL.md` (plus any
resource files) and upload it the same way.
