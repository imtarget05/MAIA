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
# The path must be rooted at the document, not merely contain a bracket. A
# traversal that propagated a constant still satisfies a substring check while
# pointing at the wrong location -- which is the case where a fix gets applied to
# a file that is not the one that regressed.
check("A2 LIST -> JSON path propagated and rooted", "$.resources[0]" in out,
      f"LIST branch did not propagate a rooted JSON path: {out!r}")

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
# C2 proves the symbolic key was reached, but not that it was reached BY A PATH.
# Requiring the full nested path is what distinguishes propagated traversal from a
# re-rooted guess that happens to land on the right node.
check("C3 nested MAP -> full nested path propagated",
      "$.resources[0].properties.template.resources.deepVault" in out,
      f"nested path not propagated through the descent: {out!r}")

# --- C4. nested deployment -> LIST -> vault -----------------------------------
# The MAP and LIST branches build their own paths, so a defect fixed in one is
# invisible to a test that only exercises the other. Worse, a re-rooted list index
# is SEMANTICALLY IDENTICAL at depth 0 -- "{path}" is already "$" there -- so no
# root-level assertion can detect it. This case puts a list inside a deployment
# template, giving the parent a non-trivial path, which is the only configuration
# in which nested index propagation becomes observable at all.
doc = {"resources": [
    {
        "type": "Microsoft.Resources/deployments",
        "name": "nested",
        "properties": {
            "template": {
                "resources": [
                    {
                        "type": "Microsoft.Resources/deployments",
                        "name": "inner",
                        "properties": {"template": {"resources": [vault()]}},
                    }
                ]
            }
        },
    }
]}
rc, out, tb = run(doc)
check("C4 nested LIST -> exit 0, no traceback", rc == 0 and not tb,
      f"rc={rc} tb={tb}")
check("C5 nested LIST -> full nested index path propagated",
      "$.resources[0].properties.template.resources[0]"
      ".properties.template.resources[0]" in out,
      f"nested list path not propagated through the descent: {out!r}")

# --- D. malformed entry ------------------------------------------------------
doc = {"resources": ["not-a-resource"]}
rc, out, tb = run(doc)
check("D1 malformed entry -> non-zero exit", rc not in (0, None), f"rc={rc}")
check("D2 malformed entry -> no traceback", not tb, "traceback leaked")
check("D3 malformed entry -> JSON path shown",
      "resources[0]" in out, "no JSON path in diagnostic")
# Rooted at the document, not merely containing a bracket: a traversal
# propagating a constant still satisfies D3 while naming the wrong location.
check("D4 malformed entry -> path rooted at the document",
      "$." in out and "$.resources[0]" in out,
      f"path not rooted at the document: {out!r}")

# --- E. malformed container --------------------------------------------------
doc = {"resources": "not-a-container"}
rc, out, tb = run(doc)
check("E1 malformed container -> non-zero exit", rc not in (0, None), f"rc={rc}")
check("E2 malformed container -> no traceback", not tb, "traceback leaked")
check("E3 malformed container -> JSON path shown",
      "resources" in out, "no JSON path in diagnostic")
check("E4 malformed container -> path rooted at the document",
      "$." in out, f"path not rooted at the document: {out!r}")

# --- F. invariants still bite ------------------------------------------------
# --- F. invariants still bite ------------------------------------------------
# Written as explicit vaults rather than vault(**{"purge": False}): a type
# checker cannot see which parameter an unpacked dict binds to and resolves the
# first positional slot, which reports a bool being passed to "name". Naming the
# argument keeps the intent readable and the types checkable.
doc = {"resources": [vault(name="noPurge", purge=False)]}
rc, out, tb = run(doc)
check("F1 purgeProtection=false bites", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": [vault(name="noRbac", rbac=False)]}
rc, out, tb = run(doc)
check("F2 rbacAuthorization=false bites", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": []}
rc, out, tb = run(doc)
check("F3 vault absent bites (never SKIP)", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": [vault()]}
del doc["resources"][0]["properties"]["enablePurgeProtection"]
rc, out, tb = run(doc)
check("F4 absent property bites", rc not in (0, None) and not tb, f"rc={rc}")

doc = {"resources": [vault()]}
doc["resources"][0]["properties"]["enablePurgeProtection"] = "false"
rc, out, tb = run(doc)
check("F5 string false-string bites", rc not in (0, None) and not tb, f"rc={rc}")

passed = sum(1 for _, c, _ in results if c)
print(f"\n{passed}/{len(results)} traversal contracts hold")
sys.exit(0 if passed == len(results) else 1)