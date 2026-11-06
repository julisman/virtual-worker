"""Pure-Python validation and canonicalization for portable template manifests."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SUPPORTED_SCHEMA_VERSION = 2


class ManifestError(ValueError):
    pass


LOCAL_DERIVED_KEYS = {"source", "installedAt", "commit", "digest", "sourceId"}
LEGACY_METADATA_KEYS = {"author", "source", "installedAt"}
SUPPORTED_FEATURES = {"customTools", "connectors", "wikis", "knowledgeBases", "mcpServers"}
RECOGNIZED_FEATURES = {"customTools", "connectors", "wikis", "knowledgeBases", "mcpServers"}
CONNECTOR_SECRET_SLOTS = (
    "pat",
    "patNonlive",
    "passwordTest",
    "passwordUat",
    "passwordStaging",
)

V1 = {
    "id",
    "templateId",
    "name",
    "description",
    "category",
    "role",
    "persona",
    "model",
    "temperature",
    "topP",
    "topK",
    "tools",
    "skills",
    "guardrails",
    "variables",
    "starterTasks",
    "version",
    "schemaVersion",
} | LEGACY_METADATA_KEYS | LOCAL_DERIVED_KEYS
V2 = V1 | {"requiresFeatures", "metadata", "customTools", "connectors",
           "wikis", "knowledgeBases", "mcpServers",
           # Per-agent behavior configs a template may seed (PRD-22 case
           # management, PRD-31 efficiency metrics). Optional objects — only
           # present when non-empty, so pre-existing manifests keep their digests.
           "cases", "metrics"}

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
_VARIABLE_TYPES = ("text", "folder", "model", "skill", "secret", "enum")
_SECRET_TOKEN_RE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
# A template id is a slug: it names the on-disk folder and (in a registry) the
# templates/<id>/ path, and it is one half of the lineage identity — so an
# explicitly authored id must already be in canonical form; we refuse rather
# than silently rewrite what a manifest claims as its identity.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def schema_fields() -> dict[str, Any]:
    """Return the pure schema surface used by documentation generators."""
    return {
        "v1TopLevel": sorted(V1),
        "v2TopLevel": sorted(V2),
        "variable": {
            "key": {"type": "string", "required": True},
            "label": {"type": "string", "default": "<key>"},
            "type": {"type": "string", "default": "text", "enum": list(_VARIABLE_TYPES)},
            "default": {"default": ""},
            "required": {"type": "boolean", "default": False},
            "help": {"type": "string", "default": ""},
        },
        "customTool": {
            "key": {"type": "string", "required": True, "pattern": SLUG_RE.pattern},
            "name": {"type": "string", "required": True},
            "description": {"type": "string", "default": ""},
            "transport": {"type": "string", "default": "http"},
            "method": {"type": "string", "default": "GET"},
            "url": {"type": "string", "default": ""},
            "headers": {"type": "object", "default": {}},
            "parameters": {"type": "object", "default": {"type": "object", "properties": {}}},
            "fixedParams": {"type": "object", "default": {}},
            "timeout": {"type": "integer", "default": 20},
            "allowPrivate": {"type": "boolean", "default": False},
        },
        "connector": {
            "name": {"type": "string", "required": True},
            "preset": {"type": "string", "required": True},
            "overrides": {"type": "object", "default": {}},
            "secretBindings": {"type": "object", "default": {}},
        },
        "wiki": {
            "name": {"type": "string", "required": True},
            "repoUrl": {"type": "string", "required": True},
            "wikiRoot": {"type": "string", "default": "."},
            "pathVariable": {"type": "string", "optional": True},
        },
        "knowledgeBase": {
            "name": {"type": "string", "required": True},
            "description": {"type": "string", "default": ""},
            "embeddingModel": {"type": "string", "default": ""},
            "sourceFolderVariable": {"type": "string", "optional": True},
        },
        "mcpServer": {
            "name": {"type": "string", "required": True},
            "transport": {"type": "string", "default": ""},
            "hint": {"type": "string", "default": ""},
            "mcpToolAllow": {"type": "array", "items": {"type": "string"}, "optional": True},
        },
    }


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "template"


def _list_field(raw: Any, field: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"template field '{field}' must be a list")
    return raw


def _normalize_variables(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value in _list_field(raw, "variables"):
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "").strip()
        if not key:
            continue
        out.append(
            {
                "key": key,
                "label": str(value.get("label") or key),
                "type": value.get("type") if value.get("type") in _VARIABLE_TYPES else "text",
                "default": value.get("default", ""),
                "required": bool(value.get("required", False)),
                "help": str(value.get("help") or ""),
            }
        )
    return out


def _normalized_text(raw: dict[str, Any], field: str) -> str:
    default = _DEFAULTS[field]
    return str(raw.get(field) or default)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"manifest field '{field}' must be a non-empty string")
    return value.strip()


def _object_field(value: Any, field: str, default: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return dict(default)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ManifestError(f"manifest field '{field}' must be an object with string keys")
    return value


def _normalize_custom_tools(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    names: set[str] = set()
    for index, value in enumerate(_list_field(raw, "customTools")):
        if not isinstance(value, dict):
            raise ManifestError(f"customTools[{index}] must be an object")
        key = _required_string(value.get("key"), f"customTools[{index}].key")
        if not SLUG_RE.fullmatch(key):
            raise ManifestError(
                f"customTools[{index}].key {key!r} must be a slug — lowercase letters, "
                "digits, and dashes"
            )
        if key in keys:
            raise ManifestError(f"customTools key '{key}' must be unique")
        keys.add(key)
        name = _required_string(value.get("name"), f"customTools[{index}].name")
        if name in names:
            raise ManifestError(f"customTools name '{name}' must be unique")
        names.add(name)

        def optional_string(field: str, default: str) -> str:
            field_value = value.get(field)
            if field_value is None:
                return default
            if not isinstance(field_value, str):
                raise ManifestError(f"manifest field 'customTools[{index}].{field}' must be a string")
            return field_value

        method = optional_string("method", "GET").upper()
        headers = _object_field(value.get("headers"), f"customTools[{index}].headers", {})
        if not all(isinstance(header, str) and isinstance(header_value, str)
                   for header, header_value in headers.items()):
            raise ManifestError(
                f"manifest field 'customTools[{index}].headers' must be an object of strings"
            )
        parameters = _object_field(
            value.get("parameters"),
            f"customTools[{index}].parameters",
            {"type": "object", "properties": {}},
        )
        fixed_params = _object_field(
            value.get("fixedParams"), f"customTools[{index}].fixedParams", {}
        )
        if not all(
            isinstance(fixed_value, (str, int, float, bool)) or fixed_value is None
            for fixed_value in fixed_params.values()
        ):
            raise ManifestError(
                f"manifest field 'customTools[{index}].fixedParams' must be a flat object of scalars"
            )
        timeout = value.get("timeout", 20)
        if timeout is None:
            timeout = 20
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ManifestError(f"manifest field 'customTools[{index}].timeout' must be an integer")
        allow_private = value.get("allowPrivate", False)
        if allow_private is None:
            allow_private = False
        if not isinstance(allow_private, bool):
            raise ManifestError(
                f"manifest field 'customTools[{index}].allowPrivate' must be a boolean"
            )
        out.append(
            {
                "key": key,
                "name": name,
                "description": optional_string("description", ""),
                "transport": optional_string("transport", "http"),
                "method": method,
                "url": optional_string("url", ""),
                "headers": headers,
                "parameters": parameters,
                "fixedParams": fixed_params,
                "timeout": timeout,
                "allowPrivate": allow_private,
            }
        )
    return out


def _normalize_connectors(raw: Any, secret_keys: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, value in enumerate(_list_field(raw, "connectors")):
        if not isinstance(value, dict):
            raise ManifestError(f"connectors[{index}] must be an object")
        name = _required_string(value.get("name"), f"connectors[{index}].name")
        if name in names:
            raise ManifestError(f"connector name '{name}' must be unique")
        names.add(name)
        preset = _required_string(value.get("preset"), f"connectors[{index}].preset")
        overrides = _object_field(value.get("overrides"), f"connectors[{index}].overrides", {})
        forbidden = sorted(set(overrides) & set(CONNECTOR_SECRET_SLOTS))
        if forbidden:
            raise ManifestError(
                f"connectors[{index}].overrides cannot contain secret slot(s): "
                + ", ".join(forbidden)
            )
        bindings = _object_field(
            value.get("secretBindings"), f"connectors[{index}].secretBindings", {}
        )
        for slot, variable_key in bindings.items():
            if not isinstance(slot, str) or not isinstance(variable_key, str):
                raise ManifestError(
                    f"connectors[{index}].secretBindings keys and values must be strings"
                )
            if variable_key not in secret_keys:
                raise ManifestError(
                    f"connectors[{index}].secretBindings['{slot}'] must reference "
                    "a declared secret variable"
                )
        out.append(
            {
                "name": name,
                "preset": preset,
                "overrides": overrides,
                "secretBindings": bindings,
            }
        )
    return out


def _section_optional_string(value: Any, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ManifestError(f"manifest field '{field}' must be a string")
    return value.strip()


def _normalize_wikis(raw: Any, declared_variables: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    allowed = {"name", "repoUrl", "wikiRoot", "pathVariable"}
    for index, value in enumerate(_list_field(raw, "wikis")):
        if not isinstance(value, dict):
            raise ManifestError(f"wikis[{index}] must be an object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ManifestError(f"Unknown wikis[{index}] field(s): {', '.join(unknown)}")
        name = _required_string(value.get("name"), f"wikis[{index}].name")
        if name in names:
            raise ManifestError(f"wiki name '{name}' must be unique")
        names.add(name)
        repo_url = _required_string(value.get("repoUrl"), f"wikis[{index}].repoUrl")
        wiki_root = _section_optional_string(value.get("wikiRoot"), f"wikis[{index}].wikiRoot", ".") or "."
        path_variable = value.get("pathVariable")
        if path_variable is not None:
            path_variable = _required_string(path_variable, f"wikis[{index}].pathVariable")
            if path_variable not in declared_variables:
                raise ManifestError(f"wikis[{index}].pathVariable must reference a declared variable")
        item = {"name": name, "repoUrl": repo_url, "wikiRoot": wiki_root}
        if path_variable is not None:
            item["pathVariable"] = path_variable
        out.append(item)
    return out


def _normalize_knowledge_bases(raw: Any, declared_variables: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    allowed = {"name", "description", "embeddingModel", "sourceFolderVariable"}
    for index, value in enumerate(_list_field(raw, "knowledgeBases")):
        if not isinstance(value, dict):
            raise ManifestError(f"knowledgeBases[{index}] must be an object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ManifestError(f"Unknown knowledgeBases[{index}] field(s): {', '.join(unknown)}")
        name = _required_string(value.get("name"), f"knowledgeBases[{index}].name")
        if name in names:
            raise ManifestError(f"knowledge base name '{name}' must be unique")
        names.add(name)
        source_variable = value.get("sourceFolderVariable")
        if source_variable is not None:
            source_variable = _required_string(source_variable, f"knowledgeBases[{index}].sourceFolderVariable")
            if source_variable not in declared_variables:
                raise ManifestError(
                    f"knowledgeBases[{index}].sourceFolderVariable must reference a declared variable"
                )
        item = {
            "name": name,
            "description": _section_optional_string(value.get("description"), f"knowledgeBases[{index}].description"),
            "embeddingModel": _section_optional_string(value.get("embeddingModel"), f"knowledgeBases[{index}].embeddingModel"),
        }
        if source_variable is not None:
            item["sourceFolderVariable"] = source_variable
        out.append(item)
    return out


def _normalize_mcp_servers(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    allowed = {"name", "transport", "hint", "mcpToolAllow"}
    for index, value in enumerate(_list_field(raw, "mcpServers")):
        if not isinstance(value, dict):
            raise ManifestError(f"mcpServers[{index}] must be an object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ManifestError(
                f"mcpServers[{index}] contains forbidden secret or machine-specific field(s): "
                f"{', '.join(unknown)}; secrets and machine-specifics must be configured manually"
            )
        name = _required_string(value.get("name"), f"mcpServers[{index}].name")
        if name in names:
            raise ManifestError(f"MCP server name '{name}' must be unique")
        names.add(name)
        allow = value.get("mcpToolAllow")
        if allow is not None:
            allow = _list_field(allow, f"mcpServers[{index}].mcpToolAllow")
            if not all(isinstance(tool, str) for tool in allow):
                raise ManifestError(f"manifest field 'mcpServers[{index}].mcpToolAllow' must be a list of strings")
            allow = [tool.strip() for tool in allow if tool.strip()]
        item = {
            "name": name,
            "transport": _section_optional_string(value.get("transport"), f"mcpServers[{index}].transport"),
            "hint": _section_optional_string(value.get("hint"), f"mcpServers[{index}].hint"),
        }
        if allow is not None:
            item["mcpToolAllow"] = allow
        out.append(item)
    return out


def _validate_secret_placeholders(manifest: dict[str, Any], secret_keys: set[str]) -> None:
    """Reject secret-variable tokens except in custom-tool header values."""
    def walk(value: Any, field: str, allow_secret: bool = False) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                nested_field = f"{field}.{key}" if field else str(key)
                if isinstance(key, str):
                    for token in _SECRET_TOKEN_RE.findall(key):
                        if token in secret_keys:
                            raise ManifestError(
                                f"secret variable placeholder '{token}' is not allowed in field '{field}'"
                            )
                nested_allow = allow_secret or bool(
                    re.fullmatch(r"customTools\[\d+\]", field) and key == "headers"
                )
                walk(nested, nested_field, nested_allow)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{field}[{index}]", allow_secret)
        elif isinstance(value, str):
            for token in _SECRET_TOKEN_RE.findall(value):
                if token in secret_keys and not allow_secret:
                    raise ManifestError(
                        f"secret variable placeholder '{token}' is not allowed in field '{field}'"
                    )

    walk(manifest, "")


def validate_and_normalize(raw: dict) -> dict:
    """Validate a portable manifest and return its canonical structural form."""
    if not isinstance(raw, dict):
        raise ManifestError("template must be a JSON object")

    try:
        schema_version = int(raw.get("schemaVersion", 1) or 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ManifestError("template schemaVersion must be an integer") from exc
    if schema_version < 1:
        schema_version = 1
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise ManifestError(
            f"This template needs a newer Virtual Worker build (schemaVersion {schema_version}; "
            f"this build supports up to {SUPPORTED_SCHEMA_VERSION}). Update Virtual Worker."
        )

    accepted = V2 if schema_version >= 2 else V1
    for key in raw:
        if key not in accepted:
            raise ManifestError(
                f"Unknown template section '{key}' — refusing to import "
                "(a newer Virtual Worker build may support it)."
            )

    name = raw.get("name")
    persona = raw.get("persona")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("template needs a name")
    if not isinstance(persona, str) or not persona.strip():
        raise ManifestError("template needs a persona")

    requires_features: list[str] = []
    if schema_version >= 2:
        try:
            requires_raw = raw.get("requiresFeatures") or []
        except (AttributeError, TypeError) as exc:
            raise ManifestError("template requiresFeatures must be a list of strings") from exc
        if not isinstance(requires_raw, list) or not all(
            isinstance(feature, str) for feature in requires_raw
        ):
            raise ManifestError("template requiresFeatures must be a list of strings")
        requires_features = sorted(set(requires_raw))
        unsupported = sorted(set(requires_features) - SUPPORTED_FEATURES)
        if unsupported:
            raise ManifestError(
                "This template requires features this Virtual Worker build does not support: "
                + ", ".join(unsupported)
                + "."
            )

    out: dict[str, Any] = {**_DEFAULTS}
    for field in _DEFAULTS:
        if field in raw and raw[field] is not None:
            out[field] = raw[field]

    out["name"] = name.strip()
    out["description"] = _normalized_text(raw, "description")
    out["category"] = _normalized_text(raw, "category")
    out["role"] = _normalized_text(raw, "role")
    out["persona"] = persona

    temperature = out["temperature"]
    try:
        out["temperature"] = 0.7 if temperature is None else float(temperature)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ManifestError("template field 'temperature' must be a number") from exc

    out["tools"] = [str(value) for value in _list_field(out["tools"], "tools")]
    out["skills"] = [str(value) for value in _list_field(out["skills"], "skills")]
    out["guardrails"] = out["guardrails"] if isinstance(out["guardrails"], dict) else {}
    out["variables"] = _normalize_variables(out["variables"])
    out["starterTasks"] = [
        {
            "title": str(task.get("title") or "").strip(),
            "description": str(task.get("description") or "").strip(),
        }
        for task in _list_field(out["starterTasks"], "starterTasks")
        if isinstance(task, dict) and task.get("title")
    ]
    out["version"] = str(out["version"] or "1.0.0")
    explicit_id = raw.get("templateId") or raw.get("id")
    if explicit_id is not None and str(explicit_id).strip():
        explicit_id = str(explicit_id).strip()
        if not SLUG_RE.fullmatch(explicit_id):
            raise ManifestError(
                f"template id {explicit_id!r} must be a slug — lowercase letters, "
                "digits, and dashes (e.g. 'support-on-duty')")
        out["templateId"] = explicit_id
    else:
        out["templateId"] = _slugify(name)
    out["schemaVersion"] = schema_version
    if schema_version >= 2:
        out["metadata"] = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        out["requiresFeatures"] = requires_features
        secret_keys = {
            variable["key"]
            for variable in out["variables"]
            if variable["type"] == "secret"
        }
        declared_variables = {variable["key"] for variable in out["variables"]}
        out["customTools"] = _normalize_custom_tools(raw.get("customTools"))
        out["connectors"] = _normalize_connectors(raw.get("connectors"), secret_keys)
        out["wikis"] = _normalize_wikis(raw.get("wikis"), declared_variables)
        out["knowledgeBases"] = _normalize_knowledge_bases(raw.get("knowledgeBases"), declared_variables)
        out["mcpServers"] = _normalize_mcp_servers(raw.get("mcpServers"))
        out["requiresFeatures"] = sorted(
            set(requires_features) | set(derive_requires_features(out))
        )
        # Optional behavior configs (PRD-22 cases, PRD-31 metrics): present in
        # the canonical form only when non-empty, so manifests that predate
        # them keep their digests byte-for-byte.
        for field in ("cases", "metrics"):
            value = raw.get(field)
            if value is not None and not isinstance(value, dict):
                raise ManifestError(f"template field '{field}' must be an object")
            if value:
                out[field] = _object_field(value, field, {})
    _validate_secret_placeholders(
        out,
        {
            variable["key"]
            for variable in out["variables"]
            if variable["type"] == "secret"
        },
    )
    return out


def jcs_canonicalize(obj: Any) -> str:
    """Canonicalize manifest-domain JSON values using the relevant JCS rules.

    Manifest numbers are bounded; full ECMAScript exponent formatting is
    intentionally out of scope, so non-integral floats use Python's shortest
    round-trip ``repr``.
    """
    if isinstance(obj, dict):
        if not all(isinstance(key, str) for key in obj):
            raise TypeError("JCS object keys must be strings")
        items: list[str] = []
        for key in sorted(obj, key=lambda value: value.encode("utf-16-be")):
            items.append(
                f"{json.dumps(key, ensure_ascii=False)}:{jcs_canonicalize(obj[key])}"
            )
        return "{" + ",".join(items) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(jcs_canonicalize(value) for value in obj) + "]"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if obj is None:
        return "null"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        if repr(obj) in {"nan", "inf", "-inf"}:
            raise TypeError("JCS does not support non-finite numbers")
        if obj.is_integer() and abs(obj) < 1e21:
            return str(int(obj))
        return repr(obj)
    raise TypeError(f"JCS cannot canonicalize {type(obj).__name__}")


def canonical_digest(manifest: dict) -> str:
    """Return the SHA-256 digest of a manifest without local-derived fields."""
    content = {key: value for key, value in manifest.items() if key not in LOCAL_DERIVED_KEYS}
    canonical = jcs_canonicalize(content)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_requires_features(manifest: dict) -> list[str]:
    """Derive gated feature names from non-empty manifest sections."""
    return sorted(
        {
            feature
            for feature in RECOGNIZED_FEATURES
            if feature in manifest and manifest[feature]
        }
    )
