# NCMS Hub — shared cognitive memory + Knowledge Bus for NemoClaw multi-sandbox
#
# No OpenShell, no NemoClaw, no Node.js — just NCMS with HTTP API + Dashboard.
# Three agent sandboxes connect via HTTP + SSE.
#
# Build: docker build -f hub.Dockerfile -t ncms-hub ../..
# Run:   docker compose -f docker-compose.nemoclaw.yaml up

# ── Stage 1: Base + Dependencies ─────────────────────────────────────────
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --frozen --extra dashboard --extra docs && \
    rm -rf /root/.cache

# ── Stage 2: Pre-download Models ─────────────────────────────────────────
FROM base AS models

# GLiNER entity extraction (~209 MB)
RUN uv run python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"

# Cross-encoder reranker (~80 MB)
RUN uv run python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# SPLADE v3 (~500 MB, gated — requires HF_TOKEN)
ARG HF_TOKEN=""
RUN if [ -n "$HF_TOKEN" ]; then \
    HF_TOKEN=$HF_TOKEN uv run python -c \
      "from sentence_transformers import SparseEncoder; SparseEncoder('naver/splade-v3')"; \
    fi

# ── Stage 3: Final ───────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy installed environment
COPY --from=base /app /app

# Copy cached models
COPY --from=models /root/.cache/huggingface /root/.cache/huggingface

# ── Intent-slot adapters (the P2 five-head SLM, per-domain) ───────────────
# Adapters ship as compact artifacts (~2.4 MB each × 4 = ~10 MB).  We copy
# the authoritative ``v4`` (conversational / clinical / software_dev) and
# ``v1`` (swe_diff — trained on SWE-Gym for diff-aware state_change) into
# the runtime path NCMS looks up via
# ``~/.ncms/adapters/<domain>/<version>/``.
# At container start, ``NCMS_INTENT_SLOT_ENABLED=true`` + the domain hint
# resolves to the matching adapter directory inside the image.
COPY experiments/intent_slot_distillation/adapters/conversational/v4 \
     /root/.ncms/adapters/conversational/v4
COPY experiments/intent_slot_distillation/adapters/clinical/v4 \
     /root/.ncms/adapters/clinical/v4
COPY experiments/intent_slot_distillation/adapters/software_dev/v4 \
     /root/.ncms/adapters/software_dev/v4
COPY experiments/intent_slot_distillation/adapters/swe_diff/v1 \
     /root/.ncms/adapters/swe_diff/v1

# Knowledge is loaded by agent sandboxes on startup (not the hub)

# Copy entrypoint
COPY deployment/nemoclaw-blueprint/entrypoint-hub.sh /app/entrypoint-hub.sh
RUN chmod +x /app/entrypoint-hub.sh

# Create data directories
RUN mkdir -p /app/data

# Environment defaults — Production bundle (Phases 1-8 + P2 SLM)
ENV NCMS_DB_PATH=/app/data/ncms.db \
    NCMS_INDEX_PATH=/app/data/index \
    NCMS_SPLADE_ENABLED=true \
    NCMS_EPISODES_ENABLED=true \
    NCMS_INTENT_CLASSIFICATION_ENABLED=true \
    NCMS_RERANKER_ENABLED=true \
    NCMS_ADMISSION_ENABLED=true \
    NCMS_RECONCILIATION_ENABLED=true \
    NCMS_CONTENT_CLASSIFICATION_ENABLED=true \
    NCMS_CONTRADICTION_DETECTION_ENABLED=true \
    NCMS_TEMPORAL_ENABLED=true \
    NCMS_MAINTENANCE_ENABLED=true \
    NCMS_SEARCH_FEEDBACK_ENABLED=true \
    NCMS_CONSOLIDATION_KNOWLEDGE_ENABLED=true \
    NCMS_EPISODE_CONSOLIDATION_ENABLED=true \
    NCMS_TRAJECTORY_CONSOLIDATION_ENABLED=true \
    NCMS_PATTERN_CONSOLIDATION_ENABLED=true \
    NCMS_DREAM_CYCLE_ENABLED=true \
    NCMS_BULK_IMPORT_QUEUE_SIZE=10000 \
    NCMS_MODEL_CACHE_DIR=/root/.cache/huggingface \
    NCMS_INTENT_SLOT_ENABLED=true \
    NCMS_INTENT_SLOT_POPULATE_DOMAINS=true

EXPOSE 9080 8420

HEALTHCHECK --interval=30s --timeout=10s --retries=5 \
    CMD curl -f http://localhost:${NCMS_HUB_PORT:-9080}/api/v1/health || exit 1

ENTRYPOINT ["/app/entrypoint-hub.sh"]
