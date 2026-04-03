# NCMS NemoClaw Blueprint

NemoClaw Blueprint for NCMS Cognitive Memory System — deploys NCMS + OpenClaw agent skills inside an OpenShell sandbox with policy-enforced security.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  ncms-nemoclaw Container                             │
│                                                      │
│  ┌──────────────┐  ┌─────────────────────────────┐   │
│  │ NCMS MCP     │  │ NemoClaw + OpenClaw          │   │
│  │ Server :8080 │◄─┤                              │   │
│  │ (18 tools)   │  │  Skill: Architect (CALM/ADR) │   │
│  │              │  │  Skill: Security (STRIDE)    │   │
│  │ Dashboard    │  │  Skill: Builder (work loop)  │   │
│  │ :8420        │  │                              │   │
│  └──────────────┘  │  LLM → inference endpoint    │   │
│                    └─────────────────────────────┘   │
│                                                      │
│  NemoClaw CLI + OpenShell on PATH                    │
└──────────────────────────────────────────────────────┘
```

## Quick Start

### Option A: Docker (fallback, no OpenShell required)

```bash
# Build (from project root)
docker build -f deployment/nemoclaw-blueprint/Dockerfile \
  -t ncms-nemoclaw:latest .

# Build with SPLADE (gated model)
docker build -f deployment/nemoclaw-blueprint/Dockerfile \
  --build-arg HF_TOKEN=hf_xxxx -t ncms-nemoclaw:latest .

# Run — DGX Spark
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  -e NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1 \
  ncms-nemoclaw:latest

# Run — Ollama on host
docker run -p 8420:8420 -p 8080:8080 \
  -e NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b \
  ncms-nemoclaw:latest

# Dashboard: http://localhost:8420
# MCP API:   http://localhost:8080
```

### Option B: Blueprint Runner

The runner auto-detects OpenShell. If available, it uses real NemoClaw commands (`openshell sandbox create`, `openshell provider create`, `openshell inference set`). Without OpenShell, it falls back to Docker.

```bash
cd deployment/nemoclaw-blueprint

# Preview deployment plan
python orchestrator/runner.py plan --profile default

# Build + deploy
python orchestrator/runner.py apply --profile default

# Check status
python orchestrator/runner.py status

# Switch profile
python orchestrator/runner.py apply --profile ollama

# Rollback
python orchestrator/runner.py rollback --run-id <id>
```

### Option C: Inside the Container

```bash
docker run -it -p 8420:8420 -p 8080:8080 ncms-nemoclaw:latest shell

# Inside:
nemoclaw onboard               # NemoClaw setup wizard
openclaw tui                   # OpenClaw chat interface
openclaw agent --agent main --local -m "What ADRs exist?" --session-id test
uv run ncms demo --nemoclaw-nd # Run ND agent demo
```

## Container Modes

| Mode | Command | Description |
|------|---------|-------------|
| `serve` | Default | MCP HTTP API (:8080) + Dashboard (:8420) |
| `demo` | `docker run ... demo` | NemoClaw ND autonomous agent demo |
| `mcp` | `docker run ... mcp` | MCP stdio server (for OpenClaw piping) |
| `blueprint` | `docker run ... blueprint plan` | Blueprint Runner inside container |
| `shell` | `docker run -it ... shell` | Interactive bash with all tools |

## Inference Profiles

| Profile | Provider | Model | Active Params | Endpoint |
|---------|----------|-------|--------------|----------|
| `nano` | DGX Spark | Nemotron 3 Nano 30B | 3B | `spark-ee7d.local:8000` |
| `super` | DGX Spark | Nemotron 3 Super 120B | 12B | `spark-ee7d.local:8000` |
| `ollama` | Ollama | Qwen 3.5 35B MoE | 3B | `host.docker.internal:11434` |

### DGX Spark Deployment Commands

**Nemotron 3 Nano 30B (3B active, FP16):**

```bash
# Download reasoning parser
sudo wget -O /root/nano_v3_reasoning_parser.py \
  https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py

# Deploy
sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-nano \
  -p 8000:8000 \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  -v /root/nano_v3_reasoning_parser.py:/app/nano_v3_reasoning_parser.py \
  nvcr.io/nvidia/vllm:26.01-py3 \
  vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --max-model-len 524288 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --reasoning-parser-plugin /app/nano_v3_reasoning_parser.py \
    --reasoning-parser nano_v3
```

**Nemotron 3 Super 120B (12B active, NVFP4):**

```bash
# Download reasoning parser
wget https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4/raw/main/super_v3_reasoning_parser.py

# Deploy
sudo docker run -d --gpus all --ipc=host --restart unless-stopped \
  --name vllm-nemotron-super \
  -e VLLM_NVFP4_GEMM_BACKEND=marlin \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -e HF_TOKEN=$HF_TOKEN \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v $(pwd)/super_v3_reasoning_parser.py:/app/super_v3_reasoning_parser.py \
  -p 8000:8000 \
  vllm/vllm-openai:cu130-nightly \
    --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 \
    --served-model-name nemotron-3-super \
    --host 0.0.0.0 --port 8000 \
    --dtype auto \
    --kv-cache-dtype fp8 \
    --trust-remote-code \
    --gpu-memory-utilization 0.90 \
    --max-model-len 524288 \
    --max-num-seqs 4 \
    --quantization fp4 \
    --moe-backend marlin \
    --mamba_ssm_cache_dtype float32 \
    --enable-chunked-prefill \
    --speculative_config '{"method":"mtp","num_speculative_tokens":3,"moe_backend":"triton"}' \
    --reasoning-parser-plugin /app/super_v3_reasoning_parser.py \
    --reasoning-parser super_v3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
```

**Switch between models:** Only one can run on port 8000 at a time.
```bash
sudo docker stop vllm-nemotron-nano   # or vllm-nemotron-super
sudo docker start vllm-nemotron-super  # or vllm-nemotron-nano
```

## Skills

Three OpenClaw agent skills:

| Skill | File | Expertise |
|-------|------|-----------|
| Architect | `skills/architect/architect.md` | CALM model, ADRs, quality attributes, fitness functions |
| Security | `skills/security/security.md` | STRIDE threats, OWASP Top 10, NIST controls, compliance |
| Builder | `skills/builder/builder.md` | Drives design of imdb-identity-service via Knowledge Bus |

Each skill uses NCMS MCP tools: `recall_memory`, `store_memory`, `search_memory`, `ask_knowledge_sync`, `announce_knowledge`.

## Security Policy

`policies/ncms-sandbox.yaml` enforces:
- **Filesystem**: Read-only source dirs, read-write only `/app/data` and `/tmp`
- **Network**: Allowlist for LLM endpoints (Spark, Ollama, NIM, HuggingFace) — all other egress denied

## Runner Protocol

The Blueprint Runner follows the NemoClaw protocol:
- `PROGRESS:<0-100>:<label>` — progress updates on stdout
- `RUN_ID:<id>` — run identifier
- Exit code 0 = success
- State persisted to `~/.nemoclaw/state/runs/<run-id>/plan.json`
