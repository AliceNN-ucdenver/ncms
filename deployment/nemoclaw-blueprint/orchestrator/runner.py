#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""
NemoClaw Blueprint Runner for NCMS

Orchestrates NCMS sandbox lifecycle inside OpenShell (when available)
or falls back to plain Docker. Compatible with the NemoClaw Blueprint
Runner protocol:
  - stdout lines PROGRESS:<0-100>:<label> for progress updates
  - stdout line RUN_ID:<id> for run identifier
  - exit code 0 = success, non-zero = failure

Usage:
  python runner.py plan [--profile default]
  python runner.py apply [--profile default]
  python runner.py status [--run-id <id>]
  python runner.py rollback --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

BLUEPRINT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".nemoclaw" / "state" / "runs"

# Profile name → NCMS env var mapping for Docker fallback
PROFILE_ENV_MAP: dict[str, dict[str, str]] = {
    "default": {
        "NCMS_LLM_MODEL": "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        "NCMS_LLM_API_BASE": "http://spark-ee7d.local:8000/v1",
    },
    "ollama": {
        "NCMS_LLM_MODEL": "ollama_chat/qwen3.5:35b-a3b",
        "NCMS_LLM_API_BASE": "",
    },
    "nim": {
        "NCMS_LLM_MODEL": "openai/nvidia/llama-3.1-nemotron-70b-instruct",
        "NCMS_LLM_API_BASE": "https://integrate.api.nvidia.com/v1",
    },
    "vllm": {
        "NCMS_LLM_MODEL": "openai/nvidia/nemotron-3-nano-30b-a3b",
        "NCMS_LLM_API_BASE": "http://localhost:8000/v1",
    },
}


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def progress(pct: int, label: str) -> None:
    print(f"PROGRESS:{pct}:{label}", flush=True)


def emit_run_id() -> str:
    rid = f"nc-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    print(f"RUN_ID:{rid}", flush=True)
    return rid


# ---------------------------------------------------------------------------
# Infrastructure detection
# ---------------------------------------------------------------------------


