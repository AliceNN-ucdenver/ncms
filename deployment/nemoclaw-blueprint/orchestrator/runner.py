#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""
NemoClaw Blueprint Runner for NCMS — Multi-Sandbox Edition

Orchestrates NCMS Hub + 3 agent sandboxes inside OpenShell (when available)
or falls back to Docker Compose. Compatible with the NemoClaw Blueprint
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

# NemoClaw provider mapping per profile
NEMOCLAW_PROVIDER_MAP: dict[str, dict[str, str]] = {
    "default": {"provider": "vllm", "experimental": "1"},
    "ollama": {"provider": "ollama", "experimental": ""},
    "nim": {"provider": "cloud", "experimental": ""},
    "vllm": {"provider": "vllm", "experimental": "1"},
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
    return subprocess.run(  # noqa: S603
        args, check=check, capture_output=capture, text=True,
    )


def openshell_available() -> bool:
    return shutil.which("openshell") is not None


def nemoclaw_available() -> bool:
    return shutil.which("nemoclaw") is not None


def docker_available() -> bool:
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


def sandbox_exists(name: str) -> bool:
    """Check if an OpenShell sandbox exists."""
    result = run_cmd(
        ["openshell", "sandbox", "list"],
        check=False, capture=True,
    )
    return result.returncode == 0 and name in result.stdout


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


def get_sandboxes(blueprint: dict[str, Any]) -> dict[str, Any]:
    """Get sandbox definitions from blueprint."""
    return blueprint.get("components", {}).get("sandboxes", {})


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
    sandboxes = get_sandboxes(blueprint)
    skills = blueprint.get("skills", [])

    endpoint = endpoint_url or inf_cfg.get("endpoint", "")
    model = inf_cfg.get("model", "")

    progress(20, "Checking prerequisites")
    use_openshell = openshell_available()
    use_nemoclaw = nemoclaw_available()
    if not use_openshell and not use_nemoclaw:
        if not docker_available():
            log("ERROR: Neither nemoclaw, openshell, nor docker found on PATH.")
            sys.exit(1)
        log("INFO: nemoclaw/openshell not found — will use Docker Compose fallback")

    backend = "nemoclaw" if use_nemoclaw or use_openshell else "docker"

    sandbox_list = []
    for key, cfg in sandboxes.items():
        sandbox_list.append({
            "key": key,
            "name": cfg.get("name", key),
            "role": cfg.get("role", "agent"),
            "agent_id": cfg.get("agent_id", key),
            "domains": cfg.get("domains", []),
        })

    plan = {
        "run_id": rid,
        "profile": profile,
        "sandboxes": sandbox_list,
        "inference": {
            "provider": inf_cfg.get("provider_type", "openai"),
            "model": model,
            "endpoint": endpoint,
        },
        "backend": backend,
        "dry_run": dry_run,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    progress(50, "Plan ready")

    log("")
    log("=== NemoClaw Blueprint Plan (Multi-Sandbox) ===")
    log("")
    log(f"  Profile:    {profile}")
    log(f"  Backend:    {backend.title()}")
    log(f"  Model:      {model}")
    log(f"  Endpoint:   {endpoint}")
    log("")
    log("  Sandboxes:")
    for sb in sandbox_list:
        role = sb["role"]
        name = sb["name"]
        domains = ", ".join(sb.get("domains", []))
        if role == "hub":
            log(f"    {name:25s}  [HUB]  ports: 8080, 8420")
        else:
            log(f"    {name:25s}  [{sb['agent_id']:10s}]  domains: {domains}")
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
    endpoint_url: str | None = None,
) -> None:
    """Apply: create all sandboxes + configure inference."""
    rid = emit_run_id()
    progress(5, "Loading plan")

    inf_cfg = resolve_profile(blueprint, profile)
    sandboxes = get_sandboxes(blueprint)

    endpoint = endpoint_url or inf_cfg.get("endpoint", "")
    model = inf_cfg.get("model", "")
    provider_type = inf_cfg.get("provider_type", "openai")
    provider_name = inf_cfg.get("provider_name", "ncms-inference")

    use_nemoclaw = nemoclaw_available() or openshell_available()

    if use_nemoclaw:
        _apply_nemoclaw(
            rid=rid,
            sandboxes=sandboxes,
            profile=profile,
            inf_cfg=inf_cfg,
            endpoint=endpoint,
            model=model,
            provider_name=provider_name,
        )
    else:
        _apply_docker_compose(
            rid=rid,
            profile=profile,
            endpoint=endpoint,
            model=model,
        )

    # Save run state
    progress(90, "Saving run state")
    state_dir = STATE_DIR / rid
    state_dir.mkdir(parents=True, exist_ok=True)
    sandbox_names = [cfg.get("name", key) for key, cfg in sandboxes.items()]
    (state_dir / "plan.json").write_text(
        json.dumps(
            {
                "run_id": rid,
                "profile": profile,
                "sandboxes": sandbox_names,
                "backend": "nemoclaw" if use_nemoclaw else "docker",
                "inference": {
                    "provider": provider_type,
                    "model": model,
                    "endpoint": endpoint,
                },
                "timestamp": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
    )

    progress(100, "Apply complete")
    log("")
    log(f"All sandboxes ready (run_id: {rid}).")
    log("  Dashboard:  http://localhost:8420")
    log("  Hub API:    http://localhost:9080")
    log(f"  Inference:  {provider_name} -> {model} @ {endpoint}")
    log("")
    log("  Connect to agents:")
    for key, cfg in sandboxes.items():
        name = cfg.get("name", key)
        if cfg.get("role") != "hub":
            log(f"    nemoclaw {name} connect")
    log("")


def _apply_nemoclaw(
    *,
    rid: str,
    sandboxes: dict[str, Any],
    profile: str,
    inf_cfg: dict[str, Any],
    endpoint: str,
    model: str,
    provider_name: str,
) -> None:
    """Apply using NemoClaw/OpenShell CLI (native sandbox path)."""
    nc_map = NEMOCLAW_PROVIDER_MAP.get(profile, NEMOCLAW_PROVIDER_MAP["default"])
    credential_env = inf_cfg.get("credential_env")
    credential_default: str = inf_cfg.get("credential_default", "")

    # Step 1: Create Hub sandbox via nemoclaw onboard (sets up gateway + provider)
    hub_cfg = None
    hub_name = ""
    for key, cfg in sandboxes.items():
        if cfg.get("role") == "hub":
            hub_cfg = cfg
            hub_name = cfg.get("name", key)
            break

    if hub_cfg:
        progress(10, f"Creating hub sandbox: {hub_name}")

        # Set NemoClaw env vars for non-interactive onboard
        onboard_env = os.environ.copy()
        onboard_env["NEMOCLAW_SANDBOX_NAME"] = hub_name
        onboard_env["NEMOCLAW_PROVIDER"] = nc_map["provider"]
        onboard_env["NEMOCLAW_MODEL"] = model
        onboard_env["NEMOCLAW_NON_INTERACTIVE"] = "1"
        onboard_env["NEMOCLAW_RECREATE_SANDBOX"] = "1"
        if nc_map.get("experimental"):
            onboard_env["NEMOCLAW_EXPERIMENTAL"] = "1"

        subprocess.run(  # noqa: S603
            ["nemoclaw", "onboard", "--non-interactive"],
            env=onboard_env, check=False,
        )
        log(f"  Hub sandbox {hub_name} created via nemoclaw onboard")

    # Step 2: Configure inference provider for DGX Spark
    progress(30, f"Configuring inference: {provider_name}")
    credential = ""
    if credential_env:
        credential = os.environ.get(credential_env, credential_default)

    provider_args = [
        "openshell", "provider", "create",
        "--name", provider_name,
        "--type", inf_cfg.get("provider_type", "openai"),
    ]
    if credential:
        provider_args.extend(["--credential", f"OPENAI_API_KEY={credential}"])
    if endpoint:
        provider_args.extend(["--config", f"OPENAI_BASE_URL={endpoint}"])
    run_cmd(provider_args, check=False, capture=True)

    run_cmd(
        [
            "openshell", "inference", "set",
            "--no-verify",
            "--provider", provider_name,
            "--model", model,
        ],
        check=False, capture=True,
    )

    # Step 3: Create agent sandboxes
    policy_file = str(BLUEPRINT_DIR / "policies" / "openclaw-sandbox.yaml")
    agent_count = sum(
        1 for cfg in sandboxes.values() if cfg.get("role") != "hub"
    )
    agent_idx = 0

    for key, cfg in sandboxes.items():
        if cfg.get("role") == "hub":
            continue

        name = cfg.get("name", key)
        agent_idx += 1
        pct = 30 + int(50 * agent_idx / max(agent_count, 1))
        progress(pct, f"Creating agent sandbox: {name}")

        # Remove existing sandbox if present
        if sandbox_exists(name):
            run_cmd(
                ["openshell", "sandbox", "stop", name],
                check=False, capture=True,
            )
            run_cmd(
                ["openshell", "sandbox", "remove", name],
                check=False, capture=True,
            )

        # Create sandbox from openclaw base
        create_args = [
            "openshell", "sandbox", "create",
            "--from", cfg.get("base", "openclaw"),
            "--name", name,
            "--policy", policy_file,
        ]
        run_cmd(create_args, check=False, capture=True)

        # Install skill
        skill_path = cfg.get("skill", "")
        if skill_path:
            full_skill_path = BLUEPRINT_DIR / skill_path
            if full_skill_path.exists():
                log(f"  Installing skill: {skill_path}")
                # Copy skill into sandbox via nemoclaw connect
                skill_content = full_skill_path.read_text()
                skill_basename = full_skill_path.name
                subprocess.run(  # noqa: S603
                    [
                        "nemoclaw", name, "connect", "--",
                        "bash", "-c",
                        f"mkdir -p /sandbox/.agents/skills && "
                        f"cat > /sandbox/.agents/skills/{skill_basename}",
                    ],
                    input=skill_content, text=True, check=False,
                )

        log(f"  Agent sandbox {name} created")


def _apply_docker_compose(
    *,
    rid: str,
    profile: str,
    endpoint: str,
    model: str,
) -> None:
    """Apply using Docker Compose (fallback when NemoClaw not available)."""
    compose_file = BLUEPRINT_DIR / "docker-compose.nemoclaw.yaml"
    if not compose_file.exists():
        log(f"ERROR: {compose_file} not found")
        sys.exit(1)

    # Set profile env vars
    env_map = PROFILE_ENV_MAP.get(profile, PROFILE_ENV_MAP["default"])
    for k, v in env_map.items():
        os.environ[k] = v

    # Build
    progress(15, "Building Docker images")
    run_cmd([
        "docker", "compose", "-f", str(compose_file), "build",
    ])

    # Run
    progress(50, "Starting containers")
    run_cmd([
        "docker", "compose", "-f", str(compose_file), "up", "-d",
    ])

    progress(80, "Containers started")


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
    log(f"  Timestamp:  {plan.get('timestamp', 'unknown')}")

    if rolled_back:
        log("  Status:     ROLLED BACK")
    else:
        sandbox_names = plan.get("sandboxes", [])
        backend = plan.get("backend", "docker")

        log("")
        log("  Sandboxes:")
        for name in sandbox_names:
            if backend == "nemoclaw" and openshell_available():
                if sandbox_exists(name):
                    result = run_cmd(
                        ["nemoclaw", name, "status"],
                        check=False, capture=True,
                    )
                    status = result.stdout.strip()[:60] if result.returncode == 0 else "unknown"
                    log(f"    ● {name:25s}  {status}")
                else:
                    log(f"    ○ {name:25s}  not found")
            else:
                if container_running(name):
                    log(f"    ● {name:25s}  RUNNING")
                elif container_exists(name):
                    log(f"    ○ {name:25s}  STOPPED")
                else:
                    log(f"    ○ {name:25s}  REMOVED")

    log("")
    inf = plan.get("inference", {})
    log("  Inference:")
    log(f"    Provider: {inf.get('provider', 'unknown')}")
    log(f"    Model:    {inf.get('model', 'unknown')}")
    log(f"    Endpoint: {inf.get('endpoint', 'unknown')}")
    log("")

    # Show container/sandbox logs
    backend = plan.get("backend", "docker")
    sandbox_names = plan.get("sandboxes", [])
    if backend == "docker":
        for name in sandbox_names:
            if container_exists(name):
                log(f"  Recent logs ({name}):")
                result = run_cmd(
                    ["docker", "logs", "--tail", "5", name],
                    check=False, capture=True,
                )
                if result.returncode == 0:
                    for line in (result.stdout + result.stderr).strip().splitlines():
                        log(f"    {line}")
                log("")


def action_rollback(rid: str) -> None:
    """Rollback a specific run: stop all sandboxes, clean up."""
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
    sandbox_names = plan.get("sandboxes", [])
    backend = plan.get("backend", "docker")

    total = len(sandbox_names)
    for i, name in enumerate(reversed(sandbox_names)):
        pct = 10 + int(70 * (i + 1) / max(total, 1))

        if backend == "nemoclaw":
            progress(pct, f"Stopping sandbox {name}")
            run_cmd(
                ["openshell", "sandbox", "stop", name],
                check=False, capture=True,
            )
            run_cmd(
                ["openshell", "sandbox", "remove", name],
                check=False, capture=True,
            )
        else:
            progress(pct, f"Removing container {name}")
            run_cmd(
                ["docker", "rm", "-f", name],
                check=False, capture=True,
            )

    progress(90, "Cleaning up run state")
    (run_dir / "rolled_back").write_text(datetime.now(UTC).isoformat())

    progress(100, "Rollback complete")
    log(f"Run {rid} rolled back ({total} sandboxes removed).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NemoClaw Blueprint Runner for NCMS (Multi-Sandbox)",
    )
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
