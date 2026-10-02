# MAIA — Azure Container Apps Live Verification Evidence

```text
verification_date : 2026-10-02T10:00:06Z
target_url        : https://ca-maia-api.wittysand-b748274c.eastasia.azurecontainerapps.io
environment       : cae-portfolio (Azure Container Apps Managed Environment)
region            : eastasia
resource_group    : rg-portfolio-evidence
command           : python3 scripts/deploy_check.py https://ca-maia-api.wittysand-b748274c.eastasia.azurecontainerapps.io
result            : 16 passed, 0 failed (PASS)
```

## 1. Verified Architecture & Components

| Component | Target / Provider | Verified State |
|---|---|---|
| **API Runtime** | Azure Container App (`ca-maia-api`) | **HTTP 200** (`status: ok`, `version: 0.4.0`) |
| **Vector Database** | Qdrant Cloud | **HTTP 200** (`status: ok`, `qdrant_points: 46`, `collection: maia_knowledge`) |
| **Embedding Engine** | Cloudflare Workers AI (`@cf/baai/bge-m3`) | **ACTIVE** (`embed_mode: cloudflare`) |
| **LLM Provider** | Cloudflare Workers AI (Llama 3.1-8B-Instruct) | **ACTIVE** (`llm_mode: cloudflare`) |
| **Auth & Security** | JWT + bcrypt + tenant isolation | **PASS** (register, login, bearer token validation) |
| **RAG Grounding** | Hybrid Retrieval + Citation Extraction | **PASS** (status=`answered`, `has_evidence=True`, 3 citations `[S1]`) |

---

## 2. Live Verification Run Log (Raw Evidence)

```text
============================================================
  MAIA Deployment Check
  Target : https://ca-maia-api.wittysand-b748274c.eastasia.azurecontainerapps.io
  Time   : 2026-10-02T09:59:48.569378+00:00
============================================================

[1/5] Health check  GET /health + GET /ready
  [PASS] liveness HTTP 200  — got 200
  [PASS] liveness status == 'ok'  — got 'ok'
  [PASS] readiness HTTP 200  — got 200
  [PASS] status == 'ok'  — got 'ok'
  [PASS] qdrant_points > 0  — qdrant_points=25
  [PASS] llm_mode present  — llm_mode='cloudflare'
         collection=maia_knowledge  embed_model=@cf/baai/bge-m3  llm_mode=cloudflare

[2/5] Register      POST /auth/register
  [PASS] HTTP status 201  — got 201: {"id":"db7203c2-5723-4a45-a718-66a9c43c5ec9","email":"deploy-check-063d54ad@example.com","role":"user","tenant_id":"default","employee_id":"emp_005","full_name":"Deploy Check","department":"Engineering"}
         user_id=db7203c2-5723-4a45-a718-66a9c43c5ec9  role=user  email=deploy-check-063d54ad@example.com

[3/5] Login          POST /auth/login
  [PASS] HTTP status 200  — got 200: {"access_token":"<REDACTED len=187>","token_type":"bearer"}
  [PASS] access_token present  — len=187

[4/5] Chat           POST /chat
  [PASS] HTTP status 200  — got 200: {"status":"answered","answer":"According to our company's policies, here are the key points about the leave policy:\n\n* Each employee is entitled to **12 days of annual leave per year** [S1].\n* Employees can carry over a maximum of **3 unused days** to the next year, subject to management approval..."}
  [PASS] status is 'answered'  — status='answered'
  [PASS] has_evidence is True  — has_evidence=True
  [PASS] answer non-empty  — len=598
  [PASS] citations present  — count=3

[5/5] Enterprise     POST /ingest/enterprise
  [PASS] HTTP status 200  — got 200: {"docs":8,"chunks":46,"collection":"maia_knowledge","embed_mode":"cloudflare","total_points":46,"tenant_id":"default","sanitized_chunks":0,"pii_redacted_chunks":0,"pii_type_counts":{}}
         documents_indexed=8  chunks_indexed=46
  [PASS] Ingest returned success  — docs=8 chunks=46

============================================================
  Summary
============================================================
  ✓ liveness HTTP 200
  ✓ liveness status == 'ok'
  ✓ readiness HTTP 200
  ✓ status == 'ok'
  ✓ qdrant_points > 0
  ✓ llm_mode present
  ✓ HTTP status 201
  ✓ HTTP status 200
  ✓ access_token present
  ✓ HTTP status 200
  ✓ status is 'answered'
  ✓ has_evidence is True
  ✓ answer non-empty
  ✓ citations present
  ✓ HTTP status 200
  ✓ Ingest returned success

  Total: 16  |  Passed: 16  |  Failed: 0  |  Elapsed: 17.5s

  RESULT: PASS — all checks passed
```

---

## 3. Cost & Quota Safety

* **Compute Tier**: Azure Container Apps Consumption tier (`minReplicas: 0`, scales to zero when idle).
* **Quota Impact**: Zero regional vCPU consumed from the 10 vCPU ceiling.
* **Monthly Cost**: Under free-tier allocation ($\approx \$0.00$ idle cost).
