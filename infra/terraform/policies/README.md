# Policy scan inputs (Trivy)
#
# This directory is scanned by `scan.sh`, which `scripts/phase1_check.sh` runs
# as part of the Phase 1 gate and `.github/workflows/terraform-validate.yml`
# runs in CI.
#
# ## Files
#
# | File | Purpose |
# |---|---|
# | `trivy.yaml` | Scan configuration: report misconfigurations only from Terraform this repository owns (`exclude-downloaded-modules`) |
# | `trivyignore` | Reviewed exceptions. One documented exception today: `AZU-0013` |
# | `scan.sh` | Positive control + repo scan + ignore-set guard. Exit 0 only if all three hold |
#
# ## Why the gate has a positive control
#
# A clean scan and a scan that is accidentally disabled both exit 0. `scan.sh`
# therefore writes a deliberately weakened Key Vault into a temp directory and
# requires Trivy to report it (`AZU-0016`, purge protection off) before it will
# trust the clean result below. Without that step, "0 findings" would mean
# "nothing was looked for" just as easily as it meant "nothing was found".
#
# ## The one documented exception
#
# `AZU-0013` — *Vault network ACL does not block access by default* — is
# suppressed for V1 on purpose. In V1 the vault keeps its public endpoint open
# (`public_network_access_enabled = true`, `default_action = "Allow"`) because
# there is no private endpoint yet and the workload reaches the vault over the
# public endpoint. A `Deny` default here would not be safer — it would lock the
# workload out of its own secrets, turning a security finding into an outage.
#
# Full reasoning and the removal condition are in `trivyignore`. Two artefacts
# are coupled to it and must change in the same review:
#
# - `../modules/keyvault/main.tf` — the `enable_private_network` switch
# - `../tests/check_plan_invariants.py` — invariant `KV2`, which asserts the
#   public endpoint is currently **enabled**
#
# ## Running it
#
# ```bash
# cd infra/terraform
# ./policies/scan.sh
# ```
#
# `PHASE1_SKIP_TRIVY=1` acknowledges a run without the scan. It is refused by
# default, because a silently-skipped security gate prints the same thing as a
# passing one.
#
# ## Adding a suppression
#
# 1. Add the ID with its reason and removal condition to `trivyignore`.
# 2. Record it in "The one documented exception" above, with the same reasoning.
# 3. Update the expected ignore set in `scan.sh`.
#
# All three must land together or `scan.sh` fails — that is the point.
