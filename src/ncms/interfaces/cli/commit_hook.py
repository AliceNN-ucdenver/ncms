"""ncms-commit-hook - Commit knowledge from coding agent sessions.

Supports both Claude Code and GitHub Copilot hook events.
Reads JSON from stdin, extracts knowledge, and commits to NCMS.

Usage:
    ncms-commit-hook --event stop --transcript /path/to/transcript
    ncms-commit-hook --event session-end
    ncms-commit-hook --event pre-compact --transcript /path/to/transcript
"""

from __future__ import annotations

import asyncio
import json
import sys

import click


@click.command()
@click.option(
    "--event",
    type=click.Choice(["stop", "session-end", "pre-compact", "file-changed", "post-tool", "error"]),
    required=True,
    help="Hook event type.",
)
@click.option("--transcript", default=None, help="Path to transcript file.")
@click.option("--tool-input", is_flag=True, help="Read tool input from stdin.")
@click.option("--project", default=None, help="Project directory.")
def main(event: str, transcript: str | None, tool_input: bool, project: str | None) -> None:
    """Commit knowledge from a coding agent session to NCMS."""
    asyncio.run(_handle_event(event, transcript, tool_input, project))


async def _handle_event(
    event: str,
    transcript: str | None,
    tool_input: bool,
    project: str | None,
) -> None:
    from ncms.config import NCMSConfig
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.application.memory_service import MemoryService

    config = NCMSConfig()
    store = SQLiteStore(db_path=config.db_path)
    await store.initialize()

    index = TantivyEngine(path=config.index_path)
    index.initialize()

    graph = NetworkXGraph()
    memory_svc = MemoryService(store=store, index=index, graph=graph, config=config)

    try:
        # Read stdin if available
        stdin_data = None
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                try:
                    stdin_data = json.loads(raw)
                except json.JSONDecodeError:
                    stdin_data = {"raw": raw}

        content = _extract_content(event, transcript, stdin_data)
        if content:
            await memory_svc.store_memory(
                content=content,
                memory_type="fact",
                tags=[f"hook:{event}"],
                source_agent=_detect_agent(stdin_data),
                project=project,
                importance=7.0 if event == "pre-compact" else 5.0,
            )
            click.echo(f"[ncms] Committed knowledge from {event} event", err=True)
    finally:
        await store.close()


def _extract_content(
    event: str,
    transcript: str | None,
    stdin_data: dict | None,
) -> str | None:
    """Extract meaningful content from the event context."""
    if event == "file-changed" and stdin_data:
        file_path = stdin_data.get("file_path", stdin_data.get("raw", ""))
        return f"Modified file: {file_path}"

    if event == "error" and stdin_data:
        return f"Error occurred: {json.dumps(stdin_data)[:500]}"

    if stdin_data and isinstance(stdin_data, dict):
        # Try to extract a summary from the input
        summary = stdin_data.get("summary", stdin_data.get("content", ""))
        if summary:
            return str(summary)[:1000]

    if transcript:
        try:
            with open(transcript) as f:
                lines = f.readlines()
            # Extract last meaningful exchange
            content = "".join(lines[-50:]) if len(lines) > 50 else "".join(lines)
            return f"Session {event}: {content[:1000]}"
        except (OSError, PermissionError):
            pass

    return None


def _detect_agent(stdin_data: dict | None) -> str:
    """Detect which coding agent is calling based on input shape."""
    if not stdin_data:
        return "unknown-agent"
    # Claude Code uses different keys than Copilot
    if "CLAUDE_TRANSCRIPT_PATH" in str(stdin_data):
        return "claude-code"
    if "copilot" in str(stdin_data).lower():
        return "github-copilot"
    return "coding-agent"


if __name__ == "__main__":
    main()
