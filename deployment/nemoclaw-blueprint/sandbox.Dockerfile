# NCMS Agent Sandbox — OpenClaw + NCMS Bus Agent sidecar
#
# Each sandbox runs ONE agent persona (architect, security, or builder).
# OpenClaw handles the TUI / agent runtime (chat + skill execution).
# The bus sidecar maintains SSE connection to NCMS Hub for real-time bus events.
#
# Build: docker build -f sandbox.Dockerfile -t ncms-openclaw ../..
# Run:   docker compose -f docker-compose.nemoclaw.yaml up

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl supervisor && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install NCMS (for bus-agent sidecar)
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev && rm -rf /root/.cache

# Copy skills (mounted per-agent via compose volumes, but include all as default)
COPY deployment/nemoclaw-blueprint/skills/ /app/skills/

# Copy sandbox entrypoint
COPY deployment/nemoclaw-blueprint/entrypoint-sandbox.sh /app/entrypoint-sandbox.sh
RUN chmod +x /app/entrypoint-sandbox.sh

# Copy supervisor config
COPY deployment/nemoclaw-blueprint/supervisord-sandbox.conf /etc/supervisor/conf.d/sandbox.conf

# Environment (overridden by compose)
ENV NCMS_HUB_URL=http://ncms-hub:8080 \
    NCMS_AGENT_ID=agent \
    NCMS_AGENT_DOMAINS=general

ENTRYPOINT ["/app/entrypoint-sandbox.sh"]
