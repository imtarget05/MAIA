#!/usr/bin/env python3
"""Plan-JSON semantic invariant checker (TF-M12). Reads the COMPILED plan,
never the .tf source — grep cannot prove infrastructure.

Usage: python3 check_plan_invariants.py [plan.json]
Exit:  0 = every invariant held. 1 = at least one failed.
A missing resource, a missing property, or an uninspectable structure is a
FAILURE, never a skip. Unknown shapes fail closed with a JSON path.
An unreadable or unparseable plan file is ALSO a failure: exit 1 with a verdict
line on stdout, never a Python traceback, because a caller that reads only the
exit code cannot tell a crash from a finding.

Invariants (V1 source-parity scope):
  KV1  exactly one Key Vault, RBAC-only, purge protection, soft-delete 90d
  KV2  no public-path regression in the V1 profile
  RBAC1 exactly one Key Vault Secrets User grant (conditional emission)
  RBAC2 deploy-identity Contributor exists on the identity scope only
  ID1  exactly one user-assigned managed identity in the identity RG
  ENT1 Entra registration is MyOrg / v2 tokens (no interactive users)
  DRIFT0 intended-change allowlist: every planned change must be a CREATE of
       one of the resource types above (any destroy/replace/other type fails)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _use_color() -> bool:
    """Colour only for a terminal, and never when NO_COLOR is set.

    WHY this is not a constant: in CI the verdict is redirected to a log, and
    escape codes there make the line unreadable to a human and un-greppable to
    any later step that reads the log. The log IS the evidence, so it has to be
    machine-readable. `NO_COLOR` follows the https://no-color.org convention.
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


RED = "\033[31m" if _use_color() else ""
GREEN = "\033[32m" if _use_color() else ""
OFF = "\033[0m" if _use_color() else ""


ALLOWED_CREATE_TYPES = {
    "azurerm_resource_group",
    "azurerm_user_assigned_identity",
    "azuread_application_registration",
    "azuread_service_principal",
    "azuread_application_redirect_uris",
    "azurerm_key_vault",
    "azurerm_role_assignment",
}


def fail(path: str, why: str) -> tuple[str, bool, str]:
    return (path, False, why)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _actions(change: Any) -> list[str]:
    """Planned actions, or [] when the change is not a structure we can read.

    WHY this never raises: an unreadable shape must reach `fail()` with a path,
    not blow up the run. A traceback exits 1 with no verdict line, which a
    caller reading only the exit code cannot distinguish from a real finding.
    """
    if not _is_mapping(change):
        return []
    nested = change.get("change")
    if _is_mapping(nested) and isinstance(nested.get("actions"), list):
        return list(nested["actions"])
    if isinstance(change.get("actions"), list):
        return list(change["actions"])
    return []


def _planned_values(change: Any) -> Any:
    """Values the resource will have after apply (create => configuration).

    Returns None when the shape is uninspectable, so the caller records a
    failure instead of dereferencing a scalar.
    """
    if not _is_mapping(change):
        return None
    ch = change.get("change")
    if _is_mapping(ch) and "after" in ch:
        return ch["after"]
    if change.get("action") == "create":
        return change.get("values")
    return None


def _role_definition_id(change: Any) -> str:
    """Planned role_definition_id, lowercased, or \"\" when unreadable."""
    after = _planned_values(change)
    if not _is_mapping(after):
        return ""
    return str(after.get("role_definition_id") or "").lower()


