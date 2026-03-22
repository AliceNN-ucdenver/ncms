"""NemoClaw Blueprint orchestrator for NCMS.

Reads blueprint.yaml, resolves profiles, and manages the sandbox container
lifecycle via Docker CLI (plan / apply / status / rollback).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

CONTAINER_NAME = "ncms-nemoclaw"
BLUEPRINT_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], *, check: bool = True, capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a shell command, printing it first."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)  # noqa: S603


def _container_running() -> bool:
    result = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _container_exists() -> bool:
    result = _run(
        ["docker", "inspect", CONTAINER_NAME],
        check=False,
        capture=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def load_blueprint(path: Path) -> dict:
    """Parse blueprint.yaml and return as dict."""
    with path.open() as f:
        return yaml.safe_load(f)


def resolve_profile(blueprint: dict, profile_name: str | None) -> dict:
    """Return the selected profile config, defaulting to 'default'."""
    profiles = blueprint.get("profiles", {})
    name = profile_name or "default"
    if name not in profiles:
        available = ", ".join(profiles.keys())
        print(f"Error: profile '{name}' not found. Available: {available}")
        sys.exit(1)
    profile = profiles[name]
    profile["_name"] = name
    return profile


def action_plan(blueprint: dict, profile: dict) -> dict:
    """Validate config and display the deployment plan."""
    sandbox = blueprint.get("sandbox", {})
    image = sandbox.get("image", "ncms-nemoclaw:latest")
    ports = sandbox.get("ports", [])
    volumes = sandbox.get("volumes", [])
    skills = blueprint.get("skills", [])
    env_vars = profile.get("env", {})

    plan = {
        "profile": profile["_name"],
        "description": profile.get("description", ""),
        "image": image,
        "ports": ports,
        "volumes": volumes,
        "skills": skills,
        "env_vars": env_vars,
        "inference": profile.get("inference", {}),
    }

    print()
    print("=== NemoClaw Blueprint Plan ===")
    print()
    print(f"  Profile:     {plan['profile']}")
    print(f"  Description: {plan['description']}")
    print(f"  Image:       {plan['image']}")
    print(f"  Container:   {CONTAINER_NAME}")
    print()
    print("  Ports:")
    for p in ports:
        print(f"    - {p}")
    print()
    print("  Volumes:")
    for v in volumes:
        print(f"    - {v}")
    print()
    print("  Environment:")
    for k, v in env_vars.items():
        display = v if v else '""'
        print(f"    {k}={display}")
    print()
    print("  Skills:")
    for s in skills:
        print(f"    - {s}")
    print()
    print("  Inference:")
    inf = profile.get("inference", {})
    print(f"    Provider: {inf.get('provider', 'unknown')}")
    print(f"    Model:    {inf.get('model', 'unknown')}")
    print(f"    Endpoint: {inf.get('endpoint', 'unknown')}")
    print()

    return plan


def action_apply(blueprint: dict, profile: dict) -> None:
    """Build the Docker image and run the container."""
    sandbox = blueprint.get("sandbox", {})
    image = sandbox.get("image", "ncms-nemoclaw:latest")
    ports = sandbox.get("ports", [])
    volumes = sandbox.get("volumes", [])
    env_vars = profile.get("env", {})

    # Show the plan first
    action_plan(blueprint, profile)

    # Stop existing container if running
    if _container_exists():
        print("Stopping existing container...")
        _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)

    # Build image from blueprint Dockerfile
    dockerfile = BLUEPRINT_DIR / "Dockerfile"
    if not dockerfile.exists():
        print(f"Error: Dockerfile not found at {dockerfile}")
        sys.exit(1)

    # Build context is the NCMS project root (two levels up from blueprint dir)
    project_root = BLUEPRINT_DIR.parent.parent
    print("Building Docker image...")
    _run([
        "docker", "build",
        "-f", str(dockerfile),
        "-t", image,
        str(project_root),
    ])

    # Run container
    print()
    print("Starting container...")
    cmd = ["docker", "run", "-d", "--name", CONTAINER_NAME]

    for p in ports:
        cmd.extend(["-p", p])

    for v in volumes:
        cmd.extend(["-v", v])

    for k, v in env_vars.items():
        cmd.extend(["-e", f"{k}={v}"])

    # Enable core NCMS features
    for feature_env in [
        "NCMS_SPLADE_ENABLED=true",
        "NCMS_EPISODES_ENABLED=true",
        "NCMS_INTENT_CLASSIFICATION_ENABLED=true",
        "NCMS_RERANKER_ENABLED=true",
        "NCMS_ADMISSION_ENABLED=true",
        "NCMS_RECONCILIATION_ENABLED=true",
    ]:
        cmd.extend(["-e", feature_env])

    cmd.append(image)
    _run(cmd)

    print()
    print("=== Container started ===")
    print("  Dashboard: http://localhost:8420")
    print("  MCP HTTP:  http://localhost:8080")
    print()


def action_status() -> None:
    """Show the container status."""
    print()
    print("=== NemoClaw Container Status ===")
    print()

    if not _container_exists():
        print("  Container not found. Run 'ncms-blueprint apply' first.")
        return

    result = _run(
        [
            "docker", "inspect", "-f",
            "Name: {{.Name}}\n"
            "State: {{.State.Status}}\n"
            "Running: {{.State.Running}}\n"
            "Started: {{.State.StartedAt}}\n"
            "Image: {{.Config.Image}}",
            CONTAINER_NAME,
        ],
        capture=True,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            print(f"  {line}")

    print()

    # Show port mappings
    result = _run(
        ["docker", "port", CONTAINER_NAME],
        check=False,
        capture=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        print("  Ports:")
        for line in result.stdout.strip().splitlines():
            print(f"    {line}")
    print()

    # Show recent logs
    print("  Recent logs:")
    result = _run(
        ["docker", "logs", "--tail", "10", CONTAINER_NAME],
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"    {line}")
    print()


def action_rollback() -> None:
    """Stop and remove the container."""
    print()
    print("=== NemoClaw Rollback ===")
    print()

    if not _container_exists():
        print("  No container to remove.")
        return

    print("  Stopping and removing container...")
    _run(["docker", "rm", "-f", CONTAINER_NAME])
    print("  Done.")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the NemoClaw Blueprint orchestrator."""
    parser = argparse.ArgumentParser(
        description="NemoClaw Blueprint orchestrator for NCMS",
    )
    parser.add_argument(
        "action",
        choices=["plan", "apply", "status", "rollback"],
        help="Action to perform",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Deployment profile (default, ollama, nim)",
    )
    parser.add_argument(
        "--blueprint",
        default=None,
        help="Path to blueprint.yaml (auto-detected if not set)",
    )

    args = parser.parse_args()

    # Locate blueprint.yaml
    blueprint_path = Path(args.blueprint) if args.blueprint else BLUEPRINT_DIR / "blueprint.yaml"
    if not blueprint_path.exists():
        print(f"Error: blueprint.yaml not found at {blueprint_path}")
        sys.exit(1)

    blueprint = load_blueprint(blueprint_path)

    if args.action == "status":
        action_status()
    elif args.action == "rollback":
        action_rollback()
    else:
        profile = resolve_profile(blueprint, args.profile)
        if args.action == "plan":
            action_plan(blueprint, profile)
        elif args.action == "apply":
            action_apply(blueprint, profile)


if __name__ == "__main__":
    main()
