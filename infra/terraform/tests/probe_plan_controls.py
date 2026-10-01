#!/usr/bin/env python3
"""Negative-control probe for check_plan_invariants.py (TF-M14).

WHY this file exists: a checker that has only ever been observed to PASS proves
nothing. The roadmap's execution contract is explicit — mutation evidence counts
only if the mutation was applied, the intended branch executed, the intended
semantic changed, and the exact control failed FOR THE INTENDED REASON. This
probe applies each mutation to a known-good plan fixture and asserts that the
matching invariant is the one that reports FAIL.

It also asserts the read-error contract: an unreadable or unparseable plan must
exit 1 with a verdict line on stdout, never a Python traceback.

Usage: python3 tests/probe_plan_controls.py
Exit:  0 = every control bit for its intended reason. 1 = at least one did not.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKER = HERE / "check_plan_invariants.py"
FIXTURE = HERE / "fixtures" / "plan.v1.json"

# Strip colour if the checker ever emits it: the probe asserts on the verdict
# text, and an escape sequence between FAIL and the invariant name would make a
# correct FAIL look absent.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_checker(plan_text: str) -> tuple[int, str, str]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(plan_text)
        tmp_path = fh.name
    proc = subprocess.run(
        [sys.executable, str(CHECKER), tmp_path],
        capture_output=True,
        text=True,
        check=False,
    )
    Path(tmp_path).unlink(missing_ok=True)
    return proc.returncode, proc.stdout, proc.stderr


def dumps(plan: object) -> str:
    return json.dumps(plan, indent=2, sort_keys=True)


def fail_lines(stdout: str) -> list[str]:
    return [
        ANSI.sub("", ln).strip()
        for ln in stdout.splitlines()
        if "FAIL" in ANSI.sub("", ln)
    ]


def mutate_vault_property(plan: dict, prop: str, value: object) -> dict:
    out = copy.deepcopy(plan)
    for change in out["resource_changes"]:
        if change.get("type") == "azurerm_key_vault":
            change["change"]["after"][prop] = value
    return out


def drop_role_grant(plan: dict, role_suffix: str) -> dict:
    out = copy.deepcopy(plan)
    out["resource_changes"] = [
        c
        for c in out["resource_changes"]
        if not (
            c.get("type") == "azurerm_role_assignment"
            and role_suffix
            in str(c.get("change", {}).get("after", {}).get("role_definition_id", ""))
        )
    ]
    return out


def add_change(plan: dict, change: dict) -> dict:
    out = copy.deepcopy(plan)
    out["resource_changes"].append(change)
    return out


def drop_type(plan: dict, rtype: str) -> dict:
    out = copy.deepcopy(plan)
    out["resource_changes"] = [
        c for c in out["resource_changes"] if c.get("type") != rtype
    ]
    return out


def set_registration_property(plan: dict, prop: str, value: object) -> dict:
    out = copy.deepcopy(plan)
    for change in out["resource_changes"]:
        if change.get("type") == "azuread_application_registration":
            change["change"]["after"][prop] = value
    return out


def main() -> int:
    if not FIXTURE.exists():
        print(f"FAIL fixture: {FIXTURE} is missing")
        return 1

    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    checks: list[tuple[str, bool, str]] = []

    # C0: the unmutated fixture must PASS. Without this, every mutation below
    # would "bite" for the wrong reason (a checker that always fails).
    code, out, err = run_checker(dumps(base))
    tail = out.strip().splitlines()[-1] if out.strip() else "<empty>"
    checks.append(
        (
            "C0 baseline fixture passes",
            code == 0 and "7 passed, 0 failed" in out,
            f"exit={code} tail={tail}",
        )
    )

    mutations: list[tuple[str, str, dict]] = [
        (
            "C1 purge protection off -> KV1",
            "KV1",
            mutate_vault_property(base, "purge_protection_enabled", False),
        ),
        (
            "C2 RBAC-only off -> KV1",
            "KV1",
            mutate_vault_property(base, "rbac_authorization_enabled", False),
        ),
        (
            "C3 retention changed -> KV1",
            "KV1",
            mutate_vault_property(base, "soft_delete_retention_days", 7),
        ),
        (
            "C4 public endpoint closed in V1 -> KV2",
            "KV2",
            mutate_vault_property(base, "public_network_access_enabled", False),
        ),
        (
            "C5 a destroy appears -> DRIFT0",
            "DRIFT0",
            add_change(
                base,
                {
                    "address": "azurerm_key_vault.vault",
                    "mode": "managed",
                    "type": "azurerm_key_vault",
                    "change": {"actions": ["delete"]},
                },
            ),
        ),
        (
            "C6 type outside the allowlist -> DRIFT0",
            "DRIFT0",
            add_change(
                base,
                {
                    "address": "azurerm_storage_account.sneaky",
                    "mode": "managed",
                    "type": "azurerm_storage_account",
                    "change": {"actions": ["create"], "after": {}},
                },
            ),
        ),
        (
            "C7 Secrets User grant removed -> RBAC1",
            "RBAC1",
            drop_role_grant(base, "4633458b-17de-408a-b874-0445c86b69e6"),
        ),
        (
            "C8 Contributor grant removed -> RBAC2",
            "RBAC2",
            drop_role_grant(base, "b24988ac-6180-42a0-ab88-20f7382dd24c"),
        ),
        (
            "C12 UAMI removed -> ID1",
            "ID1",
            drop_type(base, "azurerm_user_assigned_identity"),
        ),
        (
            "C13 audience widened -> ENT1",
            "ENT1",
            set_registration_property(base, "sign_in_audience", "AzureADMultipleOrgs"),
        ),
        (
            "C14 token version downgraded -> ENT1",
            "ENT1",
            set_registration_property(base, "requested_access_token_version", 1),
        ),
    ]

    for name, invariant, plan in mutations:
        code, out, err = run_checker(dumps(plan))
        hit = [ln for ln in fail_lines(out) if ln.startswith(f"FAIL {invariant}:")]
        checks.append(
            (
                name,
                code == 1 and bool(hit) and "Traceback" not in err,
                f"exit={code} expected='FAIL {invariant}' got={hit or '<no such FAIL>'}",
            )
        )

    # Read-error contract: fail closed with a verdict line, never a traceback.
    for name, payload, needle in [
        (
            "C9 unparseable JSON -> verdict, no traceback",
            "not json at all",
            "plan is not valid JSON",
        ),
        (
            "C10 non-object plan root -> verdict, no traceback",
            "[1, 2, 3]",
            "plan root is list",
        ),
    ]:
        code, out, err = run_checker(payload)
        checks.append(
            (
                name,
                code == 1 and "Traceback" not in err and needle in out,
                f"exit={code} traceback={'Traceback' in err} verdict={needle in out}",
            )
        )

    # A missing file is also a read error, not a crash.
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(HERE / "fixtures" / "does-not-exist.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    checks.append(
        (
            "C11 missing plan file -> verdict, no traceback",
            proc.returncode == 1
            and "Traceback" not in proc.stderr
            and "not readable" in proc.stdout,
            f"exit={proc.returncode} traceback={'Traceback' in proc.stderr}",
        )
    )

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name.ljust(width)}  {detail}")
    failed = [c for c in checks if not c[1]]
    print(f"\nSummary: {len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
