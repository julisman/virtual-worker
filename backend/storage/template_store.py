"""virtual worker Templates — one-click deployable agent blueprints (PRD-05).

A *template* is a portable blueprint that fully describes a deployable Virtual
Employee: persona, recommended model + sampling, recommended built-in tools,
skills, guardrails, optional starter tasks, and a typed ``variables`` schema so
deploy is a guided form (``{{business_name}}`` style substitution).

Two sources, local-first:

* **built-in** — a curated read-only gallery shipped with Virtual Worker (defined as Python
  data below). These never touch disk.
* **user / imported** — JSON manifests persisted under
  ``<VW_HOME>/templates/<id>/template.json``. The files are the source of
  truth; importing validates + scrubs before writing.

**Deploy** stamps a template into a real agent via :mod:`storage.agent_store`
(the exact same row a hand-built agent uses), substituting ``{{variables}}`` and
enforcing the guardrail floor. **Export** reads an existing agent back into a
template, **scrubbing any secrets** (api keys / tokens / passwords) by default —
nothing private ever lands in a shared template (FR-14, D2).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone

import config
from storage import agent_store, task_store

# Keys whose *values* are redacted out of any exported/imported template — never
# ship a secret in a portable, shareable file (FR-14 / D2).
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|bearer|credential|private[_-]?key)",
    re.I,
)

# Template fields that carry free text the deployer substitutes {{variables}} into.
_TEXT_FIELDS = ("name", "description", "persona", "role")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "template"


def _templates_dir():
    return config.ensure_dir(config.APP_HOME / "templates")


# --------------------------------------------------------------------------- #
# Built-in gallery (Python data — read-only, never written to disk)
# --------------------------------------------------------------------------- #

BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "customer-support-rep",
        "name": "{{business_name}} Support Rep",
        "description": "A friendly front-line support agent that answers customer "
        "questions and resolves common issues for your business.",
        "category": "Support",
        "role": "Customer Support Representative",
        "persona": (
            "You are a customer support representative for {{business_name}}. "
            "You are warm, patient, and clear. Greet the customer, understand "
            "their problem, and resolve it in as few steps as possible. If you "
            "don't know an answer, say so honestly and offer to escalate rather "
            "than guessing. Keep replies concise and action-oriented."
        ),
        "model": None,
        "temperature": 0.5,
        "topP": 0.95,
        "topK": 64,
        "tools": ["web_search", "web_fetch"],
        "skills": [],
        "guardrails": {"shell": "off", "verify": True, "maxIterations": 6, "dailyCap": 60},
        "variables": [
            {"key": "business_name", "label": "Business name", "type": "text",
             "default": "Acme", "required": True,
             "help": "Used in the greeting and throughout the persona."},
        ],
        "starterTasks": [
            {"title": "Draft a top-10 FAQ for {{business_name}}",
             "description": "Compile the ten questions customers ask most and write "
             "a clear answer for each."},
        ],
    },
    {
        "id": "research-analyst",
        "name": "Research Analyst",
        "description": "Digs into a topic across the web, weighs sources, and writes "
        "a structured, cited brief.",
        "category": "Research",
        "role": "Research Analyst",
        "persona": (
            "You are a meticulous research analyst specializing in {{domain}}. "
            "Given a question, you search broadly, cross-check claims against "
            "multiple sources, separate fact from speculation, and produce a "
            "concise structured brief with citations. You flag uncertainty "
            "explicitly and never fabricate a source."
        ),
        "model": None,
        "temperature": 0.4,
        "topP": 0.95,
        "topK": 64,
        "tools": ["web_search", "web_fetch"],
        "skills": [],
        "guardrails": {"shell": "readonly", "verify": True, "maxIterations": 10, "dailyCap": 40},
        "variables": [
            {"key": "domain", "label": "Research domain", "type": "text",
             "default": "general topics", "required": False,
             "help": "The subject area this analyst specializes in."},
        ],
        "starterTasks": [],
    },
    {
        "id": "code-reviewer",
        "name": "Code Reviewer",
        "description": "Reviews changes in a working folder for correctness, "
        "readability, security, and performance.",
        "category": "Engineering",
        "role": "Code Reviewer",
        "persona": (
            "You are a senior code reviewer. You read changes in the working "
            "folder and review them across five axes: correctness, readability, "
            "architecture, security, and performance. You give specific, "
            "actionable feedback referencing exact files and lines, lead with the "
            "highest-impact issues, and never auto-edit code — you only read and "
            "advise. Praise what's done well, then list concrete fixes."
        ),
        "model": None,
        "temperature": 0.2,
        "topP": 0.95,
        "topK": 64,
        "tools": [],
        "skills": [],
        # Read-only shell: a reviewer inspects, it does not mutate the repo.
        "guardrails": {"shell": "readonly", "verify": True, "maxIterations": 12, "dailyCap": 60},
        "variables": [
            {"key": "working_dir", "label": "Repository folder", "type": "folder",
             "default": "", "required": True,
             "help": "The project folder to review. Grants scoped read tools."},
        ],
        "starterTasks": [],
    },
    {
        "id": "personal-assistant",
        "name": "Personal Assistant",
        "description": "A general-purpose assistant that drafts, plans, summarizes, "
        "and keeps {{owner_name}} organized.",
        "category": "Productivity",
        "role": "Personal Assistant",
        "persona": (
            "You are {{owner_name}}'s personal assistant. You help with drafting "
            "messages, planning, summarizing, and day-to-day organization. You are "
            "proactive but never presumptuous: you confirm before taking "
            "consequential actions, keep things tidy, and adapt to "
            "{{owner_name}}'s preferences over time."
        ),
        "model": None,
        "temperature": 0.7,
        "topP": 0.95,
        "topK": 64,
        "tools": ["web_search", "web_fetch"],
        "skills": [],
        "guardrails": {"shell": "auto", "verify": True, "maxIterations": 6, "dailyCap": 60},
        "variables": [
            {"key": "owner_name", "label": "Your name", "type": "text",
             "default": "the owner", "required": False,
             "help": "Who the assistant works for."},
            {"key": "working_dir", "label": "Working folder", "type": "folder",
             "default": "", "required": False,
             "help": "Optional folder to grant file tools."},
        ],
        "starterTasks": [],
    },
    {
        "id": "intern",
        "name": "Intern",
        "description": "An eager generalist intern that takes tasks off the board "
        "and works them autonomously under tight guardrails.",
        "category": "General",
        "role": "Generalist Intern",
        "persona": (
            "You are a hard-working intern at {{business_name}}. You pick up tasks "
            "from your board, work them carefully one at a time, ask when you're "
            "blocked, and report what you did. You stay strictly within your "
            "guardrails and never take destructive shortcuts."
        ),
        "model": None,
        "temperature": 0.6,
        "topP": 0.95,
        "topK": 64,
        "tools": ["web_search", "web_fetch"],
        "skills": [],
        "guardrails": {"shell": "auto", "verify": True, "maxIterations": 6, "dailyCap": 30},
        "variables": [
            {"key": "business_name", "label": "Business / team name", "type": "text",
             "default": "the team", "required": False,
             "help": "Where the intern works."},
            {"key": "working_dir", "label": "Working folder", "type": "folder",
             "default": "", "required": False, "help": "Optional folder to grant file tools."},
        ],
        "starterTasks": [],
    },
]

_BUILTIN_BY_ID = {t["id"]: t for t in BUILTIN_TEMPLATES}

# Canonical field set for a template manifest (used to normalize on import/export).
_DEFAULTS = {
    "name": "Untitled template",
    "description": "",
    "category": "General",
    "role": "",
    "persona": "",
    "model": None,
    "temperature": 0.7,
    "topP": None,
    "topK": None,
    "tools": [],
    "skills": [],
    "guardrails": {},
    "variables": [],
    "starterTasks": [],
    "version": "1.0.0",
}


# --------------------------------------------------------------------------- #
# Secret scrubbing
# --------------------------------------------------------------------------- #

def scrub_secrets(obj):
    """Recursively redact secret-looking keys from a template-ish structure.

    Any dict key matching ``api_key``/``secret``/``token``/``password``/… has its
    value dropped. Returns a *new* structure; the input is left untouched.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                continue  # drop the secret entirely (don't even keep a placeholder)
            out[k] = scrub_secrets(v)
        return out
    if isinstance(obj, list):
        return [scrub_secrets(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# Normalization / validation
# --------------------------------------------------------------------------- #

def _normalize_variables(raw) -> list[dict]:
    out: list[dict] = []
    for v in raw or []:
        if not isinstance(v, dict):
            continue
        key = str(v.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": str(v.get("label") or key),
            "type": v.get("type") if v.get("type") in
            ("text", "folder", "model", "skill", "secret", "enum") else "text",
            "default": v.get("default", ""),
            "required": bool(v.get("required", False)),
            "help": str(v.get("help") or ""),
        })
    return out


def _normalize(tpl: dict, source: str) -> dict:
    """Coerce an arbitrary dict into a canonical template manifest.

    Unknown keys are dropped, missing keys get defaults, guardrails are pushed
    through the agent-store floor (a template may only *tighten*, never weaken,
    the destructive deny-list — FR-10), and secrets are scrubbed.
    """
    tpl = scrub_secrets(tpl or {})
    out = {**_DEFAULTS}
    for k in _DEFAULTS:
        if k in tpl and tpl[k] is not None:
            out[k] = tpl[k]
    out["id"] = str(tpl.get("id") or _slugify(out["name"]))
    out["name"] = str(out["name"]).strip() or "Untitled template"
    out["description"] = str(out["description"] or "")
    out["category"] = str(out["category"] or "General")
    out["role"] = str(out["role"] or "")
    out["persona"] = str(out["persona"] or "")
    out["tools"] = [str(x) for x in (out["tools"] or [])]
    out["skills"] = [str(x) for x in (out["skills"] or [])]
    out["variables"] = _normalize_variables(out["variables"])
    out["starterTasks"] = [
        {"title": str(t.get("title") or "").strip(),
         "description": str(t.get("description") or "").strip()}
        for t in (out["starterTasks"] or []) if isinstance(t, dict) and t.get("title")
    ]
    # Guardrail floor: run through agent_store so the deny-list is always enforced.
    out["guardrails"] = agent_store._guardrails(json.dumps(out["guardrails"] or {}))
    out["source"] = source
    return out


class TemplateError(ValueError):
    """A template failed validation on import."""


def validate_template(tpl) -> dict:
    """Validate a candidate template dict; raise :class:`TemplateError` if bad.

    Returns the normalized manifest. Enforces: it's an object, it has a name and
    a persona, and (via :func:`_normalize`) the guardrail floor + secret scrub.
    """
    if not isinstance(tpl, dict):
        raise TemplateError("template must be a JSON object")
    if not str(tpl.get("name") or "").strip():
        raise TemplateError("template needs a name")
    if not str(tpl.get("persona") or "").strip():
        raise TemplateError("template needs a persona")
    return _normalize(tpl, source="imported")


# --------------------------------------------------------------------------- #
# Read (built-in + user/imported)
# --------------------------------------------------------------------------- #

def _read_user_template(path) -> dict | None:
    try:
        data = json.loads((path / "template.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    norm = _normalize(data, source=data.get("source") or "imported")
    norm["id"] = path.name  # the folder name is the authoritative id
    return norm


def _user_templates() -> list[dict]:
    d = _templates_dir()
    out: list[dict] = []
    for child in sorted(d.iterdir() if d.exists() else []):
        if child.is_dir() and (child / "template.json").exists():
            t = _read_user_template(child)
            if t:
                out.append(t)
    return out


def list_templates() -> list[dict]:
    """Built-in gallery first, then user/imported templates (by name)."""
    builtins = [_normalize(t, source="builtin") for t in BUILTIN_TEMPLATES]
    users = sorted(_user_templates(), key=lambda t: t["name"].lower())
    return builtins + users


def get_template(template_id: str) -> dict | None:
    if template_id in _BUILTIN_BY_ID:
        return _normalize(_BUILTIN_BY_ID[template_id], source="builtin")
    path = _templates_dir() / template_id
    if path.is_dir() and (path / "template.json").exists():
        return _read_user_template(path)
    return None


# --------------------------------------------------------------------------- #
# Import / delete (user/imported templates live on disk)
# --------------------------------------------------------------------------- #

def _unique_dir_id(base_id: str) -> str:
    d = _templates_dir()
    if not (d / base_id).exists() and base_id not in _BUILTIN_BY_ID:
        return base_id
    i = 2
    while (d / f"{base_id}-{i}").exists() or f"{base_id}-{i}" in _BUILTIN_BY_ID:
        i += 1
    return f"{base_id}-{i}"


def import_template(tpl: dict) -> dict:
    """Validate a template dict and persist it under the user templates dir.

    Raises :class:`TemplateError` on invalid input. Built-in ids never collide —
    an imported id that clashes is disambiguated.
    """
    norm = validate_template(tpl)
    folder_id = _unique_dir_id(_slugify(norm.get("id") or norm["name"]))
    norm["id"] = folder_id
    norm["source"] = "imported"
    norm["installedAt"] = _now()
    dest = config.ensure_dir(_templates_dir() / folder_id)
    (dest / "template.json").write_text(
        json.dumps(norm, indent=2, ensure_ascii=False), encoding="utf-8")
    return norm


def delete_template(template_id: str) -> bool:
    if template_id in _BUILTIN_BY_ID:
        return False  # built-ins are read-only
    path = _templates_dir() / template_id
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


# --------------------------------------------------------------------------- #
# Deploy (template -> real agent via agent_store)
# --------------------------------------------------------------------------- #

def _subst(text: str, ctx: dict) -> str:
    """Replace ``{{key}}`` tokens from ``ctx`` (unknown tokens are left as-is)."""
    if not text:
        return text or ""

    def repl(m):
        key = m.group(1).strip()
        return str(ctx[key]) if key in ctx else m.group(0)

    return re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", repl, text)


def _unique_agent_name(name: str) -> str:
    """Disambiguate so deploying twice never collides (FR-11)."""
    existing = {a["name"].strip().lower() for a in agent_store.list_agents()}
    if name.strip().lower() not in existing:
        return name
    i = 2
    while f"{name} {i}".strip().lower() in existing:
        i += 1
    return f"{name} {i}"


def resolve_variables(tpl: dict, variables: dict | None) -> dict:
    """Build the substitution context: declared defaults, overridden by input."""
    variables = variables or {}
    ctx: dict = {}
    for v in tpl.get("variables", []):
        ctx[v["key"]] = v.get("default", "")
    for k, val in variables.items():
        if val is not None and val != "":
            ctx[k] = val
    return ctx


def deploy_template(template_id: str, variables: dict | None = None) -> dict | None:
    """Instantiate a template into a brand-new agent and return it.

    Substitutes ``{{variables}}`` into the text fields, maps the special
    ``working_dir`` / ``model`` variables onto the agent, enforces the guardrail
    floor, and seeds any starter tasks. Two deploys create two distinct agents.
    """
    tpl = get_template(template_id)
    if not tpl:
        return None
    ctx = resolve_variables(tpl, variables)

    name = _unique_agent_name(_subst(tpl["name"], ctx).strip() or "Untitled agent")
    working_dir = str(ctx.get("working_dir") or "").strip()
    model = ctx.get("model") or tpl.get("model")

    agent = agent_store.create_agent({
        "name": name,
        "description": _subst(tpl["description"], ctx),
        "persona": _subst(tpl["persona"], ctx),
        "role": _subst(tpl["role"], ctx),
        "model": model or None,
        "temperature": tpl.get("temperature", 0.7),
        "topP": tpl.get("topP"),
        "topK": tpl.get("topK"),
        "workingDir": working_dir,
        "guardrails": tpl.get("guardrails") or {},
        "tools": tpl.get("tools") or [],
        "skills": tpl.get("skills") or [],
    })

    # Seed starter tasks (off-by-default duty — they sit on the board, FR-7/OQ3).
    for t in tpl.get("starterTasks", []):
        try:
            task_store.create(agent["id"], {
                "title": _subst(t.get("title", ""), ctx),
                "description": _subst(t.get("description", ""), ctx),
            }, origin="mentor")
        except Exception:  # a board failure must not abort an otherwise-good deploy
            pass

    return agent


# --------------------------------------------------------------------------- #
# Export (existing agent -> portable template, secrets scrubbed)
# --------------------------------------------------------------------------- #

def template_from_agent(agent_id: str, *, name: str | None = None,
                        include_starters: bool = False) -> dict | None:
    """Capture an existing agent as a portable template (FR-13/14).

    Reads the agent's persona/model/sampling/tools/skills/guardrails and emits a
    template manifest. Private content — **memory, sessions, tokens, traces, and
    any secret-looking fields — is excluded**; mcp servers (which may hold
    credentials) are not exported. Optionally seeds starter tasks from the
    agent's current to-do board.
    """
    agent = agent_store.get_agent(agent_id)
    if not agent:
        return None

    tpl_name = (name or agent.get("name") or "Untitled").strip()
    business = (agent.get("role") or "").strip()

    starter_tasks: list[dict] = []
    if include_starters:
        try:
            for t in task_store.list_for(agent_id, status="todo"):
                starter_tasks.append({
                    "title": t.get("title", ""),
                    "description": t.get("description", ""),
                })
        except Exception:
            starter_tasks = []

    tpl = {
        "id": _slugify(tpl_name),
        "name": tpl_name,
        "description": agent.get("description") or "",
        "category": "General",
        "role": agent.get("role") or "",
        "persona": agent.get("persona") or "",
        "model": agent.get("model"),
        "temperature": agent.get("temperature", 0.7),
        "topP": agent.get("topP"),
        "topK": agent.get("topK"),
        "tools": list(agent.get("tools") or []),
        "skills": list(agent.get("skills") or []),
        "guardrails": agent.get("guardrails") or {},
        "variables": [],
        "starterTasks": starter_tasks,
        "version": "1.0.0",
        "author": "exported",
    }
    # Scrub + normalize (drops any secret-looking field, enforces guardrail floor).
    out = _normalize(tpl, source="user")
    return out
