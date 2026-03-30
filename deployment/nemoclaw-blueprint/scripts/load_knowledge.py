#!/usr/bin/env python3
"""Load knowledge files from a directory into the NCMS Hub via HTTP API.

Runs inside a NemoClaw sandbox. Uses httpx (same library as the bus sidecar)
so it shares the proxy approval for host.docker.internal:9080.

Usage:
    uv run python load_knowledge.py --hub http://host.docker.internal:9080 \
        --agent architect --dir /sandbox/knowledge/architecture
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

SUPPORTED_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".txt"}
MAX_CONTENT_SIZE = 50_000  # characters


def load_file(
    client: httpx.Client,
    hub_url: str,
    filepath: Path,
    domains: list[str],
    agent_id: str,
) -> bool:
    """Post a single file's content to the NCMS Hub."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  Skip: {filepath} (read error: {e})")
        return False

    if not content.strip():
        print(f"  Skip: {filepath} (empty)")
        return False

    # Truncate very large files
    if len(content) > MAX_CONTENT_SIZE:
        content = content[:MAX_CONTENT_SIZE]

    # Infer domain from path components
    file_domains = list(domains)
    rel = str(filepath)
    if "ADR" in rel or "adr" in rel:
        if "decisions" not in file_domains:
            file_domains.append("decisions")

    body = {
        "content": content,
        "type": "fact",
        "domains": file_domains,
        "source_agent": agent_id,
        "tags": [filepath.name],
    }

    try:
        resp = client.post(f"{hub_url.rstrip('/')}/api/v1/memories", json=body)
        if resp.status_code == 200:
            print(f"  Loaded: {filepath}")
            return True
        else:
            print(f"  Skip: {filepath} (HTTP {resp.status_code})")
            return False
    except Exception as e:
        print(f"  Skip: {filepath} ({e})")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Load knowledge into NCMS Hub")
    parser.add_argument("--hub", required=True, help="NCMS Hub URL")
    parser.add_argument("--agent", required=True, help="Agent ID (source_agent)")
    parser.add_argument(
        "--dir", required=True, action="append", dest="dirs",
        help="Directory to load (can specify multiple)",
    )
    parser.add_argument(
        "--domain", action="append", dest="domains", default=[],
        help="Domain tags (can specify multiple)",
    )
    args = parser.parse_args()

    total = 0
    loaded = 0

    # Use httpx (same as bus sidecar) so proxy approval is shared
    with httpx.Client(timeout=30.0) as client:
        for dir_path in args.dirs:
            p = Path(dir_path)
            if not p.is_dir():
                print(f"  Skip: {dir_path} (not a directory)")
                continue

            for filepath in sorted(p.rglob("*")):
                if not filepath.is_file():
                    continue
                if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if filepath.name.startswith("."):
                    continue

                # Infer domain from directory name if not specified
                domains = list(args.domains)
                if not domains:
                    rel = filepath.relative_to(p)
                    if rel.parts:
                        domains = [p.name]

                total += 1
                if load_file(client, args.hub, filepath, domains, args.agent):
                    loaded += 1

    print(f"\n  Result: {loaded}/{total} files loaded into hub")
    sys.exit(0 if loaded > 0 else 1)


if __name__ == "__main__":
    main()
