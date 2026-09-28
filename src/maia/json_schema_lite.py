"""JSON-Schema validation for prompt output contracts and MCP tool arguments.

Two consumers sit on this trust boundary:

* **PromptOps** — an LLM answer is only usable if it validates against the
  prompt's declared ``output_schema``.
* **MCP** — ``tools/call`` arguments are validated *before* the handler runs, so a
  hallucinated argument name never reaches a side-effecting integration.

Implementation choice (2026-09-28): validation is delegated to the
**`jsonschema` library** (Draft 2020-12) whenever it is installed, so the whole
spec works — ``anyOf``/``oneOf``, ``$ref``/``$defs``, ``format`` (with a real
format checker), ``dependentRequired``, ``uniqueItems``, ``multipleOf`` — instead of
the hand-written subset that used to live here. Two properties are kept on purpose:

1. **Stable error strings.** ``jsonschema``'s own messages are rewritten into the
   format this module has always produced (``"$.rating: expected integer, got
   str"``, ``"missing required property 'score'"``). Those strings go to operators
   *and* into the LLM retry prompt, so they are a contract, not cosmetics.
2. **A built-in fallback.** If ``jsonschema`` is absent, the previous subset
   validator is used so a minimal install keeps working. It is a *fallback*, not
   the primary engine, and ``validator_name()`` reports which one is live.

``validate_builtin()`` stays public so the two paths can be compared
test-for-test, which is what keeps the fallback honest.
"""
from __future__ import annotations

import math
import re
from typing import Any

try:  # pragma: no cover - import branch
    import jsonschema
    from jsonschema import FormatChecker
    from jsonschema.validators import Draft202012Validator, validator_for

    JSONSCHEMA_AVAILABLE = True
except Exception:  # pragma: no cover - minimal install without the library
    jsonschema = None  # type: ignore[assignment]
    FormatChecker = None  # type: ignore[assignment]
    Draft202012Validator = None  # type: ignore[assignment,misc]
    validator_for = None  # type: ignore[assignment]
    JSONSCHEMA_AVAILABLE = False

__all__ = [
    "JSONSCHEMA_AVAILABLE",
    "ValidationError",
    "describe_errors",
    "jsonschema_available",
    "validate",
    "validate_builtin",
    "validator_name",
]

_FORMAT_CHECKER = FormatChecker() if FormatChecker is not None else None

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
}


def _matches_type(value: Any, expected: str) -> bool:
    """Type check with the two JSON/Python traps handled explicitly.

    ``bool`` is a subclass of ``int`` in Python, so ``True`` would satisfy
    ``type: integer`` unless excluded. JSON does not make that equivalence, and a
    prompt schema like ``{"qty": {"type": "integer"}}`` must reject ``true``.
    """
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float)) and math.isfinite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    checks = _TYPE_CHECKS.get(expected)
    if checks is None:  # unknown type keyword -> do not block the caller
        return True
    return isinstance(value, checks)


def _type_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def jsonschema_available() -> bool:
    """True when the real ``jsonschema`` engine is in use."""
    return JSONSCHEMA_AVAILABLE


def validator_name() -> str:
    """Which engine :func:`validate` is using (for logs and diagnostics)."""
    if not JSONSCHEMA_AVAILABLE:
        return "builtin-subset"
    from importlib.metadata import version

    try:
        return f"jsonschema=={version('jsonschema')}"
    except Exception:  # pragma: no cover - odd installs without metadata
        return "jsonschema (version unknown)"


def describe_errors(errors: list[str]) -> str:
    """Single-line summary used in tool results / eval reports."""
    if not errors:
        return "ok"
    return "; ".join(errors)


