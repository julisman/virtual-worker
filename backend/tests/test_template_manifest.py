"""Tests for the dependency-free portable template manifest boundary."""

import hashlib

import pytest

from storage.template_manifest import (
    CONNECTOR_SECRET_SLOTS,
    LOCAL_DERIVED_KEYS,
    ManifestError,
    SUPPORTED_FEATURES,
    V2,
    canonical_digest,
    derive_requires_features,
    jcs_canonicalize,
    schema_fields,
    validate_and_normalize,
)


def _manifest(**overrides):
    raw = {"name": "Example", "persona": "You help."}
    raw.update(overrides)
    return raw


def test_normalizes_template_store_shape_without_guardrail_floor():
    normalized = validate_and_normalize(
        _manifest(
            description=None,
            category=None,
            role=123,
            model="local-model",
            temperature="0.25",
            tools=[1, None],
            skills=["skill-a"],
            guardrails={"maxIterations": 0, "custom": "as-authored"},
            variables=[
                {"key": "region", "label": "Region", "type": "bad", "required": 1},
                {"key": "", "label": "ignored"},
                "ignored",
            ],
            starterTasks=[
                {"title": " Start ", "description": None},
                {"description": "no title"},
                "ignored",
            ],
            version=None,
        )
    )

    assert normalized["description"] == ""
    assert normalized["category"] == "General"
    assert normalized["role"] == "123"
    assert normalized["temperature"] == 0.25
    assert normalized["tools"] == ["1", "None"]
    assert normalized["guardrails"] == {"maxIterations": 0, "custom": "as-authored"}
    assert normalized["variables"] == [
        {
            "key": "region",
            "label": "Region",
            "type": "text",
            "default": "",
            "required": True,
            "help": "",
        }
    ]
    assert normalized["starterTasks"] == [{"title": "Start", "description": ""}]
    assert normalized["version"] == "1.0.0"
    assert normalized["templateId"] == "example"


def test_schema_version_and_unknown_sections_fail_loudly():
    with pytest.raises(ManifestError, match="newer Virtual Worker build"):
        validate_and_normalize(_manifest(schemaVersion=3))
    with pytest.raises(ManifestError, match="Unknown template section 'customTools'"):
        validate_and_normalize(_manifest(customTools=[]))


def test_v2_requires_features_accepts_supported_and_rejects_unsupported():
    assert validate_and_normalize(
        _manifest(schemaVersion=2, requiresFeatures=["connectors"])
    )["requiresFeatures"] == ["connectors"]
    assert validate_and_normalize(
        _manifest(schemaVersion=2, requiresFeatures=["mcpServers", "wikis"])
    )["requiresFeatures"] == ["mcpServers", "wikis"]
    with pytest.raises(ManifestError, match="forecasting"):
        validate_and_normalize(_manifest(schemaVersion=2, requiresFeatures=["forecasting"]))


def test_v2_defaults_and_feature_list_are_normalized():
    normalized = validate_and_normalize(_manifest(schemaVersion=2, requiresFeatures=[]))
    assert normalized["schemaVersion"] == 2
    assert normalized["requiresFeatures"] == []
    assert normalized["metadata"] == {}
    assert normalized["customTools"] == []
    assert normalized["connectors"] == []


def test_custom_tools_normalize_defaults_and_enforce_identity():
    normalized = validate_and_normalize(
        _manifest(
            schemaVersion=2,
            customTools=[
                {
                    "key": "fetch-tool",
                    "name": "Fetch",
                    "method": "post",
                }
            ],
        )
    )
    assert normalized["customTools"] == [
        {
            "key": "fetch-tool",
            "name": "Fetch",
            "description": "",
            "transport": "http",
            "method": "POST",
            "url": "",
            "headers": {},
            "parameters": {"type": "object", "properties": {}},
            "fixedParams": {},
            "timeout": 20,
            "allowPrivate": False,
        }
    ]
    with pytest.raises(ManifestError, match="key 'same' must be unique"):
        validate_and_normalize(
            _manifest(
                schemaVersion=2,
                customTools=[
                    {"key": "same", "name": "one"},
                    {"key": "same", "name": "two"},
                ],
            )
        )
    with pytest.raises(ManifestError, match="name 'same' must be unique"):
        validate_and_normalize(
            _manifest(
                schemaVersion=2,
                customTools=[
                    {"key": "one", "name": "same"},
                    {"key": "two", "name": "same"},
                ],
            )
        )
    with pytest.raises(ManifestError, match="must be a slug"):
        validate_and_normalize(
            _manifest(schemaVersion=2, customTools=[{"key": "Bad Key", "name": "bad"}])
        )
    with pytest.raises(ManifestError, match="customTools.*list"):
        validate_and_normalize(_manifest(schemaVersion=2, customTools={}))


