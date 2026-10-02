# MAIA — Azure OIDC Delivery Contract (M1) — VERIFIED_LIVE

```text
evidence_date  : 2026-10-02T15:43:23Z
workflow       : Azure Verify (manual — OIDC + transient)
run_id         : 37028967610
ref            : main
source_sha     : 7a692e0c5383cca38529220a72808a7cb9c98fdb
inputs         : environment=validation, apply=false
result         : completed / success
azure_mutation : NONE (apply=false; the transient job was skipped)
```

This document records the **deploy path**, not the application. Nothing in this
run started the MAIA API. What it proves is that the pipeline can reach Azure
with no client secret, that the federation is actually constrained, and that
remote state works.

| Job | Conclusion |
|---|---|
| OIDC login (no client secret) | `success` |
| OIDC negative control (wrong subject must be REJECTED) | `success` |
| Remote state + plan invariants | `success` — 7 passed, 0 failed |
| Transient apply + verify + destroy | `skipped` (opt-in, `apply=false`) |

---

## 1. OIDC_POSITIVE = VERIFIED_LIVE

`azure/login@7184910d9eb2b1c5e48f7073824a90609bb9b6d6` (pinned by SHA), with
`id-token: write` and `environment: azure-verify`. No client secret is present
in the workflow.

`az account show` output — the identity actually obtained, not merely that the
step exited 0:

```json
{
  "id": "a3deec78-7edb-41cd-9e94-ec1d4d9379f5",
  "name": "Azure subscription 1",
  "state": "Enabled",
  "user": {
    "name": "***",
    "type": "servicePrincipal"
  }
}
```

`type: servicePrincipal` is the load-bearing detail: the credential is a
federated identity, not a user and not a password.

The job also re-runs the repo's own `tests/probe_no_client_secret.py` against
`infra/terraform`, so a future edit that reintroduces a secret fails the build
rather than passing quietly.

```text
OIDC_POSITIVE   = VERIFIED_LIVE
NO_CLIENT_SECRET = VERIFIED_LIVE
```

## 2. OIDC_NEGATIVE = VERIFIED_LIVE

A positive login alone cannot distinguish a correctly-scoped federated
credential from an over-broad one. This job mints a token **without** the
`azure-verify` environment, so the OIDC `sub` claim differs from the
credential's subject, and requires the rejection to be for the *right reason*:

```text
azure/login outcome without the environment: failure
ERROR: AADSTS700213: No matching federated identity record found for
presented assertion subject
'repo:imtarget05@163159731/MAIA@1357198812:ref:refs/heads/main'

PASS: rejected with AADSTS700213 — no matching federated identity
      for the presented SUBJECT. Azure quotes the subject it
      rejected, so the mismatch is directly evidenced.

subject restriction enforced: the credential accepts
repo:imtarget05/MAIA:environment:azure-verify and rejects everything else.
```

The distinction this guards against, and which the run log shows was a real
observed failure mode rather than a hypothetical:

| Condition | Outcome | Verdict |
|---|---|---|
| subject correctly restricted | fails `AADSTS700213` | GREEN |
| over-broad credential | login **succeeds** | RED |
| unrelated breakage | fails for another reason | RED |

```text
OIDC_NEGATIVE       = VERIFIED_LIVE
SUBJECT_RESTRICTION = ENFORCED (environment-scoped)
```

## 3. REMOTE_STATE = VERIFIED_LIVE

`terraform init` ran against the remote backend, and the workflow asserts the
state is genuinely remote rather than a local file that merely looks fine.

`terraform plan` produced **create-only** changes against allowlisted types,
and all seven plan invariants passed:

```text
PASS DRIFT0: all planned changes are create-only of allowlisted types
PASS ID1: exactly one user-assigned managed identity
PASS ENT1: MyOrg audience + v2 tokens
PASS KV1: RBAC-only, purge protection, retention 90d
PASS KV2: V1 public profile preserved (Disabled arrives in V5)
PASS RBAC1: Secrets User grant present (conditional emission intact)
PASS RBAC2: Contributor grant present; scope is RG-derived by construction

Summary: 7 passed, 0 failed
```

```text
REMOTE_STATE     = VERIFIED_LIVE
PLAN_INVARIANTS  = 7/7 PASSED
```

---

## 4. What this evidence does NOT establish

Stated plainly, because the distinction is the point:

- **No application runtime.** The MAIA API was never started. This says
  nothing about whether MAIA serves a request correctly.
- **No Azure resources were created.** `apply=false`; the transient job was
  skipped. The plan is a plan.
- **No PostgreSQL / Redis / Qdrant connectivity** was exercised here. Those
  belong to the application-runtime evidence, not the delivery contract.
- **The federated credential is environment-scoped**, so this run required the
  `azure-verify` environment to exist with its protection rules. That is a
  deliberate control, and it means a fork without that environment fails
  closed rather than open.

## 5. Reproduce

```bash
gh workflow run azure-verify.yml --ref main \
  -f environment=validation -f apply=false
gh run watch
```

Read-only by default. The only input that creates billable resources is
`apply=true`, and that job destroys within the same run.
