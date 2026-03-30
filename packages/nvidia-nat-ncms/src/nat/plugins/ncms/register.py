# SPDX-License-Identifier: Apache-2.0
"""Factory registration for the NCMS memory provider."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nat.builder.builder import Builder
from nat.cli.register_workflow import register_memory

from .config import NCMSMemoryConfig
from .editor import NCMSMemoryEditor
from .http_client import NCMSHttpClient
from .sse_listener import sse_listener

logger = logging.getLogger(__name__)


@register_memory(config_type=NCMSMemoryConfig)
async def ncms_memory_client(config: NCMSMemoryConfig, builder: Builder):
    """Build an NCMSMemoryEditor and manage its lifecycle.

    On startup:
    1. Create HTTP client pointed at the NCMS Hub
    2. Register with the Knowledge Bus (if domains configured)
    3. Load domain knowledge files (if knowledge_paths configured)
    4. Start SSE listener for announcements (if enabled)

    On shutdown:
    5. Cancel SSE listener
    6. Deregister from bus
    7. Close HTTP client
    """
    client = NCMSHttpClient(
        hub_url=config.hub_url,
        connect_timeout=config.connect_timeout_s,
        request_timeout=config.request_timeout_s,
    )

    # Register with Knowledge Bus if this agent handles domains
    if config.domains:
        try:
            await client.bus_register(
                config.agent_id, config.domains, config.subscribe_to,
            )
            logger.info(
                "Registered agent %s for domains %s", config.agent_id, config.domains,
            )
        except Exception:
            logger.warning(
                "Bus registration failed for %s — continuing without bus",
                config.agent_id,
                exc_info=True,
            )

    # Load domain knowledge on startup — read files and store via API
    knowledge_paths = config.knowledge_paths
    logger.info(
        "Knowledge paths for %s: %s", config.agent_id, knowledge_paths,
    )
    if knowledge_paths:
        await _load_knowledge_files(client, config.agent_id, config.domains, knowledge_paths)

    editor = NCMSMemoryEditor(client=client, config=config)

    # Mutable holder for late-binding the workflow callback.
    # The SSE listener starts immediately with a proxy; the actual
    # workflow_fn gets bound later when NAT finishes building it.
    _workflow_holder: list[Any] = []

    async def _workflow_proxy(input_message: str) -> str:
        """Proxy that delegates to the late-bound workflow callback."""
        if _workflow_holder:
            return await _workflow_holder[0](input_message)
        raise RuntimeError("Workflow not yet bound")

    # Expose the holder on the editor so external code can bind it
    editor._workflow_holder = _workflow_holder  # type: ignore[attr-defined]

    # Detect the agent's listening port from the NAT FastAPI frontend.
    # This enables the SSE listener to self-call /generate for LLM synthesis.
    _self_port: int | None = None
    try:
        import os
        # NAT sets the port via CLI args; we detect it from the config or env
        _self_port = int(os.environ.get("NAT_PORT", "0")) or None
    except (ValueError, TypeError):
        pass

    # Start SSE listener for bus announcements
    sse_task: asyncio.Task | None = None
    if config.enable_sse and config.subscribe_to:
        sse_task = asyncio.create_task(
            sse_listener(
                client, config.agent_id, config.subscribe_to,
                workflow_fn=_workflow_proxy,
                domains=config.domains,
                self_port=_self_port,
            ),
            name=f"ncms-sse-{config.agent_id}",
        )
        logger.info("SSE listener started for agent %s", config.agent_id)

    try:
        yield editor
    finally:
        # Cleanup
        if sse_task is not None:
            sse_task.cancel()
            try:
                await sse_task
            except asyncio.CancelledError:
                pass

        if config.domains:
            try:
                await client.bus_deregister(config.agent_id)
            except Exception:
                logger.debug("Bus deregister failed on shutdown", exc_info=True)

        await client.close()
        logger.info("NCMS memory client shut down for agent %s", config.agent_id)


_SUPPORTED_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".txt"}
_MAX_CONTENT_SIZE = 50_000


async def _load_knowledge_files(
    client: NCMSHttpClient,
    agent_id: str,
    agent_domains: list[str],
    knowledge_paths: list,
) -> None:
    """Read knowledge files from disk and store each as a memory in the hub."""
    from pathlib import Path

    total = 0
    loaded = 0

    for kp in knowledge_paths:
        dir_path = kp if isinstance(kp, str) else kp.get("path", "")
        extra_domains = [] if isinstance(kp, str) else kp.get("domains", [])
        if not dir_path:
            continue

        p = Path(dir_path)
        if not p.is_dir():
            logger.warning("[knowledge] Not a directory: %s", dir_path)
            continue

        for filepath in sorted(p.rglob("*")):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                continue
            if filepath.name.startswith("."):
                continue

            total += 1
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                logger.warning("[knowledge] Read error: %s", filepath)
                continue

            if not content.strip():
                continue
            if len(content) > _MAX_CONTENT_SIZE:
                content = content[:_MAX_CONTENT_SIZE]

            # Merge agent domains + per-path domains
            file_domains = list(set(agent_domains + extra_domains))
            rel = str(filepath)
            if "ADR" in rel or "adr" in rel:
                if "decisions" not in file_domains:
                    file_domains.append("decisions")

            try:
                await client.store_memory(
                    content=content,
                    type="fact",
                    domains=file_domains,
                    source_agent=agent_id,
                )
                loaded += 1
                logger.info("[knowledge] Loaded: %s", filepath.name)
            except Exception:
                logger.warning("[knowledge] Failed: %s", filepath.name, exc_info=True)

    logger.info("[knowledge] Loaded %d/%d files into hub for %s", loaded, total, agent_id)
