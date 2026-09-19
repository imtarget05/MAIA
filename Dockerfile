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
# Environment variables
# =============================================================================
# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

# =============================================================================
# Working directory
# =============================================================================
WORKDIR /app

# =============================================================================
# Install system dependencies (if any) — keep minimal for slim image
# =============================================================================
# Add any required system packages here (e.g., libgomp1 for ONNX runtime).
# Currently none required beyond what python:3.12-slim provides.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# Install Python dependencies (layer caching: copy requirements first)
# =============================================================================
COPY requirements.txt .

# Pin pip/setuptools/wheel to stable versions for reproducible builds
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && pip cache purge

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
# /tmp/storage: ephemeral caches (e.g., agent checkpoints on Render ephemeral disk)
RUN mkdir -p /tmp/storage \
    && chmod 777 /tmp/storage

# =============================================================================
# Create non-root user for security (production best practice)
# =============================================================================
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app \
    && chown -R appuser:appuser /tmp/storage

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
    CMD python -c "
import os, sys, urllib.request
service = os.environ.get('SERVICE', 'api').lower()
if service == 'ui':
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(2)
        s.connect(('127.0.0.1', 8501))
        sys.exit(0)
    except Exception:
        sys.exit(1)
    finally:
        s.close()
else:
    try:
        urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)
        sys.exit(0)
    except Exception:
        sys.exit(1)
"

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
