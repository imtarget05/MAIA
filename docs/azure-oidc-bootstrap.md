# Azure Phase 2 — remote state, GitHub OIDC, and the cost controls

**Status: `IMPLEMENTED_UNVERIFIED`.** The source is here and the offline gate is
green, but nothing in this document has been executed against a live Azure
subscription yet. Every step below that reads like a measurement is a
*definition* of what must be measured, not a measurement. See
[`enterprise-target/COMPLETION-MATRIX.md`](./enterprise-target/COMPLETION-MATRIX.md)
row 2.

Read this before creating anything billable.

## 0. What bootstrap does and does not create

**No application compute / database / API stack is created during bootstrap.**
No VM, no APIM, no Front Door, no managed PostgreSQL, no managed Redis, no
Container App. Those are Phase 3+ and are **not authorised** by this procedure.

**Bootstrap does create a Storage Account**, and a storage account *can* incur
cost — capacity plus transactions — even when the expected amount is very small.
It is deliberately wrong to call this "no paid resources are created".

| Resource | Lifetime | Why |
|---|---|---|
| `rg-maia-tfstate` / `sttfmaia` / `tfstate` | **permanent** | Holds Terraform remote state. Destroying it destroys state history. Never transient. |
| `rg-maia-verify` + everything Terraform creates in it | **transient** | Destroyed after verification by `transient_verify.sh` |

The separation is the point: the budget cannot stop anything, so resource
*LIFETIME* is what actually controls spend — and only the application stack gets
a lifetime.

## 1. What the blocker actually was

Two separate things were called "the Azure blocker", and only one is a
credential problem:

| Blocker | Reality | Where it is handled |
|---|---|---|
| Cannot authenticate | **Already resolved.** `az login` works; the subscription is live | §2 |
| `.tfstate` on a laptop, no CI path | Real, unbuilt until now | §3, §4 |
| No cost ceiling | Real, and the most dangerous of the three | §5 |

The distinction matters because only the second and third are engineering work.
Re-running `az login` fixes nothing that was actually broken.

## 2. Local credential — already done, here is how to re-check

```bash
az login
az account list -o table
az account set --subscription "<SUBSCRIPTION_ID>"
az account show -o table
```

A green `az account show` printing the right tenant and subscription is the
whole Definition of Done for the local half of the credential blocker.

## 3. Remote state

`infra/terraform/backend.tf` declares an empty `backend "azurerm" {}` — partial
configuration. Every value lives in `environments/<env>/backend.hcl`:

```hcl
resource_group_name  = "rg-maia-tfstate"
storage_account_name = "sttfmaia"
container_name       = "tfstate"
key                  = "maia/prod.terraform.tfstate"

use_azuread_auth = true
```

Three properties, each a decision someone can undo by accident:

- **Partial config, not values in the backend block.** No line in this
  repository turns a leaked clone into a usable credential.
- **`use_azuread_auth = true`, so no account key is needed.** The state data
  plane authenticates with an Entra token. `scripts/bootstrap_azure.sh`
  additionally sets `--allow-shared-key-access false` on the storage account,
  which is the *control*; the backend config only chooses it. With SharedKey
  refused server-side, a leaked account key is useless against state.
- **A key per environment.** `dev` / `validation` / `prod` never share a state
  file, so transient-env churn cannot corrupt prod.

### Why there is no `use_oidc` in these files

A static `use_oidc = true` forces the azurerm backend down the GitHub Actions
OIDC path, which reads `ACTIONS_ID_TOKEN_REQUEST_TOKEN`. That variable does not
exist on a developer machine, so local `terraform init` would break. Committing
it would trade one broken path for another: CI green, laptop broken.

So authentication is supplied by whoever runs Terraform:

| | mechanism |
|---|---|
| **local** | `az login` — the backend picks up the token via `use_azuread_auth` |
| **CI** | `ARM_USE_OIDC=true` + `ARM_USE_AZUREAD_AUTH=true` + `ARM_CLIENT_ID` / `ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID`, exported by `.github/workflows/azure-verify.yml` |

One config, two paths, no second backend. `tests/probe_no_client_secret.py`
rule **S5** fails the build if `use_oidc` is ever hardcoded back in.

```bash
cd infra/terraform
terraform init -reconfigure -backend-config=environments/prod/backend.hcl
```

The offline gate is unaffected: `scripts/phase1_check.sh` inits with
`-backend=false`, which skips backend initialisation entirely.