class ValidationError(ValueError):
    """Raised by the :func:`ValidationError`-style helpers when a payload is invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(describe_errors(errors))


def _path_str(path: Any) -> str:
    """Render a jsonschema ``absolute_path`` deque as ``$.a.b[0]``."""
    out = "$"
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _format_message(error: Any) -> str:
    """Translate one ``jsonschema`` error into this module's stable wording."""
    path = _path_str(error.absolute_path)
    keyword = error.validator
    value = error.validator_value
    instance = error.instance

    if keyword == "type":
        allowed = value if isinstance(value, list) else [value]
        names = "|".join(str(t) for t in allowed)
        return f"{path}: expected {names}, got {_type_label(instance)}"
    if keyword == "required":
        present = instance if isinstance(instance, dict) else {}
        missing = [f for f in (value or []) if f not in present]
        target = missing[0] if missing else (value[0] if value else "?")
        return f"{path}: missing required property {target!r}"
    if keyword == "additionalProperties":
        known = set(error.schema.get("properties") or {})
        extras = sorted(set(instance or {}) - known)
        if extras:
            return f"{path}: unexpected property {extras[0]!r}"
        return f"{path}: unexpected property"
    if keyword == "enum":
        return f"{path}: {instance!r} not in enum {value!r}"
    if keyword == "const":
        return f"{path}: expected const {value!r}, got {instance!r}"
    if keyword == "minLength":
        return f"{path}: shorter than minLength={value}"
    if keyword == "maxLength":
        return f"{path}: longer than maxLength={value}"
    if keyword == "pattern":
        return f"{path}: does not match pattern {value!r}"
    if keyword == "minimum":
        return f"{path}: below minimum={value}"
    if keyword == "maximum":
        return f"{path}: above maximum={value}"
    if keyword == "minItems":
        return f"{path}: fewer than minItems={value}"
    if keyword == "maxItems":
        return f"{path}: more than maxItems={value}"
    if keyword == "minProperties":
        return f"{path}: fewer than minProperties={value}"
    if keyword == "uniqueItems":
        return f"{path}: items are not unique"
    if keyword == "multipleOf":
        return f"{path}: not a multiple of {value}"
    if keyword == "anyOf":
        return f"{path}: does not satisfy any of the {len(value or [])} allowed variants"
    if keyword == "oneOf":
        return (
            f"{path}: does not satisfy exactly one of the {len(value or [])} "
            "allowed variants"
        )
    if keyword == "format":
        return f"{path}: invalid {value} value"
    if keyword in ("dependentRequired", "dependencies"):
        deps = value if isinstance(value, dict) else {}
        for required in deps.values():
            names = required if isinstance(required, list) else [required]
            missing = [n for n in names if n not in (instance or {})]
            if missing:
                return f"{path}: missing dependency {missing[0]!r}"
    # Unknown/other keyword: keep the library's explanation rather than hiding it.
    return f"{path}: {error.message}"


def validate(payload: Any, schema: dict | None, *, path: str = "$") -> list[str]:
    """Validate ``payload`` against ``schema``; return a list of error strings.

    An empty list means valid. ``None``/``{}`` means "no contract declared" and is
    always valid — prompt specs and tool schemas are optional at the call site.
    """
    if not schema:
        return []
    if not JSONSCHEMA_AVAILABLE:  # pragma: no cover - minimal-install fallback
        return validate_builtin(payload, schema, path=path)
    # Local import keeps the type checker happy: the module-level names are
    # deliberately Optional (they are None on a minimal install), and the guard
    # above is the only thing that makes them safe to use here.
    from jsonschema.validators import Draft202012Validator, validator_for

    validator_cls = validator_for(schema, default=Draft202012Validator)
    validator = validator_cls(schema, format_checker=_FORMAT_CHECKER)
    return [_format_message(error) for error in validator.iter_errors(payload)]


def validate_builtin(payload: Any, schema: dict | None, *, path: str = "$") -> list[str]:
    """Subset validator used when ``jsonschema`` is unavailable (and in tests)."""
    if not schema:
        return []
    errors: list[str] = []
    _validate_builtin(payload, schema, path, errors)
    return errors


def _validate_builtin(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, t) for t in allowed):
            errors.append(
                f"{path}: expected {'|'.join(str(a) for a in allowed)}, "
                f"got {_type_label(value)}"
            )
            return  # deeper checks are meaningless on a type mismatch

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    if isinstance(value, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append(f"{path}: shorter than minLength={min_len}")
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append(f"{path}: longer than maxLength={max_len}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = True  # a broken pattern is a spec bug, not caller's
            if not matched:
                errors.append(f"{path}: does not match pattern {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: below minimum={minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: above maximum={maximum}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: fewer than minItems={min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: more than maxItems={max_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _validate_builtin(item, item_schema, f"{path}[{i}]", errors)

    if isinstance(value, dict):
        min_props = schema.get("minProperties")
        if isinstance(min_props, int) and len(value) < min_props:
            errors.append(f"{path}: fewer than minProperties={min_props}")
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            if field not in value:
                errors.append(f"{path}: missing required property {field!r}")
        for field, sub_schema in properties.items():
            if field in value and isinstance(sub_schema, dict):
                _validate_builtin(value[field], sub_schema, f"{path}.{field}", errors)
        extra = schema.get("additionalProperties")
        if extra is False:
            for field in value:
                if field not in properties:
                    errors.append(f"{path}: unexpected property {field!r}")
        elif isinstance(extra, dict):
            for field, val in value.items():
                if field not in properties:
                    _validate_builtin(val, extra, f"{path}.{field}", errors)