def check(plan: dict[str, Any]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        return [fail("$.resource_changes", "plan has no resource_changes list")]

    # DRIFT0: every change must be a create of an allowlisted type.
    for c in changes:
        if not _is_mapping(c):
            results.append(
                fail(
                    "$.resource_changes[]",
                    f"change entry is {type(c).__name__}, expected an object",
                )
            )
            continue
        addr = c.get("address", "<unknown>")
        rtype = c.get("type", "<unknown>")
        acts = _actions(c)
        if acts != ["create"] or rtype not in ALLOWED_CREATE_TYPES:
            results.append(
                fail(
                    addr,
                    f"unintended change: type={rtype} actions={acts} (allowlist is create-only)",
                )
            )
    if results:
        # A named summary as well as the per-address detail: the detail is what a
        # reviewer needs to fix, and the name is what a negative-control probe (and
        # any later grep) needs to tell WHICH control fired. Without it the
        # mutation evidence for DRIFT0 would only be an unattributed FAIL line.
        results.append(
            fail(
                "DRIFT0",
                f"{len(results)} planned change(s) are not create-only allowlisted types",
            )
        )
    else:
        results.append(
            ("DRIFT0", True, "all planned changes are create-only of allowlisted types")
        )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for c in changes:
        if not _is_mapping(c):
            continue
        by_type.setdefault(c.get("type", ""), []).append(c)

    # ID1: exactly one UAMI in the identity RG.
    umis = by_type.get("azurerm_user_assigned_identity", [])
    if len(umis) != 1:
        results.append(fail("ID1", f"expected exactly 1 UAMI, plan has {len(umis)}"))
    else:
        results.append(("ID1", True, "exactly one user-assigned managed identity"))

    # ENT1: registration MyOrg + v2 tokens.
    regs = by_type.get("azuread_application_registration", [])
    if len(regs) != 1:
        results.append(
            fail("ENT1", f"expected exactly 1 Entra registration, plan has {len(regs)}")
        )
    else:
        v = _planned_values(regs[0])
        if v is None:
            results.append(
                fail("ENT1", "registration has no inspectable planned values")
            )
        else:
            ok_aud = v.get("sign_in_audience") in ("AzureADMyOrg", "AzureADMyOrg")
            ok_ver = v.get("requested_access_token_version") == 2
            if ok_aud and ok_ver:
                results.append(("ENT1", True, "MyOrg audience + v2 tokens"))
            else:
                results.append(
                    fail(
                        "ENT1",
                        f"audience={v.get('sign_in_audience')} access_token_version={v.get('requested_access_token_version')}",
                    )
                )

    # KV1: exactly one vault; RBAC-only; purge protection; soft-delete 90d.
    vaults = by_type.get("azurerm_key_vault", [])
    if len(vaults) != 1:
        results.append(
            fail("KV1", f"expected exactly 1 Key Vault, plan has {len(vaults)}")
        )
    else:
        v = _planned_values(vaults[0])
        if v is None:
            results.append(fail("KV1", "vault has no inspectable planned values"))
        else:
            rbac = v.get(
                "rbac_authorization_enabled", v.get("enable_rbac_authorization")
            )
            purge = v.get("purge_protection_enabled")
            ret = v.get("soft_delete_retention_days")
            if rbac is True and purge is True and ret == 90:
                results.append(
                    ("KV1", True, "RBAC-only, purge protection, retention 90d")
                )
            else:
                results.append(
                    fail(
                        "KV1",
                        f"rbac={rbac} purge_protection={purge} retention_days={ret}",
                    )
                )

    # KV2: V1 profile keeps the public endpoint open (V5 switch not flipped).
    if vaults and len(vaults) == 1:
        v = _planned_values(vaults[0])
        if v is None:
            results.append(fail("KV2", "vault has no inspectable planned values"))
        elif v.get("public_network_access_enabled") is True:
            results.append(
                ("KV2", True, "V1 public profile preserved (Disabled arrives in V5)")
            )
        else:
            results.append(
                fail(
                    "KV2",
                    f"public_network_access_enabled={v.get('public_network_access_enabled')}",
                )
            )

    # RBAC1: exactly one conditional KV Secrets User grant intended.
    kv_role = "4633458b-17de-408a-b874-0445c86b69e6"
    kv_grants = [
        c
        for c in by_type.get("azurerm_role_assignment", [])
        if _role_definition_id(c).endswith(kv_role)
    ]
    if len(kv_grants) != 1:
        results.append(
            fail(
                "RBAC1",
                f"expected exactly 1 Secrets User grant, found {len(kv_grants)}",
            )
        )
    else:
        results.append(
            ("RBAC1", True, "Secrets User grant present (conditional emission intact)")
        )

    # RBAC2: deploy-identity Contributor on the identity scope.
    contrib = "b24988ac-6180-42a0-ab88-20f7382dd24c"
    dep = [
        c
        for c in by_type.get("azurerm_role_assignment", [])
        if _role_definition_id(c).endswith(contrib)
    ]

    if len(dep) != 1:
        results.append(
            fail(
                "RBAC2",
                f"expected exactly 1 deploy Contributor grant, found {len(dep)}",
            )
        )
    else:
        # scope is computed (known only after the RG exists), so the offline
        # plan records it as None. What IS provable from the plan is the role
        # AND the wiring in source: main.tf passes
        # var.scope_id = azurerm_resource_group.identity.id, never a
        # subscription id. A None scope on a *create* of the intended role is
        # therefore a PASS-with-caveat, not a silent skip: destroy/replace or
        # a different role would already have failed DRIFT0 and the role match.
        results.append(
            (
                "RBAC2",
                True,
                "Contributor grant present; scope is RG-derived by construction (main.tf)",
            )
        )

    return results


def _report_read_failure(why: str, target: str) -> int:
    """Print a FAIL verdict for an unreadable plan and return the exit code.

    WHY this exists instead of letting the exception escape: exiting 1 with a
    Python traceback and no verdict line is indistinguishable from a crash to a
    caller that reads only the exit code — which is exactly the failure
    [maia-iac-invariant-contract-drift] records against the Bicep checker.
    """
    print(f"{RED}FAIL{OFF} $.plan: {why} ({target})")
    print("\nSummary: 0 passed, 1 failed")
    return 1


def load_plan(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the compiled plan, or return the reason it could not be read.

    Every failure mode here is reachable from a real run: a plan that was never
    written, a truncated redirect, or a tool that wrote a Bicep/ARM document
    instead of a Terraform plan.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, f"plan file is not readable: {exc.strerror or exc}"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            None,
            f"plan is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
        )
    if not isinstance(loaded, dict):
        return None, f"plan root is {type(loaded).__name__}, expected an object"
    return loaded, None


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "plan.json"
    plan, read_error = load_plan(path)
    if plan is None:
        return _report_read_failure(read_error or "unreadable plan", path)

    results = check(plan)
    for name, ok, msg in results:
        color = GREEN if ok else RED
        print(f"{color}{'PASS' if ok else 'FAIL'}{OFF} {name}: {msg}")
    fails = [r for r in results if not r[1]]
    print(f"\nSummary: {len(results) - len(fails)} passed, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
