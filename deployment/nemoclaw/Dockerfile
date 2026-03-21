FROM ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest

# Install NCMS
RUN pip install ncms

# Pre-download models to avoid runtime network calls inside sandbox
RUN python -c "from gliner import GLiNER; GLiNER.from_pretrained('urchade/gliner_medium-v2.1')"
RUN python -c "from sentence_transformers import SparseEncoder; SparseEncoder('naver/splade-v3')"
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Copy skill file
COPY skills/ncms-memory/ /sandbox/skills/ncms-memory/

# Copy OpenClaw config
COPY openclaw.json /sandbox/.openclaw/openclaw.json
