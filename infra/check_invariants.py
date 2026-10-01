#!/usr/bin/env python3
"""Assert the security invariants MAIA's V1 stack actually claims.

WHY THIS EXISTS. Compiling every template and running the parameter and
negative tests was green on 2026-10-01 with `enablePurgeProtection: false`
in the Key Vault module. Compile checks the SHAPE of a template; it says
nothing about whether the values in it are safe.

WHY IT READS COMPILED JSON AND NOT THE .bicep SOURCE. Grepping the source
for `enablePurgeProtection: true` would pass just as happily on a line
inside a comment, inside a string, or on a resource that V1 never
deploys. The compiled ARM template is what Azure actually receives.

WHY THE WALKER IS DEFENSIVE. Bicep's languageVersion 2.0 emits nested
deployments whose `resources` is a SYMBOLIC-NAME MAP, not a list:

    resources[1].properties.template.resources
      -> dict keyed by symbolic name
         ("managedIdentity", "entraApplication", "entraServicePrincipal")

An earlier checker assumed `resources` was always a list, iterated the
dict, received its string KEYS, and called .get() on a string. That raised
AttributeError. The template was fine; the walker was stale. Both shapes
are legal ARM, so both are accepted here.

FAIL-CLOSED CONTRACT. Unknown structure is a FAILURE with the JSON path,
never a traceback and never a skip. A checker that cannot see a vault
cannot confirm a guarantee, and calling that anything else is how a
security check turns into decoration.
"""
from __future__ import annotations

import json
import sys

RED = "\033[31m"
GREEN = "\033[32m"
BOLD = "\033[1m"
OFF = "\033[0m"

# Only properties whose safety is a design guarantee, not a default.
INVARIANTS = [
    ("enablePurgeProtection", True,
     "A vault without purge protection can be emptied by an identity holding "
     "only Reader over the subscription. Deletion is recoverable; the "
     "contents may not be. This must be asserted, not merely defaulted."),
    ("enableRbacAuthorization", True,
     "Without RBAC authorization the vault also honours legacy access "
     "policies, so any principal holding a data-plane role gets in "
     "regardless of the role assignments this stack manages."),
]

KEYVAULT_TYPE = "microsoft.keyvault/vaults"


class Unsupported(Exception):
    """A resource-container shape this checker cannot read."""

    def __init__(self, path, expected, got):
        super().__init__(
            f"unsupported resource node at {path}: expected {expected}, got {got}")
        self.path = path


def iter_resources(node, path):
    """Yield (json_path, resource_object) for every resource under `node`.

    Accepts both legal ARM encodings of a `resources` container:
      * list of resource objects (classic form)
      * dict mapping symbolic name -> resource object (languageVersion 2.0)
    Anything else raises Unsupported carrying the JSON path.
    """
    if isinstance(node, list):
        for index, entry in enumerate(node):
            child = f"{path}[{index}]"
            if not isinstance(entry, dict):
                raise Unsupported(child, "object", type(entry).__name__)
            yield child, entry
        return
    if isinstance(node, dict):
        for key, entry in node.items():
            child = f"{path}.{key}"
            if not isinstance(entry, dict):
                raise Unsupported(child, "object", type(entry).__name__)
            yield child, entry
        return
    raise Unsupported(path, "list or object", type(node).__name__)


def find_vaults(node, path):
    """Collect Key Vaults, recursing into nested deployment templates."""
    found = []
    if isinstance(node, dict):
        resources = node.get("resources")
        if resources is not None:
            for child_path, resource in iter_resources(resources,
                                                       f"{path}.resources"):
                rtype = resource.get("type")
                if isinstance(rtype, str) and rtype.lower() == KEYVAULT_TYPE:
                    found.append((child_path, resource))
                props = resource.get("properties")
                if isinstance(props, dict):
                    nested = props.get("template")
                    if isinstance(nested, dict):
                        found.extend(find_vaults(
                            nested, f"{child_path}.properties.template"))
    elif isinstance(node, list):
        for index, entry in enumerate(node):
            if isinstance(entry, (dict, list)):
                found.extend(find_vaults(entry, f"{path}[{index}]"))
    return found


def main(argv):
    if len(argv) < 2:
        print(f"usage: {argv[0]} <compiled-template.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            template = json.load(handle)
    except OSError as exc:
        print(f"{RED}{BOLD}  FAIL{OFF}  cannot read {argv[1]}: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"{RED}{BOLD}  FAIL{OFF}  {argv[1]} is not valid JSON: {exc}")
        return 1

    if not isinstance(template, dict):
        print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: top-level template "
              f"is {type(template).__name__}, expected object")
        return 1

    try:
        vaults = find_vaults(template, "<template>")
    except Unsupported as exc:
        # Fail closed, with the path. Never a traceback.
        print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: {exc}")
        print("        the walker cannot read this template, so no security "
              "invariant can be confirmed.")
        return 1

    if not vaults:
        # Not a skip. An invariant that cannot be located is an unverified
        # guarantee, and calling it anything else is how a security check
        # turns into decoration.
        print(f"{RED}  FAIL{OFF}  no Microsoft.KeyVault/vaults found in the "
              f"template")
        print("        V1 claims a Key Vault. If it was renamed, moved or "
              "removed, the security boundary changed and this check cannot "
              "confirm it.")
        return 1

    failures = 0
    for index, (path, vault) in enumerate(vaults):
        label = f"{path} ({index + 1}/{len(vaults)})"
        props = vault.get("properties")
        if not isinstance(props, dict):
            print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: {label} has "
                  f"properties of type {type(props).__name__}, expected object")
            failures += 1
            continue
        for prop, expected, why in INVARIANTS:
            actual = props.get(prop, "<absent>")
            if actual == expected:
                print(f"{GREEN}  PASS{OFF}  {label}: {prop} == {expected}")
            else:
                failures += 1
                print(f"{RED}  FAIL{OFF}  {label}: {prop} is {actual!r}, "
                      f"expected {expected!r}")
                print(f"        {why}")

    if failures:
        print(f"{RED}{BOLD}  {failures} security invariant(s) violated{OFF}")
        return 1
    print(f"{GREEN}  {len(INVARIANTS)} invariant(s) held across "
          f"{len(vaults)} vault(s){OFF}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Unsupported as exc:  # last-resort guard; must never traceback
        print(f"{RED}{BOLD}  FAIL{OFF}  invariant discovery: {exc}")
        sys.exit(1)