def test_connectors_normalize_references_and_bind_declared_secrets():
    normalized = validate_and_normalize(
        _manifest(
            schemaVersion=2,
            variables=[
                {"key": "api-token", "type": "secret"},
                {"key": "region", "type": "text"},
            ],
            connectors=[
                {
                    "name": "Warehouse",
                    "preset": "inventory",
                    "secretBindings": {"pat": "api-token"},
                }
            ],
        )
    )
    assert normalized["connectors"] == [
        {
            "name": "Warehouse",
            "preset": "inventory",
            "overrides": {},
            "secretBindings": {"pat": "api-token"},
        }
    ]
    with pytest.raises(ManifestError, match="secret slot.*pat"):
        validate_and_normalize(
            _manifest(schemaVersion=2, connectors=[{"name": "w", "preset": "p", "overrides": {"pat": "x"}}])
        )
    with pytest.raises(ManifestError, match="declared secret"):
        validate_and_normalize(
            _manifest(
                schemaVersion=2,
                variables=[{"key": "region", "type": "text"}],
                connectors=[{"name": "w", "preset": "p", "secretBindings": {"pat": "region"}}],
            )
        )


@pytest.mark.parametrize("field", ["persona", "url", "fixedParams"])
def test_secret_placeholders_are_restricted_to_custom_tool_headers(field):
    tool = {"key": "fetch", "name": "fetch"}
    if field == "url":
        tool["url"] = "https://example.test/{{ token }}"
    elif field == "fixedParams":
        tool["fixedParams"] = {"token": "{{token}}"}
    with pytest.raises(ManifestError, match="token.*field"):
        validate_and_normalize(
            _manifest(
                schemaVersion=2,
                persona="{{token}}" if field == "persona" else "P",
                variables=[{"key": "token", "type": "secret"}],
                customTools=[tool],
            )
        )


def test_secret_header_placeholder_and_non_secret_placeholder_are_allowed():
    normalized = validate_and_normalize(
        _manifest(
            schemaVersion=2,
            persona="Use {{region}}.",
            variables=[
                {"key": "token", "type": "secret"},
                {"key": "region", "type": "text"},
            ],
            customTools=[
                {
                    "key": "fetch",
                    "name": "fetch",
                    "headers": {"Authorization": "Bearer {{ token }}"},
                }
            ],
        )
    )
    assert normalized["customTools"][0]["headers"]["Authorization"] == "Bearer {{ token }}"


def test_v2_accepts_m4_sections_and_still_refuses_unknown():
    for field in ("mcpServers", "wikis", "knowledgeBases"):
        assert validate_and_normalize(
            _manifest(schemaVersion=2, **{field: []})
        )[field] == []
    with pytest.raises(ManifestError, match="forecasts"):
        validate_and_normalize(_manifest(schemaVersion=2, forecasts=[]))


def test_schema_surface_and_feature_derivation_cover_m4_sections():
    assert SUPPORTED_FEATURES == {
        "connectors", "customTools", "wikis", "knowledgeBases", "mcpServers"
    }
    assert CONNECTOR_SECRET_SLOTS == (
        "pat", "patNonlive", "passwordTest", "passwordUat", "passwordStaging"
    )
    fields = schema_fields()
    assert set(fields["v2TopLevel"]) == V2
    assert "parameters" in fields["customTool"]
    assert fields["customTool"]["parameters"]["default"] == {"type": "object", "properties": {}}
    assert set(fields["wiki"]) == {"name", "repoUrl", "wikiRoot", "pathVariable"}
    assert set(fields["knowledgeBase"]) == {
        "name", "description", "embeddingModel", "sourceFolderVariable"
    }
    assert set(fields["mcpServer"]) == {"name", "transport", "hint", "mcpToolAllow"}
    assert derive_requires_features(
        {"customTools": [{"name": "x"}], "connectors": {"one": True}, "wikis": "wiki"}
    ) == ["connectors", "customTools", "wikis"]


def test_legacy_metadata_is_accepted_but_not_deployed_or_hashed():
    raw = _manifest(author="A", source="import", installedAt="today")
    normalized = validate_and_normalize(raw)
    assert not (set(normalized) & {"author", "source", "installedAt"})
    assert canonical_digest(normalized) == canonical_digest(
        {**normalized, "source": "local", "installedAt": "later"}
    )


def test_required_name_and_persona_are_non_empty_strings():
    with pytest.raises(ManifestError, match="template needs a name"):
        validate_and_normalize({"name": "", "persona": "P"})
    with pytest.raises(ManifestError, match="template needs a name"):
        validate_and_normalize({"name": 1, "persona": "P"})
    with pytest.raises(ManifestError, match="template needs a persona"):
        validate_and_normalize({"name": "N", "persona": "  "})
    with pytest.raises(ManifestError, match="template needs a persona"):
        validate_and_normalize({"name": "N", "persona": None})


