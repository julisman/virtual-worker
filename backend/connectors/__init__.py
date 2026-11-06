"""Portal connectors are not part of this build.

This stub keeps the template-manifest and skill code paths that *can* reference
connectors importable and inert: no presets, no records, nothing to resolve.
A future connector plugin can replace it by implementing the same names.
"""

PRESETS: dict[str, dict] = {}


def preset_base_url(preset: str) -> str:
    return ""


def connector_identity(record: dict) -> dict:
    return {"name": (record or {}).get("name", ""), "preset": (record or {}).get("preset", "")}