## 4. GitHub OIDC — no client secret

```
GitHub Actions --OIDC token--> Entra app (maia-github-oidc)
                                    |  subject: repo:imtarget05/MAIA:environment:azure-verify
                                    v
                          RBAC on ONE resource group
```

Three GitHub **variables** (not secrets — these are identifiers):

```text
AZURE_CLIENT_ID        appId of maia-github-oidc
AZURE_TENANT_ID        the directory tenant id
AZURE_SUBSCRIPTION_ID  the target subscription
```

No client secret is created, because the app registration has no password
credential at all. `scripts/bootstrap_azure.sh` asserts that
(`length(passwordCredentials) == 0`), and
`infra/terraform/tests/probe_no_client_secret.py` fails the gate if a secret
reference reappears anywhere in the deploy path.

### Two planes, two roles — the failure this prevents

`Contributor` and `Storage Blob Data Contributor` are not interchangeable, and
omitting the second produces a confusing result: `azure/login` succeeds,
`az account show` succeeds, the management plane works — and then
`terraform init` fails with `403 AuthorizationPermissionMismatch`.

| Plane | Role | Scope | Needed by |
|---|---|---|---|
| management | `Contributor` | `rg-maia-verify` | Terraform creating/deleting resources |
| data | `Storage Blob Data Contributor` | `rg-maia-tfstate` | the backend reading/writing state blobs |

The same trap applies to **the person running bootstrap**. A subscription Owner
is a management-plane role and carries **no** `dataActions`, so
`az storage container create --auth-mode login` fails 403 even though every
other command in the script succeeds — leaving a half-built resource group
behind. `bootstrap_azure.sh` therefore grants the runner `Storage Blob Data
Contributor` on the state resource group *before* creating the container,
rather than re-enabling shared account keys, which would delete the property
that makes leaked keys useless.

### The negative control that makes this meaningful

`.github/workflows/azure-verify.yml` contains **two** login jobs:

| job | `environment:` | expected |
|---|---|---|
| `oidc-login` | `azure-verify` | login SUCCEEDS |
| `oidc-negative-control` | *(none, on purpose)* | login FAILS |

The second job is what stops a wildcard subject from looking correct. If the
federated credential accepted any token, `oidc-login` would go green and
`oidc-negative-control` would go red — failing the workflow. A single successful
login proves the happy path works and says nothing about whether it is
*constrained*.

### Why the environment is not bookkeeping

`environment:` is what changes the OIDC token's `sub` claim from
`repo:imtarget05/MAIA:ref:...` to
`repo:imtarget05/MAIA:environment:azure-verify`. It is not a label; it is the
## 5. Cost — the part people get wrong

> **An Azure budget is an alert. It is not a cap.** Exceeding it does **not**
> stop resources. There is no $10 hard stop, and writing "budget $10" in a plan
> is not a cost control.

So the cost control in this repository is not the budget. It is four things:

1. **Alert** — a monthly budget on `rg-maia-verify`, so an anomaly is noticed.
2. **Blast radius** — Contributor on one RG, so a mistake is contained.
3. **Lifetime** — `scripts/transient_verify.sh` applies, verifies and destroys in
   one run. The destroy is armed with `trap destroy EXIT` **before** the apply,
   so there is no path through the script that creates resources without a
   cleanup path already in place.
4. **Opt-in to spend** — the apply job in CI is `workflow_dispatch` only and
   defaults to `apply: false`. The default workflow run creates nothing.

A budget with no notification target is not monitoring — it exists, but nothing
tells anyone it was crossed. `bootstrap_azure.sh` accepts
`AZURE_BUDGET_ALERT_EMAIL`; without it the script prints
`NO ALERT RECIPIENT CONFIGURED` rather than inventing an address, and the
50/80/100% contacts must be added in the portal (RG → Cost Management →
Budgets → alert contact).

Note what is **exempt** from all of this: the state storage account is permanent
(§0). It is never a transient cost because it is never destroyed — and it is
also the one resource whose loss would be unrecoverable.

```bash
cd infra/terraform
./scripts/transient_verify.sh validation          # apply -> verify -> destroy
KEEP=1 ALSO_KEEP_RUNNING=1 ./scripts/transient_verify.sh validation   # opt out, loudly
```

Two keys are required to leave a stack running, because "I just want to look at
it tomorrow" is how an overnight bill happens.

