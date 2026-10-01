# =============================================================================
# MAIA — Intelligent RAG Knowledge Platform
# Production Dockerfile (dual-service: FastAPI + Streamlit)
# =============================================================================
# Base image: Python 3.12 slim (production-optimized, minimal attack surface)
# =============================================================================
FROM python:3.12-slim AS builder

# Build-time metadata
LABEL maintainer="MAIA Team"
LABEL description="MAIA RAG Knowledge Platform — FastAPI backend + Streamlit frontend"
LABEL version="1.0"

# =============================================================================
# Builder stage: install deps into /install (dropped privileges later)
# =============================================================================
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# pip --prefix can silently skip deps it sees in the build env (e.g. packaging,
# which pip itself vendors). Force them into /install and verify imports resolve
# against the prefix ALONE (python -S ignores system site-packages).
RUN pip install --no-cache-dir --prefix=/install --ignore-installed packaging \
    && PYTHONPATH=/install/lib/python3.12/site-packages python -S -c \
       "import packaging, langchain_core.runnables, fastapi, uvicorn, qdrant_client; print('builder import check ok')"

# =============================================================================
# Runtime stage: copy only installed deps + app code (no pip cache, no build ctx)
# =============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

COPY --from=builder /install /usr/local

# =============================================================================
# Copy application code
# =============================================================================
# Copy source code
COPY src/ /app/src/

# Copy Streamlit frontend entry point
COPY app_streamlit.py /app/

# Copy sample data (read-only documentation corpus)
COPY data/ /app/data/

# =============================================================================
# Create runtime directories
# =============================================================================
# /tmp/storage: ephemeral caches (e.g., agent checkpoints on the container's
# ephemeral disk; first observed on the legacy Render free tier)
RUN mkdir -p /tmp/storage \
    && chmod 777 /tmp/storage

# =============================================================================
# Create non-root user for security (production best practice)
# =============================================================================
RUN chown -R appuser:appuser /app /tmp/storage

USER appuser

# =============================================================================
# Expose service ports
# =============================================================================
# 8000 — FastAPI backend
# 8501 — Streamlit frontend
EXPOSE 8000 8501

# =============================================================================
# Healthcheck
# =============================================================================
# For API: checks /health endpoint
# For UI:  checks if port 8501 is listening (using python socket check)
# Note: HEALTHCHECK in a dual-service image checks the default SERVICE=api.
# When running UI, override with HEALTHCHECK NONE in compose or rely on
# container-level liveness from the orchestrator.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket, urllib.request; (socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('127.0.0.1', 8501)) if os.environ.get('SERVICE', 'api').lower() == 'ui' else urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2))"

# =============================================================================
# Entrypoint script (multi-service via SERVICE env var)
# =============================================================================
# SERVICE=api  → uvicorn FastAPI
# SERVICE=ui   → streamlit frontend
# (unset)      → defaults to api
# =============================================================================
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
