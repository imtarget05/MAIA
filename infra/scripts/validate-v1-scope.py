#!/usr/bin/env python3
"""V1 deployment-scope invariant for the MAIA enterprise stack.

Compiles infra/main.bicep and asserts the V1 entrypoint cannot provision
later-wave resources:

1. Every compiled resource type is allowlisted, else fail closed.
2. Forbidden-prefix types must be absent, or carry a condition that is FALSE
   under both v1-dev and v1-prod.
3. Role assignments are scope-checked: a grant scoped to a forbidden-type
   resource (e.g. a Storage account) is treated as forbidden itself.
4. Conditions are evaluated in the NESTED module scope (invocation values
   over module defaults), because Bicep compiles `if` on module resources
   against the module's own parameters — not the entrypoint's.

Usage: python3 infra/scripts/validate-v1-scope.py   (exit 0 pass, 1 fail)
Requires: az bicep (offline build only, no Azure credentials, no deploy).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "infra" / "main.bicep"
MODULES_DIR = ROOT / "infra" / "modules"
PARAM_FILES = [
    ROOT / "infra" / "parameters" / "v1-dev.bicepparam",
    ROOT / "infra" / "parameters" / "v1-prod.bicepparam",
]

ALLOWED_PREFIXES = (
    "Microsoft.Resources/deployments",
    "Microsoft.Resources/resourceGroups",
    "Microsoft.ManagedIdentity/",
    "Microsoft.Graph/",
    "Microsoft.KeyVault/vaults",
    "Microsoft.Authorization/roleAssignments",
)

# Later-wave types: absent, or condition-false (rules 2-3 above).
FORBIDDEN_PREFIXES = (
    "Microsoft.Cdn/",
    "Microsoft.ApiManagement/",
    "Microsoft.ContainerRegistry/",
    "Microsoft.DBforPostgreSQL/",
    "Microsoft.Cache/",
    "Microsoft.Search/",
    "Microsoft.App/",
    "Microsoft.OperationalInsights/",
    "Microsoft.Insights/",
    "Microsoft.Storage/",
    "Microsoft.Network/",
    "Microsoft.DocumentDB/",
    "Microsoft.EventHub/",
    "Microsoft.ServiceBus/",
)

# Extension-type resources compile to symbolic names, not ARM types.
SYMBOLIC_TYPES = {
    "managedIdentity": "Microsoft.ManagedIdentity/userAssignedIdentities",
    "entraApplication": "Microsoft.Graph/applications",
    "entraServicePrincipal": "Microsoft.Graph/servicePrincipals",
}


def bicep_build(path: Path) -> dict:
    p = subprocess.run(
        ["az", "bicep", "build", "--file", str(path), "--stdout"],
        capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise SystemExit(f"bicep build failed for {path}:\n{p.stderr[:2000]}")
    return json.loads(p.stdout)


def _literal(raw: str, where: str):
    raw = raw.strip()
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("\\'", "'")
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw == "[]":
        return []
    if raw == "{}":
        return {}
    raise SystemExit(f"{where}: unsupported literal (extend the validator, "
                     f"do not guess): {raw[:80]}")


def parse_bicepparams(path: Path) -> dict:
    """Parse the small literal subset our parameter files use (flat values)."""
    text = path.read_text()
    params: dict = {}
    for m in re.finditer(
            r"(?m)^param\s+(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\[.*?\]|\{.*?\}|true|false|-?\d+)\s*$",
            text):
        params[m.group(1)] = _literal(m.group(2), str(path))
    # Multiline arrays/objects (one entry per line) for secret-name lists.
    for m in re.finditer(r"(?m)^param\s+(\w+)\s*=\s*\[\s*\n(.*?)\n\]",
                         text, re.S):
        items = re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(2))
        params[m.group(1)] = [i.replace("\\'", "'") for i in items]
    return params


def parse_module_defaults(module_path: Path) -> dict:
    """`param NAME TYPE = DEFAULT` defaults from a module source file."""
    text = module_path.read_text()
    defaults: dict = {}
    for m in re.finditer(
            r"(?m)^param\s+(\w+)\s+\w+\s*=\s*('(?:[^'\\]|\\.)*'|true|false|-?\d+|\[\]|\{\})\s*$",
            text):
        defaults[m.group(1)] = _literal(m.group(2), str(module_path))
    return defaults


def _split_args(s: str) -> list[str]:
    parts, depth, cur, instr = [], 0, "", False
    for ch in s:
        if ch == "'" and not (cur and cur[-1:] == "\\"):
            instr = not instr
        if not instr:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(cur.strip())
                cur = ""
                continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def eval_expr(expr, ctx: dict, where: str):
    expr = expr.strip()
    if expr.startswith("[") and expr.endswith("]"):
        return _eval_inner(expr[1:-1].strip(), ctx, where)
    raise SystemExit(f"{where}: not an ARM expression: {expr[:80]}")


def _eval_inner(expr: str, ctx: dict, where: str):
    m = re.fullmatch(r"parameters\('([^']+)'\)", expr)
    if m:
        name = m.group(1)
        if name not in ctx:
            raise SystemExit(f"{where}: condition references unresolvable "
                             f"parameter '{name}' (fail closed)")
        v = ctx[name]
        if isinstance(v, _Opaque):
            raise SystemExit(f"{where}: condition depends on opaque value "
                             f"'{name}' (fail closed)")
        return v
    m = re.fullmatch(r"'(?:[^'\\]|\\.)*'", expr)
    if m:
        return expr[1:-1]
    if expr in ("true", "false"):
        return expr == "true"
    if re.fullmatch(r"-?\d+", expr):
        return int(expr)
    m = re.fullmatch(r"(\w+)\((.*)\)", expr, re.S)
    if not m:
        raise SystemExit(f"{where}: unsupported ARM expression (extend the "
                         f"validator, do not guess): {expr[:80]}")
    fn, raw_args = m.group(1).lower(), m.group(2)
    args = [_eval_inner(a, ctx, where) for a in _split_args(raw_args)]
    if fn == "empty":
        return args[0] in (None, "", [], {})
    if fn == "not":
        return not args[0]
    if fn == "equals":
        return args[0] == args[1]
    if fn == "and":
        return all(args)
    if fn == "or":
        return any(args)
    raise SystemExit(f"{where}: unsupported ARM function '{fn}'")


class _Opaque:
    def __init__(self, text: str):
        self.text = text


def parse_invocations(main_src: str) -> dict:
    """deployment name -> (module relpath, {param: raw ARM value}).

    Keyed by the deployment's `name:` property (what ARM carries), not the
    Bicep symbolic name.
    """
    out = {}
    for m in re.finditer(
            r"module\s+\w+\s+'([^']+)'\s*=\s*\{(.*?)\n\}",
            main_src, re.S):
        rel, body = m.group(1), m.group(2)
        nm = re.search(r"(?m)^\s*name:\s*'([^']+)'", body)
        if not nm:
            raise SystemExit("module block without name: property "
                             "(fail closed)")
        pm = re.search(r"params:\s*\{(.*)\}\s*$", body, re.S)
        passed: dict = {}
        if pm:
            for am in re.finditer(r"(\w+):\s*(\[[^\]]*\]|'[^']*'|[^\s,}]+)",
                                  pm.group(1)):
                passed[am.group(1)] = am.group(2).strip()
        out[nm.group(1)] = (rel, passed)
    return out


def resolve_invocation_value(raw: str, entry_params: dict, where: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        m = re.fullmatch(r"parameters\('([^']+)'\)", inner)
        if m:
            name = m.group(1)
            if name not in entry_params:
                raise SystemExit(f"{where}: invocation passes unknown "
                                 f"entrypoint parameter '{name}'")
            return entry_params[name]
        return _Opaque(raw)
    # Bare identifier in a params block refers to the same-named entrypoint
    # parameter (Bicep scoping); anything else is opaque by design.
    if re.fullmatch(r"[A-Za-z_]\w*", raw):
        if raw in entry_params:
            return entry_params[raw]
        return _Opaque(raw)
    try:
        return _literal(raw, where)
    except SystemExit:
        return _Opaque(raw)


def scope_resource_type(scope_expr) -> str | None:
    """Extract the resource type from a role-assignment scope expression."""
    if not isinstance(scope_expr, str):
        return None
    m = re.search(r"resourceId\('([^']+)'", scope_expr)
    return m.group(1) if m else None


def main() -> int:
    template = bicep_build(MAIN)
    main_src = MAIN.read_text()
    invocations = parse_invocations(main_src)
    failures: list[str] = []
    checked = 0

    def check_resource(r, ctx: dict, ppath, where: str):
        if isinstance(r, str):
            if r not in SYMBOLIC_TYPES:
                failures.append(f"{where}: unknown symbolic resource '{r}' "
                                f"(fail closed)")
                return
            rtype, cond, scope = SYMBOLIC_TYPES[r], None, None
        else:
            rtype, cond = r.get("type", "?"), r.get("condition")
            scope = r.get("scope")
        if not any(rtype.startswith(p) for p in ALLOWED_PREFIXES) and not any(
                rtype.startswith(p) for p in FORBIDDEN_PREFIXES):
            failures.append(f"{where}: type {rtype} is neither allowlisted "
                            f"nor forbidden (fail closed)")
            return
        effective_forbidden = any(
            rtype.startswith(p) for p in FORBIDDEN_PREFIXES)
        if rtype == "Microsoft.Authorization/roleAssignments" and scope:
            stype = scope_resource_type(scope)
            if stype and any(stype.startswith(p) for p in FORBIDDEN_PREFIXES):
                effective_forbidden = True
                where = f"{where} (scope {stype})"
        if not effective_forbidden:
            return
        if not cond:
            failures.append(f"{where}: forbidden in V1 with NO condition")
            return
        for ppath, entry_params in param_sets:
            try:
                val = eval_expr(cond, ctx, where)
            except SystemExit as e:
                failures.append(str(e))
                continue
            if val:
                failures.append(f"{where}: forbidden in V1 but condition is "
                                f"TRUE under {ppath}")

    param_sets = [(p, parse_bicepparams(p)) for p in PARAM_FILES]
    for p, _ in param_sets:
        if not p.exists():
            print(f"FAIL: parameter file missing: {p}")
            return 1
    nested_ctx: dict = {}
    for dep in template.get("resources", []):
        if not isinstance(dep, dict):
            continue
        if dep.get("type") != "Microsoft.Resources/deployments":
            for ppath, _ in param_sets:
                check_resource(dep, {}, ppath.name,
                               f"top:{dep.get('name')}")
            checked += 1
            continue
        name = dep.get("name")
        if name not in invocations:
            failures.append(f"deployment '{name}': no matching module block "
                            f"in main.bicep (fail closed)")
            continue
        rel, passed = invocations[name]
        mod_path = (ROOT / "infra" / rel).resolve()
        if not str(mod_path).startswith(str(ROOT / "infra")):
            failures.append(f"deployment '{name}': module path escapes "
                            f"infra/ (fail closed)")
            continue
        ctx0 = parse_module_defaults(mod_path)
        for ppath, entry_params in param_sets:
            ctx = dict(ctx0)
            for k, raw in passed.items():
                ctx[k] = resolve_invocation_value(
                    raw, entry_params, f"{name}:{k}")
            nested_ctx[(name, str(ppath))] = ctx
        props = dep.get("properties") or {}
        tmpl = props.get("template")
        if not isinstance(tmpl, dict):
            failures.append(f"deployment '{name}': non-inline template "
                            f"(fail closed)")
            continue
        for r in tmpl.get("resources", []):
            for ppath, _ in param_sets:
                check_resource(r, nested_ctx[(name, str(ppath))], ppath,
                               f"{name}:{r.get('name') if isinstance(r, dict) else r}")
                checked += 1
    if failures:
        print("V1 SCOPE INVARIANT FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"V1 scope invariant holds: {checked} resource checks, 0 later-wave "
          f"types provisionable under v1-dev and v1-prod.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
