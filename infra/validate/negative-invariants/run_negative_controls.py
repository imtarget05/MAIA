#!/usr/bin/env python3
"""Negative controls for infra/check_invariants.py.

Each case mutates a DISPOSABLE copy of the compiled template and asserts the
checker's exit code and output. Nothing here touches the repo.
"""
import copy
import json
import pathlib
import subprocess
import sys

CHECKER = pathlib.Path(__file__).resolve().parents[2] / "check_invariants.py"
SRC = pathlib.Path("/tmp/arm/main.v1.json")
TMP = pathlib.Path("/tmp/arm/nc.json")


def run(doc, label):
    TMP.write_text(json.dumps(doc), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(CHECKER), str(TMP)],
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    traceback = "Traceback" in out
    print(f"  {label:38s} exit={proc.returncode}  traceback={'YES' if traceback else 'no'}")
    for line in out.strip().splitlines()[:3]:
        print(f"      {line[:104]}")
    return proc.returncode, out, traceback


def find_vault(doc):
    """Locate the vault's properties dict, whatever shape its container has."""
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("type"), str) and \
                    node["type"].lower() == "microsoft.keyvault/vaults":
                return node
            for value in node.values():
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    raise SystemExit("no vault found in the source template")


base = json.loads(SRC.read_text(encoding="utf-8"))
results = {}

# NC2 purge protection disabled
d = copy.deepcopy(base)
find_vault(d)["properties"]["enablePurgeProtection"] = False
results["NC2 purgeProtection=false"] = run(d, "NC2 purgeProtection=false")[:2]

# NC3 RBAC authorization disabled
d = copy.deepcopy(base)
find_vault(d)["properties"]["enableRbacAuthorization"] = False
results["NC3 rbacAuthorization=false"] = run(d, "NC3 rbacAuthorization=false")[:2]

# NC4 vault absent
d = copy.deepcopy(base)
target = find_vault(d)
parent = None
for node in [d]:
    pass


def strip_vault(doc):
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            resources = node.get("resources")
            if isinstance(resources, list):
                node["resources"] = [
                    r for r in resources
                    if not (isinstance(r, dict) and
                            isinstance(r.get("type"), str) and
                            r["type"].lower() == "microsoft.keyvault/vaults")]
            elif isinstance(resources, dict):
                for key in list(resources):
                    r = resources[key]
                    if isinstance(r, dict) and isinstance(r.get("type"), str) and \
                            r["type"].lower() == "microsoft.keyvault/vaults":
                        del resources[key]
            for value in node.values():
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)


strip_vault(copy.deepcopy(base))  # prove it runs without dying
d = copy.deepcopy(base)
strip_vault(d)
results["NC4 vault absent"] = run(d, "NC4 vault absent")[:2]

# NC5 unexpected ARM structure: resources becomes a string
d = copy.deepcopy(base)
d["resources"] = "not-a-container"
rc, out, tb = run(d, "NC5 resources is a string")
results["NC5 unexpected structure"] = (rc, out)

# NC5b nested resources entries that are strings (the old crash shape)
d = copy.deepcopy(base)
d["resources"][1]["properties"]["template"]["resources"] = ["a-string-entry"]
rc, out, tb = run(d, "NC5b nested entry is a string")
results["NC5b string entry"] = (rc, out)