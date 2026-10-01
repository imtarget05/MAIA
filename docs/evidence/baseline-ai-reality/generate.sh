#!/usr/bin/env bash
# Regenerates the WAVE 0 baseline AI reality check.
#
# WHY this builds its own virtualenv instead of using the developer's: the whole
# question is "what does the SHIPPED IMAGE execute?", and the developer's .venv
# has fastembed, rank-bm25, sentence-transformers and redis installed. Measuring
# there would answer a question nobody asked — the dev box is not the product.
# requirements.api.txt is the exact file Dockerfile.api installs, so a venv built
# from it is the production dependency set, not an approximation of it.
#
# Usage:  ./docs/evidence/baseline-ai-reality/generate.sh
# Writes: ./docs/evidence/baseline-ai-reality/<today>-baseline.log
# Exit:   0 (this records facts; it is a measurement, not a pass/fail gate)
#
# Note: this is deliberately NOT wired into CI. A CI runner would have to build
# this venv on every run to learn a fact that only changes when
# requirements.api.txt changes. The cheaper control for that is a dependency
# contract test — see the same report's "how to keep this honest" note.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO" || exit 2

OUT_DIR="docs/evidence/baseline-ai-reality"
VENV="${MAIA_APICheck_VENV:-/tmp/maia_apicheck}"
OUT="$OUT_DIR/$(date +%Y-%m-%d)-baseline.log"
mkdir -p "$OUT_DIR"

section() { printf '\n## %s\n' "$1" >>"$OUT"; }
p() { printf '%s\n' "$*" >>"$OUT"; }

{
  echo "# MAIA — WAVE 0 baseline AI reality check"
  echo "measured_at : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "source_sha  : $(git rev-parse HEAD)   (the product source under measurement;"
  echo "              this log is committed on top of that commit, so the two SHAs differ)"
  echo "dependency set : requirements.api.txt (the exact file Dockerfile.api installs)"
  echo "question    : what does the shipped image EXECUTE, not what does the source contain?"
} >"$OUT"

section "Why this venv exists"
p "Dockerfile.api line 24: RUN pip install --no-cache-dir -r requirements.api.txt"
p "The repository's own .venv additionally installs fastembed, rank-bm25,"
p "sentence-transformers and redis. Measuring there would report capabilities"
p "the production image does not have."
p ""
p "Building a clean venv from requirements.api.txt ..."
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || exit 2
fi
"$VENV/bin/pip" install -q --no-input -r requirements.api.txt >/dev/null 2>&1
p "venv ready: $("$VENV/bin/python" -c 'import sys;print(sys.executable)')"
p "packages installed: $("$VENV/bin/python" -m pip list --format=freeze 2>/dev/null | wc -l | tr -d ' ')"

section "Measurement"
rm -rf /tmp/maia_baseline_corpus
"$VENV/bin/python" tools/baseline_ai_reality_check.py >>"$OUT" 2>&1

section "How to keep this honest"
p "A finding here is a statement about requirements.api.txt, so the durable"
p "control is a dependency contract test, not this script. The next step of"
p "WAVE 3 is a test that asserts each capability the product claims is either"
p "importable from requirements.api.txt or explicitly declared dormant, so a"
p "capability cannot silently disappear from the image."
p ""
p "Re-run after ANY change to requirements.api.txt, Dockerfile.api, or the"
p "import sites named in the report."

echo "wrote $OUT"
