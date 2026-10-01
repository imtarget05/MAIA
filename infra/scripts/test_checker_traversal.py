#!/usr/bin/env python3
"""Traversal contract tests for the ARM invariant checker.

These exist because an exit-code-only assertion is not enough. A walker can
"pass" by never entering the branch it claims to check. Every positive case
below asserts that a SPECIFIC vault was discovered at a SPECIFIC path.

Run: python3 infra/scripts/test_checker_traversal.py <checker.py>
"""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile

CHECKER = sys.argv[1]
TMPDIR = pathlib.Path(tempfile.mkdtemp())
TIMEOUT = 30  # a canonical ARM walk is milliseconds; minutes means a bug


def run(doc):
    with (TMPDIR / "t.json").open("w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    try:
        p = subprocess.run([sys.executable, CHECKER, str(TMPDIR / "t.json")],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT", True
    out = p.stdout + p.stderr
    return p.returncode, out, "Traceback" in out


def vault(name="v", purge=True, rbac=True):
    return {
        "type": "Microsoft.KeyVault/vaults",
        "apiVersion": "2023-07-01",
        "name": name,
        "properties": {
            "enablePurgeProtection": purge,
            "enableRbacAuthorization": rbac,
        },
    }


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{'' if cond else '  <- ' + detail}")


# --- A. plain LIST -----------------------------------------------------------
doc = {"resources": [vault()]}
rc, out, tb = run(doc)
check("A1 LIST -> vault discovered (exit 0)", rc == 0 and not tb, f"rc={rc} tb={tb}")

# --- B. symbolic-name MAP ----------------------------------------------------
doc = {"resources": {"vaultSymbol": vault()}}
rc, out, tb = run(doc)
check("B1 MAP -> vault discovered (exit 0)", rc == 0 and not tb, f"rc={rc} tb={tb}")
check("B2 MAP -> vault path shown in output", "vaultSymbol" in out,
      "output never mentioned the symbolic key")

# --- C. nested deployment -> MAP -> vault ------------------------------------
# This is the case that distinguishes "traversed" from "did not crash".
doc = {"resources": [
    {
        "type": "Microsoft.Resources/deployments",
        "name": "nested",
        "properties": {
            "template": {
                "languageVersion": "2.0",
                "resources": {"deepVault": vault()},
            }
        },
    }
]}
rc, out, tb = run(doc)
check("C1 nested MAP -> exit 0, no traceback", rc == 0 and not tb,
      f"rc={rc} tb={tb}")
check("C2 nested MAP -> vault ACTUALLY observed", "deepVault" in out,
      "checker skipped the nested symbolic-name map entirely")

# --- D. malformed entry ------------------------------------------------------
doc = {"resources": ["not-a-resource"]}
rc, out, tb = run(doc)
check("D1 malformed entry -> non-zero exit", rc not in (0, None), f"rc={rc}")
check("D2 malformed entry -> no traceback", not tb, "traceback leaked")
check("D3 malformed entry -> JSON path shown",
      "resources[0]" in out, "no JSON path in diagnostic")

# --- E. malformed container --------------------------------------------------
doc = {"resources": "not-a-container"}
rc, out, tb = run(doc)
check("E1 malformed container -> non-zero exit", rc not in (0, None), f"rc={rc}")
check("E2 malformed container -> no traceback", not tb, "traceback leaked")
check("E3 malformed container -> JSON path shown",
      "resources" in out, "no JSON path in diagnostic")

# --- F. invariants still bite ------------------------------------------------
for label, kwargs in (("purgeProtection", {"purge": False}),
                      ("rbacAuthorization", {"rbac": False})):
    doc = {"resources": [vault(**kwargs)]}
    rc, out, tb = run(doc)
    check(f"F1 {label}=false bites", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": []}
rc, out, tb = run(doc)
check("F2 vault absent bites (never SKIP)", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": [vault()]}
del doc["resources"][0]["properties"]["enablePurgeProtection"]
rc, out, tb = run(doc)
check("F3 absent property bites", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": [vault()]}
doc["resources"][0]["properties"]["enablePurgeProtection"] = "false"
rc, out, tb = run(doc)
check("F4 string 'false' bites", rc not in (0, None) and not tb, f"rc={rc}")

passed = sum(1 for _, c, _ in results if c)
print(f"\n{passed}/{len(results)} traversal contracts hold")
sys.exit(0 if passed == len(results) else 1)