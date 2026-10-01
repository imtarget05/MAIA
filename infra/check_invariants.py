#!/usr/bin/env python3
"""Assert the security invariants MAIA's V1 stack actually claims.

WHY THIS EXISTS. A compile proves a template is well-formed; it says nothing
about whether the values in it are safe. Before this check existed, flipping
`enablePurgeProtection` to false in the Key Vault module left every other CI
check reporting success, because nothing inspected resource properties. The
property itself was always correct; what was missing was anything that would
notice it changing.

WHY IT READS COMPILED JSON AND NOT THE .bicep SOURCE. Grepping the source for
`enablePurgeProtection: true` passes just as happily on a line inside a comment
or on a resource V1 never deploys. The compiled ARM template is what Azure
receives, so asserting on it tests the real artifact.

WHY THE WALK IS DEFENSIVE. The shape of a compiled Bicep template is not
uniform, and this script was written against two different entrypoints that
disagreed:

  * a module compiles to a `Microsoft.Resources/deployments` resource whose
    payload sits under properties.template, one level deeper;
  * inside that nested template, `resources` is a list of dicts for real
    resources but of BARE STRINGS for the symbolic names of the module's own
    variables, functions and outputs;
  * `properties` itself may be a string, because a resource whose properties are
    computed rather than literal serialises the whole bag as an ARM expression.

Every one of those crashed an earlier version of this walk with an
AttributeError, and a security check that crashes is a security check that gets
switched off. So the walk records the JSON path of whatever it looks at, skips
anything of an unexpected shape, and a resource that cannot be found is reported
as a failure listing the paths that were searched, not a traceback.

SCOPE, DELIBERATELY NARROW. This is not an Azure Policy engine and does not try
to be. It asserts exactly the two properties the V1 module comments claim as its
security boundary. A property with no claim behind it does not belong here,
because an assertion nobody can justify is an assertion nobody maintains.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterator

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
OFF = "\033[0m"

VAULT_TYPE = "Microsoft.KeyVault/vaults"

# The invariants V1 claims, and the reason each one is claimed. Adding a row
# here is a security decision and needs a reason in this table, not a bare
# boolean.
INVARIANTS: list[tuple[str, Any, str]] = [
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

# Guards against a malformed or self-referential structure turning the walk into
# an infinite recursion. Bicep nests two levels today (entrypoint, then module);
# four leaves room without being unbounded.
MAX_DEPTH = 4


def iter_resources(
    template: Any, depth: int = 0, path: str = "$"
) -> Iterator[tuple[dict, str]]:
    """Yield (resource, json_path) for every real resource in a template.

    Yields nothing for entries of an unexpected shape rather than raising. See
    the module docstring for the three shapes this has to survive; a crash here
    would take the invariant check down with it.
    """
    if depth > MAX_DEPTH or not isinstance(template, dict):
        return

    resources = template.get("resources")
    if resources is None:
        return

    if isinstance(resources, dict):
        # Bicep languageVersion 2.0 emits a SYMBOLIC-NAME MAP here. Treating
        # that as "no resources" skipped an entire nested deployment subtree --
        # including any Key Vault inside it -- and still exited 0. The map is
        # legal ARM, so it is traversed, not rejected.
        for key in resources:
            here = f"{path}.resources.{key}"
            value = resources[key]
            if not isinstance(value, dict):
                raise Unsupported(here, "object", type(value).__name__)
            yield value, here
            yield from _descend(value, here, depth)
        return

    if not isinstance(resources, list):
        # An unreadable required structure is a FAILURE carrying its path, not
        # an empty result. Skipping it would mean claiming to have checked
        # something that was never read.
        raise Unsupported(
            f"{path}.resources", "list or symbolic-name map",
            type(resources).__name__)

    declared = _declared_symbols(template)
    for index, resource in enumerate(resources):
        here = f"{path}.resources[{index}]"
        if isinstance(resource, str):
            # A bare string can legitimately be a symbolic reference to a
            # variable, function or output of the enclosing module. It must be
            # a DECLARED name; an undeclared one is malformed, not ignorable.
            if resource in declared:
                continue
            raise Unsupported(
                here, "object, or a declared symbolic name", f"str {resource!r}")
        if not isinstance(resource, dict):
            raise Unsupported(here, "object", type(resource).__name__)

        yield resource, here
        yield from _descend(resource, here, depth)


def _descend(resource: dict, here: str, depth: int):
    """Recurse into properties.template.

    Shared by both container shapes so the two cannot drift apart. `yield from`
    is required: calling this without iterating the returned generator would
    create it, never advance it, and silently skip the nested subtree.
    """
    properties = resource.get("properties")
    if isinstance(properties, dict):
        nested = properties.get("template")
        if isinstance(nested, dict):
            yield from iter_resources(
                nested, depth + 1, f"{here}.properties.template"
            )


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


def _declared_symbols(template: Any) -> set:
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


def find_vaults(template: Any) -> list[tuple[dict, str]]:
    return [
        (r, path) for r, path in iter_resources(template) if r.get("type") == VAULT_TYPE
    ]




def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <compiled-template.json>", file=sys.stderr)
        return 2

    path = sys.argv[1]

    # A missing or unparseable file is reported rather than raised. This script
    # runs immediately after a compile, so "the compile produced no output" is a
    # real condition a caller has to be able to read, and a traceback would bury
    # the cause under a stack trace in the CI log.
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

    print(f"{BOLD}-- V1 security invariants on the compiled template{OFF}")
    print(f"        {path}")

    if not isinstance(template, dict):
        print(
            f"{RED}  FAIL{OFF}  the compiled template is a "
            f"{type(template).__name__}, not an object"
        )
        return 1

    try:
        vaults = find_vaults(template)
    except Unsupported as exc:
        # Fail closed, with the JSON path. Never a traceback, never a skip.
        print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: {exc}")
        print("        the walk refuses to guess about an unreadable structure,")
        print("        so no security invariant can be confirmed here.")
        return 1

    if not vaults:
        # Not a skip. An invariant that cannot be located is an unverified
        # guarantee, and calling that anything else is how a security check turns
        # into decoration. The searched paths are listed so a reader can tell
        # "the vault moved" from "this is not the template I expected".
        print(f"{RED}  FAIL{OFF}  no {VAULT_TYPE} found in the template")
        print("        V1 claims a Key Vault. If it was renamed, moved or")
        print("        removed, the security boundary changed and this check")
        print("        cannot confirm it. Paths searched:")
        for resource, resource_path in iter_resources(template):
            print(f"          {resource_path}  {resource.get('type', '?')}")
        return 1

    failures = 0
    for vault, vault_path in vaults:
        properties = vault.get("properties")
        if not isinstance(properties, dict):
            # Reachable: a vault whose properties are an ARM expression rather
            # than an object. The values are then not statically visible, which
            # is itself worth failing on rather than skipping.
            failures += 1
            print(
                f"{RED}  FAIL{OFF}  {vault_path}: properties is not an object "
                f"({type(properties).__name__}); the security values cannot be read"
            )
            continue

        for prop, expected, why in INVARIANTS:
            actual = properties.get(prop, "<absent>")
            # Both value and type are compared, so a truthy "false" string from a
            # mis-serialised template cannot pass as the boolean True.
            if type(actual) is type(expected) and actual == expected:
                print(f"{GREEN}  PASS{OFF}  {vault_path}: {prop} == {expected}")
            else:
                failures += 1
                print(
                    f"{RED}  FAIL{OFF}  {vault_path}: {prop} is {actual!r}, "
                    f"expected {expected!r}"
                )
                print(f"        {why}")

    if failures:
        print(f"{RED}{BOLD}  {failures} security invariant(s) violated{OFF}")
        return 1

    print(
        f"{GREEN}  {len(INVARIANTS)} invariant(s) held across {len(vaults)} vault(s){OFF}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Guards against a malformed or self-referential structure turning the walk into
# an infinite recursion. Bicep nests two levels today (entrypoint, then module);
# four leaves room without being unbounded.
MAX_DEPTH = 4
