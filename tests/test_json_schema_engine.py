import pytest
from maia import json_schema_lite as j


def test_engine_is_jsonschema():
    assert j.jsonschema_available() is True
    assert j.validator_name().startswith("jsonschema==")
    assert j.JSONSCHEMA_AVAILABLE is True


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "score": {"type": "integer", "minimum": 0, "maximum": 5},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "score"],
    "additionalProperties": False,
}

CASES = [
    ({"title": "ok", "score": 3}, []),
    ({"title": "", "score": 3}, ["$.title: shorter than minLength=1"]),
    ({"title": "ok", "score": 9}, ["$.score: above maximum=5"]),
    ({"title": "ok", "score": -1}, ["$.score: below minimum=0"]),
    ({"title": "ok"}, ["$: missing required property 'score'"]),
    ({"title": "ok", "score": 1, "extra": 1}, ["$: unexpected property 'extra'"]),
    ({"title": "ok", "score": "three"}, ["$.score: expected integer, got str"]),
    ({"title": 5, "score": 3}, ["$.title: expected string, got integer"]),
    ({"title": "ok", "score": 3, "tags": ["a", 2]}, ["$.tags[1]: expected string, got integer"]),
    ({"title": "ok", "score": True}, ["$.score: expected integer, got boolean"]),
]


@pytest.mark.parametrize("payload,expected", CASES)
def test_jsonschema_messages_match_builtin(payload, expected):
    """The stable error strings must be identical on both engines.

    These strings feed the LLM retry prompt and operator dashboards, so a swap of
    validation engine must not silently change what operators see.
    """
    got = j.validate(payload, SCHEMA)
    assert got == expected, f"jsonschema engine: {got}"
    builtin = j.validate_builtin(payload, SCHEMA)
    assert builtin == expected, f"builtin fallback: {builtin}"


def test_builtin_is_exercised_as_fallback_when_jsonschema_absent(monkeypatch):
    monkeypatch.setattr(j, "JSONSCHEMA_AVAILABLE", False)
    assert j.validator_name() == "builtin-subset"
    assert j.validate({"title": "ok", "score": 9}, SCHEMA) == [
        "$.score: above maximum=5"
    ]


def test_format_checker_rejects_bad_datetime():
    schema = {"type": "object", "properties": {"at": {"type": "string", "format": "date-time"}}}
    assert j.validate({"at": "2026-09-28T10:00:00Z"}, schema) == []
    errors = j.validate({"at": "not-a-date"}, schema)
    assert errors == ["$.at: invalid date-time value"]


def test_builtin_fallback_ignores_format_keyword():
    """Documented gap: the fallback does not implement `format`."""
    schema = {"type": "object", "properties": {"at": {"type": "string", "format": "date-time"}}}
    assert j.validate_builtin({"at": "not-a-date"}, schema) == []


def test_ref_and_defs_are_resolved():
    schema = {
        "type": "object",
        "properties": {"user": {"$ref": "#/$defs/user"}},
        "required": ["user"],
        "$defs": {"user": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
    }
    assert j.validate({"user": {"id": 1}}, schema) == []
    errors = j.validate({"user": {"id": "x"}}, schema)
    assert errors == ["$.user.id: expected integer, got str"]


def test_enum_and_pattern_messages():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "code": {"type": "string", "pattern": "^[A-Z]{2}$"},
        },
    }
    assert j.validate({"mode": "c"}, schema) == ["$.mode: 'c' not in enum ['a', 'b']"]
    assert j.validate({"code": "abc"}, schema) == [
        "$.code: does not match pattern '^[A-Z]{2}$'"
    ]


def test_validate_passes_through_empty_schema():
    assert j.validate({"anything": True}, None) == []
    assert j.validate({"anything": True}, {}) == []


def test_validation_error_wraps_describe_errors():
    err = j.ValidationError(["$.a: below minimum=0", "$.b: expected string, got integer"])
    assert "$.a: below minimum=0" in str(err)
    assert err.errors == [
        "$.a: below minimum=0",
        "$.b: expected string, got integer",
    ]
    assert j.describe_errors([]) == "ok"
