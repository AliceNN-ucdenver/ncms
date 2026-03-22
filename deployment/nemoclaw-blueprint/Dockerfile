# NCMS NemoClaw Blueprint Container
#
# Multi-stage build: base -> models -> final
# Runs both MCP server (HTTP) and Dashboard concurrently.
#
# Build:
#   docker build -f deployment/nemoclaw-blueprint/Dockerfile -t ncms-nemoclaw:latest .
#
# Build with SPLADE (requires HuggingFace token):
#   docker build -f deployment/nemoclaw-blueprint/Dockerfile \
#     --build-arg HF_TOKEN=hf_xxxx -t ncms-nemoclaw:latest .
#
# Run:
#   docker run -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest

# ── Stage 1: Base ────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy NCMS source and install
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --extra docs --extra dashboard

# ── Stage 2: Model Downloads ────────────────────────────────────────────
FROM base AS models

# GLiNER entity extraction model (~209 MB) — public
RUN uv run python -c "\
from gliner import GLiNER; \
GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"

# Cross-encoder reranker model (~80 MB) — public
RUN uv run python -c "\
from sentence_transformers import CrossEncoder; \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# SPLADE v3 sparse neural model (~500 MB) — gated, optional
ARG HF_TOKEN=""
RUN --mount=type=secret,id=env,required=false \
    export HF_TOKEN="${HF_TOKEN}" && \
    if [ -f /run/secrets/env ]; then . /run/secrets/env; fi && \
    if [ -n "$HF_TOKEN" ]; then \
        uv run python -c "\
import os; \
from huggingface_hub import login; \
login(token=os.environ['HF_TOKEN']); \
from sentence_transformers import SparseEncoder; \
SparseEncoder('naver/splade-v3')"; \
    else \
        echo "Skipping SPLADE download (no HF_TOKEN provided)"; \
    fi

# ── Stage 3: Final Image ────────────────────────────────────────────────
FROM base AS final

# Copy cached models from download stage
COPY --from=models /root/.cache/huggingface /root/.cache/huggingface

# Copy NCMS source (fresh copy for final image)
COPY src/ src/

# Copy skills into container
COPY deployment/nemoclaw-blueprint/skills/ /app/skills/

# Create data directories
RUN mkdir -p /app/data/.ncms /app/data/index

# Copy entrypoint
COPY deployment/nemoclaw-blueprint/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Environment defaults
ENV NCMS_DB_PATH=/app/data/.ncms/ncms.db \
    NCMS_INDEX_PATH=/app/data/index \
    NCMS_SPLADE_ENABLED=true \
    NCMS_EPISODES_ENABLED=true \
    NCMS_INTENT_CLASSIFICATION_ENABLED=true \
    NCMS_RERANKER_ENABLED=true \
    NCMS_ADMISSION_ENABLED=true \
    NCMS_RECONCILIATION_ENABLED=true \
    NCMS_DREAM_CYCLE_ENABLED=false

EXPOSE 8420 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/v1/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
