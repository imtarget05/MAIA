#!/usr/bin/env bash
# Fails when a committed evidence log cites a source SHA that no branch contains.
#
# WHY THIS EXISTS. Rebasing a branch rewrites commit SHAs. Evidence logs record
# the SHA they were measured at, so after a rebase a log can point at a commit
# that still resolves through the reflog but is on no branch at all — a ghost.
# The log then claims to describe source nobody can check out, which is worse
# than a stale number, because it looks authoritative.
#
# This happened for real: rebasing migration/terraform-maia onto origin/main
# left two evidence logs citing unreachable SHAs, while the logs' own content
# stayed byte-identical. Nothing in the repository noticed.
#
# Usage:  ./scripts/verify_evidence_shas.sh
# Exit:   0 = every cited SHA is on a branch. 1 = at least one is a ghost.
#
# This checks REACHABILITY, not correctness. A reachable SHA can still be the
# wrong one; that is what the logs' own "method" lines are for.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2

# Logs that record a measured source SHA. Extend this list rather than globbing
# every file under docs/: a log without a source_sha is not making the claim.
LOGS=(
  docs/evidence/terraform-phase1/*-gate.log
  docs/evidence/baseline-ai-reality/*-baseline.log
)

printf 'HEAD: %s\n\n' "$(git rev-parse --short HEAD)"

CHECKED=0
GHOSTS=0
for f in "${LOGS[@]}"; do
  [ -f "$f" ] || continue
  sha="$(grep -m1 -E '^source_sha[[:space:]]*:' "$f" | awk '{print $3}')"
  if [ -z "$sha" ]; then
    printf 'FAIL %s\n     no source_sha recorded\n' "$f"
    GHOSTS=$((GHOSTS + 1))
    continue
  fi
  CHECKED=$((CHECKED + 1))
  if git rev-parse --verify --quiet "$sha" >/dev/null 2>&1; then
    if git --no-pager branch -a --contains "$sha" 2>/dev/null | grep -q .; then
      printf 'PASS %s -> %s (on a branch)\n' "$f" "$(git rev-parse --short "$sha")"
    else
      printf 'FAIL %s -> %s (exists in the object store but is on NO branch — ghost SHA)\n' "$f" "$(git rev-parse --short "$sha")"
      GHOSTS=$((GHOSTS + 1))
    fi
  else
    printf 'FAIL %s -> %s (does not resolve at all)\n' "$f" "${sha:0:8}"
    GHOSTS=$((GHOSTS + 1))
  fi
done

printf '\n'
if [ "$CHECKED" -eq 0 ]; then
  echo "verify-evidence-shas: FAIL (no evidence log found to check)"
  exit 1
fi
if [ "$GHOSTS" -gt 0 ]; then
  echo "verify-evidence-shas: FAIL ($GHOSTS of $CHECKED logs cite an unreachable SHA)"
  echo "  Re-run the generate.sh for each affected directory, then commit the"
  echo "  regenerated log. Do not hand-edit a source_sha line."
  exit 1
fi
echo "verify-evidence-shas: PASS ($CHECKED logs, all on a branch)"
