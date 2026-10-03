#!/usr/bin/env bash
# M6 live probe against the deployed MAIA container app.
# Registers an isolated throwaway account, then exercises the served HITL path.
# No real user data is touched. Credentials are generated here and never stored.
set -uo pipefail

U="https://ca-maia-api.wittysand-b748274c.eastasia.azurecontainerapps.io"
STAMP=$(date +%s)
EMAIL="m6probe${STAMP}@example.com"
PASS="M6Pr0be-$(head -c 9 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 9)"

echo "== register (isolated account) =="
curl -s -o /tmp/m6_reg.json -w 'HTTP %{http_code}  %{time_total}s\n' --max-time 40 \
  -X POST "$U/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"full_name\":\"M6 Probe\"}"
head -c 200 /tmp/m6_reg.json; echo

echo "== login =="
# The deployed endpoint is an OAuth2 password flow: it consumes
# application/x-www-form-urlencoded with grant_type/username/password, not a
# JSON body. Both facts read from the deployment's own openapi.json.
curl -s -o /tmp/m6_login.json -w 'HTTP %{http_code}  %{time_total}s\n' --max-time 40 \
  -X POST "$U/auth/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$EMAIL" \
  --data-urlencode "password=$PASS" \
  --data-urlencode "grant_type=password"

TOKEN=$(/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
try:
    d = json.load(open("/tmp/m6_login.json"))
except Exception:
    print(""); raise SystemExit
t = d.get("access_token") or (d.get("data") or {}).get("access_token") or ""
print(t)
PY
)
if [ -z "$TOKEN" ]; then
  echo "LOGIN DID NOT RETURN A TOKEN (auth may be disabled on this deployment)"
  head -c 200 /tmp/m6_login.json; echo
  exit 3
fi
echo "token acquired: length=${#TOKEN}"

AUTH="Authorization: Bearer $TOKEN"

echo "== /auth/me =="
curl -s -o /tmp/m6_me.json -w 'HTTP %{http_code}\n' --max-time 30 \
  -H "$AUTH" "$U/auth/me"
/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
d = json.load(open("/tmp/m6_me.json"))
if isinstance(d, dict) and "email" in d:
    d["email"] = d["email"][:3] + "***@example.com"
print(json.dumps(d, ensure_ascii=False)[:300])

# The resume payload wants employee_id aligned with the authenticated user so
# the executed tool resolves against the same identity that approved it.
emp = d.get("employee_id") or ""
open("/tmp/m6_emp.txt", "w").write(emp)
print("employee_id captured:", emp)
PY
EMP=$(cat /tmp/m6_emp.txt)
export EMP

echo "== FLOW: HITL approval (served /agent/chat) =="
SID="m6-$STAMP"
curl -s -o /tmp/m6_chat.json -w 'HTTP %{http_code}  %{time_total}s\n' --max-time 60 \
  -X POST "$U/agent/chat" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"question\":\"Tôi muốn xin nghỉ 2 ngày từ 15/09\",\"session_id\":\"$SID\"}"
/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
d = json.load(open("/tmp/m6_chat.json"))
keys = ("status", "answer", "citations", "pending_action")
print(json.dumps({k: d.get(k) for k in keys if k in d}, ensure_ascii=False)[:500])
print("has_pending_action:", "pending_action" in d)
PY

echo "== FLOW: HITL approve via LangGraph resume =="
# /actions/confirm belongs to the LEGACY EnterpriseAgent/session_store path,
# not the LangGraph thread. The served LangGraph resume is POST /agent/chat
# with {"resume": {...}} on the SAME session_id, which rebuilds the thread
# identity thread_id="{tenant_id}:{session_id}" (api.py:1473-1476) and invokes
# Command(resume=...) against the checkpoint the interrupt left behind.
SID="m6-$STAMP"

# The resume-only request still must carry a `question` field: the request model
# requires it (deploy returned 422 "question Field required" without it). The
# value is ignored on the resume branch -- api.py invokes Command(resume=...)
# and never reads init_state -- so a short placeholder is honest, not a hack.
QMSK="resume"

curl -s -o /tmp/m6_confirm.json -w 'resume HTTP %{http_code}  %{time_total}s\n' --max-time 60 \
  -X POST "$U/agent/chat" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"question\":\"$QMSK\",\"session_id\":\"$SID\",\"resume\":{\"approved\":true,\"employee_id\":\"$EMP\"}}"
/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
raw = open("/tmp/m6_confirm.json").read()
try:
    d = json.loads(raw)
    print(json.dumps({k: d.get(k) for k in ("status", "action_result") if k in d},
                     ensure_ascii=False)[:400])
    # The resume path carries the tool result inside action_result.result.ok,
    # not as a top-level key -- surface it explicitly so a silent false-success
    # cannot look green.
    ar = d.get("action_result") or {}
    inner = ar.get("result") if isinstance(ar, dict) else None
    print("tool ok:", (inner or {}).get("ok") if isinstance(inner, dict) else None)
    # action_failed vs action_completed is the field that discriminates.
    print("error field:", d.get("error") or ar.get("error"))
except Exception:
    print("raw:", raw[:200])
PY

echo "== FLOW: idempotency guard (memory backend, same request ID) =="
# For the second resume we send the SAME resume payload over the SAME session.
# A thread whose interrupt was already consumed has no pending interrupt, so a
# replay-safe implementation must refuse to mint a NEW approval and a NEW
# idempotency operation. The observable outcome here: status must NOT come
# back as a fresh action_completed with a second request_id.

curl -s -o /tmp/m6_confirm2.json -w 'replay HTTP %{http_code}  %{time_total}s\n' --max-time 60 \
  -X POST "$U/agent/chat" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"question\":\"$QMSK\",\"session_id\":\"$SID\",\"resume\":{\"approved\":true,\"employee_id\":\"$EMP\"}}"
/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
first = json.load(open("/tmp/m6_confirm.json"))
second = json.load(open("/tmp/m6_confirm2.json"))
f1 = (first.get("action_result") or {}).get("result", {}) if isinstance(first.get("action_result"), dict) else {}
f2 = (second.get("action_result") or {}).get("result", {}) if isinstance(second.get("action_result"), dict) else {}
print("first :", first.get("status"), "| request_id:", f1.get("request_id"), "| op:", (f1.get("operation_id") or "")[:20])
print("second:", second.get("status"), "| request_id:", f2.get("request_id"), "| op:", (f2.get("operation_id") or "")[:20])
# A second distinct request_id means the replay executed the tool again.
print("duplicate_side_effect:", bool(f1.get("request_id") and f2.get("request_id") and f1.get("request_id") != f2.get("request_id")))
PY

echo "== FLOW: RAG grounded query =="
curl -s -o /tmp/m6_rag.json -w 'HTTP %{http_code}  %{time_total}s\n' --max-time 60 \
  -X POST "$U/agent/chat" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"question\":\"Chính sách nghỉ phép của công ty là gì?\",\"session_id\":\"m6-rag-$STAMP\"}"
/Users/mainguyenbinhtan/Downloads/Projects/MAIA/.venv/bin/python - <<'PY'
import json
d = json.load(open("/tmp/m6_rag.json"))
print("status  :", d.get("status"))
ans = d.get("answer") or ""
print("answer  :", ans[:180].replace("\n", " "))
print("citations:", len(d.get("citations") or []))
PY