def test_non_object_and_invalid_requires_features_fail_with_manifest_error():
    with pytest.raises(ManifestError, match="template must be a JSON object"):
        validate_and_normalize([])
    with pytest.raises(ManifestError, match="requiresFeatures must be a list"):
        validate_and_normalize(_manifest(schemaVersion=2, requiresFeatures="customTools"))
    with pytest.raises(ManifestError, match="requiresFeatures must be a list"):
        validate_and_normalize(_manifest(schemaVersion=2, requiresFeatures=["customTools", 1]))


def test_jcs_handles_numbers_and_integral_float_equivalence():
    assert jcs_canonicalize({"i": 2, "f": 1.25, "negative": -3.5, "zero": -0.0}) == (
        '{"f":1.25,"i":2,"negative":-3.5,"zero":0}'
    )
    assert canonical_digest(validate_and_normalize(_manifest(temperature=1.0))) == canonical_digest(
        validate_and_normalize(_manifest(temperature=1))
    )


def test_jcs_preserves_unicode_and_sorts_keys_by_utf16_code_units():
    value = {"😀": "雪", "\ue000": "private", "a": "é"}
    assert jcs_canonicalize(value) == '{"a":"é","😀":"雪","":"private"}'


def test_jcs_rejects_unexpected_values():
    with pytest.raises(TypeError):
        jcs_canonicalize({"bad": object()})
    with pytest.raises(TypeError):
        jcs_canonicalize(float("nan"))


def test_omitted_and_explicit_defaults_have_same_digest():
    omitted = validate_and_normalize(_manifest())
    explicit = validate_and_normalize(
        _manifest(
            description="",
            category="General",
            role="",
            model=None,
            temperature=0.7,
            topP=None,
            topK=None,
            tools=[],
            skills=[],
            guardrails={},
            variables=[],
            starterTasks=[],
            version="1.0.0",
        )
    )
    assert omitted == explicit
    assert canonical_digest(omitted) == canonical_digest(explicit)


def test_array_order_changes_digest():
    one = validate_and_normalize(_manifest(tools=["a", "b"]))
    two = validate_and_normalize(_manifest(tools=["b", "a"]))
    assert canonical_digest(one) != canonical_digest(two)


def test_v2_sections_are_included_in_deterministic_digest():
    first = _manifest(
        schemaVersion=2,
        variables=[{"key": "token", "type": "secret"}],
        customTools=[
            {
                "name": "fetch",
                "key": "fetch",
                "headers": {"Authorization": "Bearer {{token}}"},
                "parameters": {"properties": {}, "type": "object"},
            }
        ],
        connectors=[
            {
                "secretBindings": {"pat": "token"},
                "preset": "inventory",
                "name": "warehouse",
            }
        ],
    )
    second = _manifest(
        connectors=[
            {
                "name": "warehouse",
                "preset": "inventory",
                "secretBindings": {"pat": "token"},
            }
        ],
        customTools=[
            {
                "parameters": {"type": "object", "properties": {}},
                "key": "fetch",
                "headers": {"Authorization": "Bearer {{token}}"},
                "name": "fetch",
            }
        ],
        variables=[{"type": "secret", "key": "token"}],
        schemaVersion=2,
    )
    assert canonical_digest(validate_and_normalize(first)) == canonical_digest(
        validate_and_normalize(second)
    )


def test_frozen_digest_vectors():
    assert canonical_digest(validate_and_normalize(_manifest(name="Alpha", persona="P", temperature=1))) == (
        "942b9a24ffb83f2196820ab2c99cdc861e32d18b5da5be4a881e75d954710639"
    )
    assert canonical_digest(
        validate_and_normalize(
            _manifest(
                name="Unicode ☃",
                persona="你好",
                tools=["a", "b"],
                temperature=-0.0,
                topP=0.0,
            )
        )
    ) == "bb5c41e8e0bb5834d1dac9404f97789d1e742d6ef800b7b4739f27b379575056"


def test_digest_drops_all_local_derived_keys():
    manifest = {"name": "N", "source": "s", "nested": {"source": "kept"}}
    without_derived = {"name": "N", "nested": {"source": "kept"}}
    assert canonical_digest(manifest) == canonical_digest(without_derived)
    assert LOCAL_DERIVED_KEYS == {"source", "installedAt", "commit", "digest", "sourceId"}


def test_derive_requires_features_covers_m4_sections():
    assert derive_requires_features(
        {
            "customTools": [{"name": "x"}],
            "connectors": {"one": True},
            "mcpServers": [],
            "wikis": "wiki",
            "knowledgeBases": None,
        }
    ) == ["connectors", "customTools", "wikis"]
    assert derive_requires_features({"customTools": [], "wikis": {}}) == []


def test_manifest_module_has_no_repo_dependency_and_hashlib_vector_is_stable():
    normalized = validate_and_normalize(_manifest(name="Hash", persona="P"))
    canonical = jcs_canonicalize(normalized)
    assert canonical_digest(normalized) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
