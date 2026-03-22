# NCMS NemoClaw Blueprint Container
#
# Multi-stage build: base → models → nemoclaw → final
# Installs NemoClaw + OpenShell alongside NCMS for full blueprint orchestration.
#
# Build (basic — Docker fallback, no OpenShell):
#   docker build -f deployment/nemoclaw-blueprint/Dockerfile -t ncms-nemoclaw:latest .
#
# Build with SPLADE (gated model, needs HuggingFace token):
#   docker build -f deployment/nemoclaw-blueprint/Dockerfile \
#     --build-arg HF_TOKEN=hf_xxxx -t ncms-nemoclaw:latest .
#
# Run:
#   docker run -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest
#   docker run -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest shell
#
# Inside the container, NemoClaw + OpenShell are on PATH:
#   nemoclaw onboard
#   openshell term
#   openclaw tui

# ── Stage 1: Base (Python + NCMS) ─────────────────────────────────────────
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy NCMS source and install
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --extra dashboard --extra docs

# ── Stage 2: Model Downloads ──────────────────────────────────────────────
FROM base AS models

# GLiNER entity extraction (~209 MB) — public
RUN uv run python -c "\
from gliner import GLiNER; \
GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"

# Cross-encoder reranker (~80 MB) — public
RUN uv run python -c "\
from sentence_transformers import CrossEncoder; \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# SPLADE v3 (~500 MB) — gated, optional
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

# ── Stage 3: NemoClaw + OpenShell ─────────────────────────────────────────
FROM base AS nemoclaw

# Install Node.js 22 (required by NemoClaw)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install OpenShell binary from NVIDIA/OpenShell releases
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
      x86_64|amd64) ASSET="openshell-x86_64-unknown-linux-musl.tar.gz" ;; \
      aarch64|arm64) ASSET="openshell-aarch64-unknown-linux-musl.tar.gz" ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/NVIDIA/OpenShell/releases/latest/download/$ASSET" \
      -o /tmp/openshell.tar.gz && \
    tar xzf /tmp/openshell.tar.gz -C /tmp && \
    install -m 755 /tmp/openshell /usr/local/bin/openshell && \
    rm -f /tmp/openshell.tar.gz /tmp/openshell && \
    openshell --version || echo "openshell installed"

# Install NemoClaw from source
# Entry point is bin/nemoclaw.js (no build step needed)
RUN git clone --depth 1 https://github.com/NVIDIA/NemoClaw.git /tmp/nemoclaw && \
    cd /tmp/nemoclaw && \
    npm install && \
    ln -sf /tmp/nemoclaw/bin/nemoclaw.js /usr/local/bin/nemoclaw && \
    chmod +x /tmp/nemoclaw/bin/nemoclaw.js

# ── Stage 4: Final Image ──────────────────────────────────────────────────
FROM base AS final

# Install Node.js 22 runtime (for NemoClaw CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Copy OpenShell binary from build stage
COPY --from=nemoclaw /usr/local/bin/openshell /usr/local/bin/openshell

# Copy NemoClaw from build stage
COPY --from=nemoclaw /tmp/nemoclaw /opt/nemoclaw
RUN ln -sf /opt/nemoclaw/bin/nemoclaw.js /usr/local/bin/nemoclaw && \
    chmod +x /opt/nemoclaw/bin/nemoclaw.js

# Copy cached models from download stage
COPY --from=models /root/.cache/huggingface /root/.cache/huggingface

# Copy NCMS source (fresh copy for final image)
COPY src/ src/

# Copy blueprint skills and policies
COPY deployment/nemoclaw-blueprint/skills/ /app/skills/
COPY deployment/nemoclaw-blueprint/policies/ /app/policies/
COPY deployment/nemoclaw-blueprint/blueprint.yaml /app/blueprint.yaml

# Copy governance-mesh knowledge files into container
# These are the real architecture/security files the ND agents reason over
COPY deployment/nemoclaw-blueprint/knowledge/ /app/knowledge/

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
    NCMS_DREAM_CYCLE_ENABLED=false \
    NEMOCLAW_BLUEPRINT_PATH=/app

EXPOSE 8420 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/v1/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
