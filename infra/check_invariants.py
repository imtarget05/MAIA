#!/usr/bin/env python3
"""Assert the security invariants MAIA's V1 stack actually claims.

WHY THIS EXISTS. `infra/validate.sh` compiles every template and runs the
parameter and negative tests, and all of that was green on 2026-10-01 with
`enablePurgeProtection: false` in the Key Vault module. Compile checks the
SHAPE of a template; it says nothing about whether the values in it are safe.
This is the check that closes that gap.

WHY IT READS COMPILED JSON AND NOT THE .bicep SOURCE. Grepping the source for
`enablePurgeProtection: true` would pass just as happily on a line inside a
comment, inside a string, or on a resource that V1 never deploys. The compiled
ARM template is what Azure actually receives, so asserting on it tests the real
artifact. It also means these invariants survive a refactor that moves the
resource into a different module or renames the file.

SCOPE, DELIBERATELY NARROW. This is not an Azure Policy engine and does not try
to be. It asserts exactly the two properties the V1 module comments claim as
its security boundary. A property with no claim behind it does not belong here,
because an assertion nobody can justify is an assertion nobody maintains.

EXIT CODES. 0 = every invariant held. 1 = at least one failed. A missing
resource is a FAILURE, never a skip: "the vault is not in the template" means
the guarantee cannot be checked, and reporting that as a pass is the exact
failure mode this script exists to prevent.
"""

from __future__ import annotations

import json
import sys
from typing import Any

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
OFF = "\033[0m"

# The invariants V1 claims, and the reason each one is claimed. Adding a row
# here is a security decision and needs a reason in this table, not a bare
# boolean.
INVARIANTS: list[tuple[str, bool, str]] = [
    (
        "enablePurgeProtection",
        True,
        "An accidental `az keyvault secret delete` becomes a support ticket "
        "instead of an outage. Irreversible once enabled, which is why it is "
        "asserted rather than merely defaulted.",
    ),
    (
        "enableRbacAuthorization",
        True,
        "RBAC only. The access-policy model is a second authorisation system "
        "that is off by default in a new vault and easy to leave enabled next "
        "to RBAC, which is how a 'no access' finding happens.",
    ),
]


class Unsupported(Exception):
    """A resource shape the walk refuses to guess about.

    Raised rather than skipped. A skipped shape means the checker did not read
    everything the template contains, so it cannot claim to have confirmed a
    security invariant. Carries the JSON path so the failure is actionable.
    """

    def __init__(self, path: str, expected: str, got: str):
        super().__init__(
            f"unsupported resource node at {path}: expected {expected}, got {got}")
        self.path = path


def _declared_symbols(template: dict[str, Any]) -> set:
    """Names a bare string in `resources` may legitimately refer to.

    Bicep serialises a module's variables, functions and outputs as symbolic
    names, so a string there is not automatically corruption -- but it must be
    a DECLARED name. An undeclared string is malformed.
    """
    names: set = set()
    if not isinstance(template, dict):
        return names
    for key in ("functions", "variables", "outputs", "imports"):
        value = template.get(key)
        if isinstance(value, dict):
            names.update(k for k in value if isinstance(k, str))
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    names.add(entry)
                elif isinstance(entry, dict):
                    for field in ("name", "symbolName"):
                        if isinstance(entry.get(field), str):
                            names.add(entry[field])
    return names


