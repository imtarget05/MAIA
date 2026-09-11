#!/usr/bin/env bash
# =============================================================================
# MAIA — Dual-service entrypoint script
# =============================================================================
# Starts either the FastAPI backend or the Streamlit frontend based on the
# SERVICE environment variable:
#
#   SERVICE=api  → uvicorn maia.api:app (default)
#   SERVICE=ui   → streamlit run app_streamlit.py
#
# Usage:
#   docker run -e SERVICE=api maia:latest
#   docker run -e SERVICE=ui  maia:latest
#   docker run                 maia:latest   (defaults to api)
# =============================================================================

set -euo pipefail

# Resolve service mode (default: api)
SERVICE="${SERVICE:-api}"
SERVICE="$(echo "${SERVICE}" | tr '[:upper:]' '[:lower:]')"

echo "============================================"
echo " MAIA starting in mode: ${SERVICE}"
echo "============================================"

case "${SERVICE}" in
  api)
    echo "→ Starting FastAPI backend on 0.0.0.0:8000"
    exec uvicorn maia.api:app \
      --host 0.0.0.0 \
      --port 8000 \
      --log-level info \
      --access-log
    ;;
  ui)
    echo "→ Starting Streamlit frontend on 0.0.0.0:8501"
    exec streamlit run app_streamlit.py \
      --server.address 0.0.0.0 \
      --server.port 8501 \
      --server.headless true
    ;;
  *)
    echo "ERROR: Unknown SERVICE='${SERVICE}'. Must be 'api' or 'ui'." >&2
    exit 1
    ;;
esac
