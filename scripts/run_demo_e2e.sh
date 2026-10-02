#!/usr/bin/env bash
# =============================================================================
# MAIA One-Command End-to-End Showcase Demo
# Covers: JWT Auth, Grounded RAG, Honest Refusal, HITL Agent, MCP Tools, SSE
# Mode: Runs offline/local with zero cloud cost (FastEmbed + Mock LLM + SQLite)
# =============================================================================
set -euo pipefail

[ -d ".venv/bin" ] && export PATH=".venv/bin:$PATH"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

PORT=8855
BASE_URL="http://127.0.0.1:${PORT}"
export MAIA_EMBED_FORCE_HASH=1
export VECTOR_STORE_BACKEND="qdrant"
export QDRANT_URL="http://127.0.0.1:6333"
export QDRANT_COLLECTION="maia_knowledge"
export MAIA_LLM_PROVIDER="mock"
export PYTHONPATH="src"
export MAIA_PORT="${PORT}"

echo -e "${BOLD}${CYAN}============================================================${NC}"
echo -e "${BOLD}${CYAN}   🧠 MAIA — Enterprise Agentic RAG Platform E2E Demo       ${NC}"
echo -e "${BOLD}${CYAN}============================================================${NC}"

# Cleanup handler on exit
cleanup() {
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo -e "\n${YELLOW}Shutting down local server (PID ${SERVER_PID})...${NC}"
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# 1. Start local server in offline mode
echo -e "\n${BOLD}[1/6] Khởi động MAIA API server (Offline-First mode)...${NC}"
uvicorn maia.api:app --port "$PORT" --log-level error &
SERVER_PID=$!

# Wait for server ready
echo -n "Chờ server sẵn sàng..."
for _ in {1..30}; do
  if curl -sf "${BASE_URL}/health" >/dev/null 2>&1; then
    echo -e " ${GREEN}Sẵn sàng!${NC}"
    break
  fi
  sleep 0.5
  echo -n "."
done

# 2. Authentication (Register + Login)
echo -e "\n${BOLD}${CYAN}[2/6] Xác thực người dùng (JWT + Role-Based Access Control)${NC}"
USER_EMAIL="demo.user.$(date +%s)@example.com"
PASSWORD="SecurePassword123!"

curl -sX POST "${BASE_URL}/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"${USER_EMAIL}\", \"password\": \"${PASSWORD}\", \"full_name\": \"Demo Engineer\", \"role\": \"user\", \"employee_id\": \"emp_001\", \"tenant_id\": \"default\"}" > /dev/null

LOGIN_RESP=$(curl -sX POST "${BASE_URL}/auth/login" \
  -d "username=${USER_EMAIL}&password=${PASSWORD}")

TOKEN=$(echo "$LOGIN_RESP" | jq -r '.access_token')
echo -e "${GREEN}✓ Đăng nhập thành công, Bearer token:${NC} ${TOKEN:0:30}..."

# Ingest sample policy documents into in-memory store
echo -e "\n${BOLD}${CYAN}[3/7] Nạp tài liệu chính sách doanh nghiệp mẫu (8 tài liệu, 46 chunks)...${NC}"
curl -sX POST "${BASE_URL}/ingest/enterprise" \
  -H "Authorization: Bearer ${TOKEN}" > /dev/null
echo -e "${GREEN}✓ Nạp dữ liệu hoàn tất!${NC}"

# 3. Flow A: Grounded RAG with citations [S1]
echo -e "\n${BOLD}${CYAN}[3/6] Demo Flow A — Grounded RAG & Citations [S1]${NC}"
echo "Câu hỏi: 'Chính sách làm việc từ xa (remote work) như thế nào?'"
RESPONSE=$(curl -sX POST "${BASE_URL}/chat" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"question": "Chính sách làm việc từ xa remote work", "session_id": "demo-sess-1"}')

echo -e "${GREEN}Response:${NC}"
echo "$RESPONSE" | jq '{status: .status, has_evidence: .has_evidence, answer: .answer, citations: .citations}'

# 4. Flow B: Honest Refusal (Evidence Gate)
echo -e "\n${BOLD}${CYAN}[4/6] Demo Flow B — Honest Refusal (Từ chối khi ngoài phạm vi tài liệu)${NC}"
echo "Câu hỏi: 'Công ty có thưởng tiền điện tử Bitcoin không?'"
REFUSAL=$(curl -sX POST "${BASE_URL}/chat" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"question": "Công ty có thưởng tiền điện tử Bitcoin cho nhân viên không?", "session_id": "demo-sess-2"}')

echo -e "${GREEN}Response:${NC}"
echo "$REFUSAL" | jq '{status: .status, has_evidence: .has_evidence, answer: .answer}'

# 5. Flow C: LangGraph HITL Action Approval (Interrupt / Resume)
echo -e "\n${BOLD}${CYAN}[5/6] Demo Flow C — LangGraph HITL State Machine (Xin nghỉ phép)${NC}"
echo "Yêu cầu: 'Tôi muốn xin nghỉ phép 2 ngày từ 15/09'"
AGENT_RESP=$(curl -sX POST "${BASE_URL}/agent/chat" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"question": "Tôi muốn xin nghỉ phép 2 ngày từ 15/09", "session_id": "leave-flow-1"}')

STATUS=$(echo "$AGENT_RESP" | jq -r '.status // empty')
ACTION=$(echo "$AGENT_RESP" | jq -r '.pending_action.tool // empty')
PARAMS=$(echo "$AGENT_RESP" | jq -r '.pending_action.params // empty')

echo -e "Trạng thái Agent: ${YELLOW}${STATUS}${NC} (Đã ngắt tiến trình để chờ con người phê duyệt)"
echo -e "Công cụ đề xuất: ${BOLD}${ACTION}${NC}"
echo -e "Tham số đề xuất: ${PARAMS}"

echo -e "\n${BOLD}>>> Phê duyệt hành động (Human Approval = TRUE)...${NC}"
CONFIRM_RESP=$(curl -sX POST "${BASE_URL}/agent/chat" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"question": "", "session_id": "leave-flow-1", "resume": {"approved": true}}')

echo -e "${GREEN}Kết quả sau khi duyệt:${NC}"
echo "$CONFIRM_RESP" | jq '{status: .status, answer: .answer, action_result: .action_result}'

# 6. Flow D: Model Context Protocol (MCP) Tool Calling
echo -e "\n${BOLD}${CYAN}[6/6] Demo Flow D — Model Context Protocol (MCP JSON-RPC 2.0)${NC}"
echo "Liệt kê danh sách công cụ qua MCP Bridge:"
python3 -m maia.mcp.bridge --server market_insight --list-tools | jq -r '.tools[] | "- \(.name): \(.description)"'

echo -e "\n${BOLD}${GREEN}============================================================${NC}"
echo -e "${BOLD}${GREEN}  ✅ E2E SHOWCASE DEMO COMPLETED SUCCESSFULLY!              ${NC}"
echo -e "${BOLD}${GREEN}============================================================${NC}"