def run_cmd(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argv list (never shell=True)."""
    return subprocess.run(args, check=check, capture_output=capture, text=True)  # noqa: S603


def openshell_available() -> bool:
    """Check if openshell CLI is installed."""
    return shutil.which("openshell") is not None


def docker_available() -> bool:
    """Check if Docker CLI is available."""
    return shutil.which("docker") is not None


def container_running(name: str) -> bool:
    result = run_cmd(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        check=False, capture=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def container_exists(name: str) -> bool:
    result = run_cmd(
        ["docker", "inspect", name],
        check=False, capture=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Blueprint loading
# ---------------------------------------------------------------------------


def load_blueprint() -> dict[str, Any]:
    bp_path = Path(os.environ.get("NEMOCLAW_BLUEPRINT_PATH", str(BLUEPRINT_DIR)))
    bp_file = bp_path / "blueprint.yaml"
    if not bp_file.exists():
        log(f"ERROR: blueprint.yaml not found at {bp_file}")
        sys.exit(1)
    with bp_file.open() as f:
        return yaml.safe_load(f)


def resolve_profile(blueprint: dict[str, Any], name: str) -> dict[str, Any]:
    """Resolve a profile from the blueprint components."""
    profiles: dict[str, Any] = (
        blueprint.get("components", {}).get("inference", {}).get("profiles", {})
    )
    if name not in profiles:
        available = ", ".join(profiles.keys())
        log(f"ERROR: Profile '{name}' not found. Available: {available}")
        sys.exit(1)
    cfg = profiles[name]
    cfg["_name"] = name
    return cfg


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def action_plan(
    profile: str,
    blueprint: dict[str, Any],
    *,
    dry_run: bool = False,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Plan: validate blueprint, check prerequisites, show deployment plan."""
    rid = emit_run_id()
    progress(10, "Validating blueprint")

    inf_cfg = resolve_profile(blueprint, profile)
    sandbox = blueprint.get("components", {}).get("sandbox", {})
    sandbox_name = sandbox.get("name", "ncms-openclaw")
    image = sandbox.get("image", "ncms-nemoclaw:latest")
    ports = sandbox.get("forward_ports", [])
    skills = blueprint.get("skills", [])

    endpoint = endpoint_url or inf_cfg.get("endpoint", "")
    model = inf_cfg.get("model", "")

    progress(20, "Checking prerequisites")
    use_openshell = openshell_available()
    if not use_openshell:
        if not docker_available():
            log("ERROR: Neither openshell nor docker found on PATH.")
            sys.exit(1)
        log("INFO: openshell not found — will use Docker fallback")

    plan = {
        "run_id": rid,
        "profile": profile,
        "sandbox_name": sandbox_name,
        "image": image,
        "ports": ports,
        "skills": skills,
        "inference": {
            "provider": inf_cfg.get("provider_type", "openai"),
            "model": model,
            "endpoint": endpoint,
        },
        "backend": "openshell" if use_openshell else "docker",
        "dry_run": dry_run,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    progress(50, "Plan ready")

    log("")
    log("=== NemoClaw Blueprint Plan ===")
    log("")
    log(f"  Profile:    {profile}")
    log(f"  Backend:    {'OpenShell' if use_openshell else 'Docker (fallback)'}")
    log(f"  Image:      {image}")
    log(f"  Sandbox:    {sandbox_name}")
    log(f"  Ports:      {', '.join(str(p) for p in ports)}")
    log(f"  Model:      {model}")
    log(f"  Endpoint:   {endpoint}")
    log("")
    log("  Skills:")
    for s in skills:
        log(f"    - {s}")
    log("")

    if dry_run:
        log("  (dry-run — no changes applied)")

    progress(100, "Plan complete")
    return plan


def action_apply(
    profile: str,
    blueprint: dict[str, Any],
    *,
    plan_path: str | None = None,
    endpoint_url: str | None = None,
) -> None:
    """Apply: create sandbox + configure inference."""
    rid = emit_run_id()
    progress(5, "Loading plan")

    inf_cfg = resolve_profile(blueprint, profile)
    sandbox = blueprint.get("components", {}).get("sandbox", {})
    sandbox_name = sandbox.get("name", "ncms-openclaw")
    image = sandbox.get("image", "ncms-nemoclaw:latest")
    ports = sandbox.get("forward_ports", [])

    endpoint = endpoint_url or inf_cfg.get("endpoint", "")
    model = inf_cfg.get("model", "")
    provider_type = inf_cfg.get("provider_type", "openai")
    provider_name = inf_cfg.get("provider_name", "ncms-inference")
    credential_env = inf_cfg.get("credential_env")
    credential_default: str = inf_cfg.get("credential_default", "")

    use_openshell = openshell_available()

    if use_openshell:
        _apply_openshell(
            rid=rid,
            sandbox_name=sandbox_name,
            image=image,
            ports=ports,
            provider_type=provider_type,
            provider_name=provider_name,
            endpoint=endpoint,
            model=model,
            credential_env=credential_env,
            credential_default=credential_default,
            profile=profile,
            inf_cfg=inf_cfg,
        )
    else:
        _apply_docker(
            rid=rid,
            sandbox_name=sandbox_name,
            image=image,
            ports=ports,
            profile=profile,
            endpoint=endpoint,
            model=model,
        )

    # Save run state
    progress(90, "Saving run state")
    state_dir = STATE_DIR / rid
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "plan.json").write_text(
        json.dumps(
            {
                "run_id": rid,
                "profile": profile,
                "sandbox_name": sandbox_name,
                "backend": "openshell" if use_openshell else "docker",
                "inference": {
                    "provider": provider_type,
                    "model": model,
                    "endpoint": endpoint,
                },
                "timestamp": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
    )

    progress(100, "Apply complete")
    log("")
    log(f"Sandbox '{sandbox_name}' is ready.")
    log("  Dashboard:  http://localhost:8420")
    log("  MCP HTTP:   http://localhost:8080")
    log(f"  Inference:  {provider_name} -> {model} @ {endpoint}")
    log("")


def _apply_openshell(
    *,
    rid: str,
    sandbox_name: str,
    image: str,
    ports: list[int],
    provider_type: str,
    provider_name: str,
    endpoint: str,
    model: str,
    credential_env: str | None,
    credential_default: str,
    profile: str,
    inf_cfg: dict[str, Any],
) -> None:
    """Apply using OpenShell CLI (real NemoClaw path)."""
    # Step 1: Create sandbox
    progress(20, f"Creating sandbox {sandbox_name}")
    create_args = [
        "openshell", "sandbox", "create",
        "--name", sandbox_name,
        "--image", image,
    ]
    for port in ports:
        create_args.extend(["--forward-port", str(port)])
    run_cmd(create_args, check=False, capture=True)

    # Step 2: Configure inference provider
    progress(50, f"Configuring inference provider {provider_name}")
    credential = ""
    if credential_env:
        credential = os.environ.get(credential_env, credential_default)

    provider_args = [
        "openshell", "provider", "create",
        "--name", provider_name,
        "--type", provider_type,
    ]
    if credential:
        provider_args.extend(["--credential", f"OPENAI_API_KEY={credential}"])
    if endpoint:
        provider_args.extend(["--config", f"OPENAI_BASE_URL={endpoint}"])
    run_cmd(provider_args, check=False, capture=True)

    # Step 3: Set inference route
    progress(70, "Setting inference route")
    run_cmd(
        ["openshell", "inference", "set", "--provider", provider_name, "--model", model],
        check=False, capture=True,
    )


def _apply_docker(
    *,
    rid: str,
    sandbox_name: str,
    image: str,
    ports: list[int],
    profile: str,
    endpoint: str,
    model: str,
) -> None:
    """Apply using Docker CLI (fallback when openshell not available)."""
    # Step 1: Build image if Dockerfile exists
    dockerfile = BLUEPRINT_DIR / "Dockerfile"
    project_root = BLUEPRINT_DIR.parent.parent

    if dockerfile.exists():
        progress(15, "Building Docker image")
        log(f"Building {image} from {dockerfile}...")
        run_cmd([
            "docker", "build",
            "-f", str(dockerfile),
            "-t", image,
            str(project_root),
        ])

    # Step 2: Remove existing container
    if container_exists(sandbox_name):
        progress(30, "Removing existing container")
        run_cmd(["docker", "rm", "-f", sandbox_name], check=False)

    # Step 3: Run container
    progress(50, "Starting container")
    cmd: list[str] = ["docker", "run", "-d", "--name", sandbox_name]

    for port in ports:
        cmd.extend(["-p", f"{port}:{port}"])

    # Volume for persistent data
    cmd.extend(["-v", "ncms-data:/app/data"])

    # Profile-specific NCMS env vars
    env_map = PROFILE_ENV_MAP.get(profile, PROFILE_ENV_MAP["default"])
    for k, v in env_map.items():
        cmd.extend(["-e", f"{k}={v}"])

    # Core NCMS features
    for feature in [
        "NCMS_SPLADE_ENABLED=true",
        "NCMS_EPISODES_ENABLED=true",
        "NCMS_INTENT_CLASSIFICATION_ENABLED=true",
        "NCMS_RERANKER_ENABLED=true",
        "NCMS_ADMISSION_ENABLED=true",
        "NCMS_RECONCILIATION_ENABLED=true",
    ]:
        cmd.extend(["-e", feature])

    # Pass through credentials if set
    for env_key in ["OPENAI_API_KEY", "NVIDIA_API_KEY", "HF_TOKEN"]:
        val = os.environ.get(env_key)
        if val:
            cmd.extend(["-e", f"{env_key}={val}"])

    cmd.append(image)
    run_cmd(cmd)

    progress(80, "Container started")


def action_status(rid: str | None = None) -> None:
    """Report current state of the most recent (or specified) run."""
    emit_run_id()

    if rid:
        run_dir = STATE_DIR / rid
    else:
        if not STATE_DIR.exists():
            log("No runs found.")
            return
        runs = sorted(STATE_DIR.iterdir(), reverse=True)
        if not runs:
            log("No runs found.")
            return
        run_dir = runs[0]

    plan_file = run_dir / "plan.json"
    if not plan_file.exists():
        log(json.dumps({"run_id": run_dir.name, "status": "unknown"}))
        return

    plan = json.loads(plan_file.read_text())
    rolled_back = (run_dir / "rolled_back").exists()

    log("")
    log("=== NemoClaw Run Status ===")
    log("")
    log(f"  Run ID:     {plan.get('run_id', 'unknown')}")
    log(f"  Profile:    {plan.get('profile', 'unknown')}")
    log(f"  Backend:    {plan.get('backend', 'unknown')}")
    log(f"  Sandbox:    {plan.get('sandbox_name', 'unknown')}")
    log(f"  Timestamp:  {plan.get('timestamp', 'unknown')}")
    if rolled_back:
        log("  Status:     ROLLED BACK")
    else:
        # Check if container is still running
        sandbox_name = plan.get("sandbox_name", "ncms-openclaw")
        if container_running(sandbox_name):
            log("  Status:     RUNNING")
        elif container_exists(sandbox_name):
            log("  Status:     STOPPED")
        else:
            log("  Status:     REMOVED")
    log("")

    inf = plan.get("inference", {})
    log("  Inference:")
    log(f"    Provider: {inf.get('provider', 'unknown')}")
    log(f"    Model:    {inf.get('model', 'unknown')}")
    log(f"    Endpoint: {inf.get('endpoint', 'unknown')}")
    log("")

    # Show container logs if running via Docker
    if plan.get("backend") == "docker":
        sandbox_name = plan.get("sandbox_name", "ncms-openclaw")
        if container_exists(sandbox_name):
            log("  Recent logs:")
            result = run_cmd(
                ["docker", "logs", "--tail", "10", sandbox_name],
                check=False, capture=True,
            )
            if result.returncode == 0:
                for line in (result.stdout + result.stderr).strip().splitlines():
                    log(f"    {line}")
            log("")


def action_rollback(rid: str) -> None:
    """Rollback a specific run: stop sandbox, clean up."""
    emit_run_id()

    run_dir = STATE_DIR / rid
    if not run_dir.exists():
        log(f"ERROR: Run {rid} not found.")
        sys.exit(1)

    plan_file = run_dir / "plan.json"
    if not plan_file.exists():
        log(f"ERROR: No plan.json for run {rid}.")
        sys.exit(1)

    plan = json.loads(plan_file.read_text())
    sandbox_name = plan.get("sandbox_name", "ncms-openclaw")
    backend = plan.get("backend", "docker")

    if backend == "openshell":
        progress(30, f"Stopping sandbox {sandbox_name}")
        run_cmd(
            ["openshell", "sandbox", "stop", sandbox_name],
            check=False, capture=True,
        )
        progress(60, f"Removing sandbox {sandbox_name}")
        run_cmd(
            ["openshell", "sandbox", "remove", sandbox_name],
            check=False, capture=True,
        )
    else:
        progress(30, f"Removing container {sandbox_name}")
        run_cmd(
            ["docker", "rm", "-f", sandbox_name],
            check=False, capture=True,
        )

    progress(90, "Cleaning up run state")
    (run_dir / "rolled_back").write_text(datetime.now(UTC).isoformat())

    progress(100, "Rollback complete")
    log(f"Run {rid} rolled back.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="NemoClaw Blueprint Runner for NCMS")
    parser.add_argument("action", choices=["plan", "apply", "status", "rollback"])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", dest="run_id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--endpoint-url",
        dest="endpoint_url",
        default=None,
        help="Override endpoint URL for the selected profile",
    )

    args = parser.parse_args()
    blueprint = load_blueprint()

    if args.action == "plan":
        action_plan(
            args.profile, blueprint,
            dry_run=args.dry_run, endpoint_url=args.endpoint_url,
        )
    elif args.action == "apply":
        action_apply(
            args.profile, blueprint,
            endpoint_url=args.endpoint_url,
        )
    elif args.action == "status":
        action_status(rid=args.run_id)
    elif args.action == "rollback":
        if not args.run_id:
            log("ERROR: --run-id is required for rollback")
            sys.exit(1)
        action_rollback(args.run_id)


if __name__ == "__main__":
    main()