### If the subscription is Azure for Students

Keep the spending limit. When the credit runs out, Azure disables the
subscription rather than falling through to unlimited pay-as-you-go — which is
the behaviour you want. Do not remove the limit to make a deploy succeed.

### Read the plan for cost, not the bill afterwards

Before any `apply` that touches the wider target architecture, read
`plan-resources.txt` (written by `transient_verify.sh`) and the plan itself. The
ones that move cost fast: APIM, Front Door Premium, managed PostgreSQL, managed
Redis, Log Analytics ingestion, Application Insights, always-on Container Apps,
Private Endpoints.

If a specific service or SKU is too expensive to deploy transiently, the honest
label is `IMPLEMENTED_NOT_RUNTIME_VERIFIED` — not a cheaper stand-in recorded
as "production verified".

## 6. Run order

```bash
# local, read-only
az account show                                   # credential blocker, local half

# bootstrap: creates the RG, storage, app, federation, budget
cd infra/terraform
./scripts/bootstrap_azure.sh --dry-run            # prints intent, mutates nothing
ALLOW_AZURE_MUTATION=1 ./scripts/bootstrap_azure.sh

# GitHub: create the `azure-verify` environment, add the three variables,
# then run the "Azure Verify" workflow with apply = false

# transient verification
./scripts/transient_verify.sh validation
```

`bootstrap_azure.sh` requires `ALLOW_AZURE_MUTATION=1` even though it creates
nothing expensive. It creates a resource group, a storage account and an app
registration, and an unconfirmed script that mutates a cloud account is exactly
the kind of thing that gets run twice by accident.

## 7. What is verified, and what is not

**Verified live (measured with `az` and with a real GitHub Actions run):**

- `rg-maia-tfstate` + `rg-maia-verify` exist; `sttfmaia` has
  `allowSharedKeyAccess=false`, HTTPS-only, TLS 1.2, blob public access off.
- `tfstate` container exists and is listable with `--auth-mode login` — Entra,
  no account key.
- `maia-github-oidc` has **0** password credentials and **0** key credentials.
- The federated credential is issuer `https://token.actions.githubusercontent.com`
  (no trailing slash), subject
  `repo:imtarget05@163159731/MAIA@1357198812:environment:azure-verify`, audience
  `api://AzureADTokenExchange`. It is the only one on the app.
- The GitHub service principal holds Contributor on `rg-maia-verify` only, plus
  Reader + Blob Data Contributor on `rg-maia-tfstate`, and Owner nowhere.
- **GitHub Actions OIDC, run `36983504649`, workflow green:**
  - `oidc-login` — `azure/login` with no client secret; `az account show`
    returned sub `a3deec78-…` / tenant `aa79a92c-…`.
  - `oidc-negative-control` — presented subject
    `repo:…@1357198812:ref:refs/heads/main`, Azure rejected it with
    **AADSTS700213** quoting that exact subject; the job stayed GREEN on that
    expected rejection.
  - `terraform-plan` — azurerm backend initialised through OIDC, no local state
    file, `state pull` reachable, plan **9 add / 0 change / 0 destroy**, all 7
    plan invariants PASS.
- `rg-maia-verify` still contains **zero** resources.

**Three things the federated subject got wrong, each found by a real run:**

| Symptom | Cause |
|---|---|
| `AADSTS700211` | Subject lacked the numeric ids GitHub appends: `repo:owner/repo` vs `repo:owner@163159731/repo@1357198812` |
| `AADSTS700211` again | Issuer had a trailing slash; GitHub presents it without one |
| `AADSTS50027` | The token itself was corrupted by sed string surgery, not by Azure policy |

**Still not verified:**

- Any `terraform apply` of the application stack. `deploy_identity_principal_id`
  is still the zero-GUID placeholder and `transient_verify.sh` refuses to apply
  until it is a real object id.
- The budget: Azure rejected every start date at both RG and subscription scope
  on this subscription, so `BUDGET_NOT_CREATED`. Create it in the portal, where
  the same dates are accepted.
authorisation input. Deleting it makes those jobs fail by design.

### Why Contributor on a resource group, not the subscription

A subscription-scoped Contributor can grant itself role assignments and
escalate to Owner. A resource-group-scoped one cannot grant anything outside
that group. One RG per project (`rg-maia-verify`, and equivalents for the other
repositories) keeps cost attribution, destruction and blast radius separable.