def iter_resources(template: dict[str, Any], depth: int = 0, path: str = "$"):
    """Yield (resource, json_path) for every resource in a compiled template.

    Bicep compiles a module block into a nested deployments resource whose
    payload sits under properties.template, and under languageVersion 2.0
    that nested template's `resources` is a SYMBOLIC-NAME MAP rather than a
    list. Iterating a dict yields its KEYS, so a list-only walk saw strings,
    skipped every one, and reported success having read nothing.

    The path is first-class traversal data, not decoration: a PASS line has to
    be able to name the exact object it observed.
    """
    if depth > 4:
        return
    resources = template.get("resources")
    if resources is None:
        return

    if isinstance(resources, dict):
        for key in resources:
            here = f"{path}.resources.{key}"
            value = resources[key]
            if not isinstance(value, dict):
                raise Unsupported(here, "object", type(value).__name__)
            yield value, here
            yield from _descend(value, depth, here)
    elif isinstance(resources, list):
        declared = _declared_symbols(template)
        for index, resource in enumerate(resources):
            here = f"{path}.resources[{index}]"
            if isinstance(resource, str):
                if resource in declared:
                    continue
                raise Unsupported(
                    here, "object, or a declared symbolic name",
                    f"str {resource!r}")
            if not isinstance(resource, dict):
                raise Unsupported(here, "object", type(resource).__name__)
            yield resource, here
            yield from _descend(resource, depth, here)
    else:
        raise Unsupported(f"{path}.resources", "list or symbolic-name map",
                          type(resources).__name__)


def _descend(resource: dict[str, Any], depth: int, path: str):
    """Recurse into properties.template.

    `yield from` is required at every call site: invoking this generator
    without iterating it creates it and discards it, silently skipping the
    nested subtree.
    """
    properties = resource.get("properties")
    if isinstance(properties, dict):
        nested = properties.get("template")
        if isinstance(nested, dict):
            yield from iter_resources(
                nested, depth + 1, f"{path}.properties.template")


def find_vaults(template: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Return every Key Vault together with the path it was found at."""
    return [
        (resource, resource_path)
        for resource, resource_path in iter_resources(template)
        if resource.get("type") == "Microsoft.KeyVault/vaults"
    ]


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <compiled-template.json>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    # WHY a missing file is reported rather than raised: this script is called
    # right after a compile, and "the compile produced no output" is a real
    # condition a caller has to be able to read. A traceback would exit non-zero
    # too, so the safety is the same, but it would bury the actual cause under a
    # stack trace in CI logs.
    try:
        with open(path, encoding="utf-8") as handle:
            template = json.load(handle)
    except FileNotFoundError:
        print(f"{RED}  FAIL{OFF}  compiled template not found: {path}")
        print("        The compile step should have produced this file.")
        return 1
    except json.JSONDecodeError as exc:
        print(f"{RED}  FAIL{OFF}  {path} is not valid JSON: {exc}")
        return 1

    try:
        vaults = find_vaults(template)
    except Unsupported as exc:
        print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: {exc}")
        print("        the walk refuses to guess about an unreadable "
              "structure, so no security invariant can be confirmed.")
        return 1
    failures = 0

    print(f"{BOLD}-- 7. V1 security invariants on the compiled template{OFF}")
    print(f"        {path}")

    if not vaults:
        # Not a skip. An invariant that cannot be located is an unverified
        # guarantee, and calling it anything else is how a security check turns
        # into decoration.
        print(f"{RED}  FAIL{OFF}  no Microsoft.KeyVault/vaults found in the template")
        print(
            "        V1 claims a Key Vault. If it was renamed, moved or removed, "
            "the security boundary changed and this check cannot confirm it."
        )
        return 1

    for index, (vault, vault_path) in enumerate(vaults):
        name = vault.get("name", "<unnamed>")
        props = vault.get("properties", {})
        label = (f"{vault_path}: {name}" if index == 0
                 else f"{vault_path}: {name} (resource {index})")

        for prop, expected, why in INVARIANTS:
            actual = props.get(prop, "<absent>")
            if actual is expected or actual == expected:
                print(f"{GREEN}  PASS{OFF}  {label}: {prop} == {expected}")
            else:
                failures += 1
                print(f"{RED}  FAIL{OFF}  {label}: {prop} is {actual!r}, expected {expected!r}")
                print(f"        {why}")

    if failures:
        print(f"{RED}{BOLD}  {failures} security invariant(s) violated{OFF}")
        return 1

    print(f"{GREEN}  {len(INVARIANTS)} invariant(s) held across {len(vaults)} vault(s){OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
