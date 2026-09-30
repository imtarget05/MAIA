import json
import pathlib
import subprocess
import sys

GOLD = pathlib.Path(__file__).resolve().parent / "golden/no_answer.jsonl"
rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]

# Content audit only: assert VERIFIED so verify_row runs its corpus checks.
# The rows themselves are NOT modified by this probe.
for r in rows:
    r["review_status"] = "VERIFIED"
    r["absence_probe_terms"] = r.get("probe_terms", [])

tmp = GOLD.parent / "_audit_tmp.jsonl"
tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
               encoding="utf-8")
out = subprocess.run(
    [sys.executable, str(GOLD.parent.parent / "verify_eval_rows.py"),
     "--file", "_audit_tmp", "--json"], capture_output=True, text=True)
tmp.unlink()
print(out.stdout)
