"""Wiki export — generates a linked markdown wiki from the memory store.

Produces pages for entities, episodes, agents, and projects with
backlinks, state timelines, and episode narratives.

Usage:
    ncms export --output ./wiki
    ncms export --output ./wiki --format markdown
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ncms.config import NCMSConfig
from ncms.domain.models import (
    EntityStateMeta,
    EpisodeMeta,
    NodeType,
)

if TYPE_CHECKING:
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


async def export_wiki(
    config: NCMSConfig,
    output_dir: Path,
) -> dict[str, int]:
    """Export the memory store as a linked markdown wiki.

    Generates:
    - index.md — top-level table of contents
    - entities/*.md — per-entity pages with state timelines + memory backlinks
    - episodes/*.md — per-episode pages with member lists + summaries
    - agents/*.md — per-agent pages with memory counts + domain expertise
    - insights/*.md — abstract/insight pages from consolidation

    Args:
        config: NCMS configuration.
        output_dir: Directory to write wiki files.

    Returns:
        Dict of page counts per category.
    """
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=config.db_path)
    await store.initialize()
    graph = NetworkXGraph()

    # Rebuild graph from stored entities/relationships
    from ncms.application.graph_service import GraphService

    graph_svc = GraphService(store=store, graph=graph)
    await graph_svc.rebuild_from_store()

    counts: dict[str, int] = {
        "entities": 0,
        "episodes": 0,
        "agents": 0,
        "insights": 0,
    }

    try:
        # Create directories
        for subdir in ["entities", "episodes", "agents", "insights"]:
            (output_dir / subdir).mkdir(parents=True, exist_ok=True)

        all_entities = await store.list_entities()
        entity_memory_counts = await _generate_entity_pages(
            store,
            graph,
            output_dir,
            all_entities,
            counts,
        )
        await _generate_episode_pages(store, output_dir, counts)
        await _generate_agent_pages(store, output_dir, counts)
        await _generate_insight_pages(store, output_dir, counts)
        _generate_index_page(
            output_dir,
            counts,
            entity_memory_counts,
            all_entities,
        )

        logger.info(
            "[export] Wiki exported to %s: %d entity, %d episode, %d agent, %d insight pages",
            output_dir,
            counts["entities"],
            counts["episodes"],
            counts["agents"],
            counts["insights"],
        )

    finally:
        await store.close()

    return counts


async def _generate_entity_pages(
    store: SQLiteStore,
    graph: object,
    output_dir: Path,
    all_entities: list,
    counts: dict[str, int],
) -> dict[str, int]:
    """Generate per-entity wiki pages with state timelines and memory backlinks."""
    logger.info("[export] Generating %d entity pages...", len(all_entities))
    entity_memory_counts: dict[str, int] = {}

    for entity in all_entities:
        entity_id = entity.id
        name = entity.name
        etype = entity.type or "unknown"
        slug = _slugify(name)

        memory_ids = graph.get_memory_ids_for_entity(entity_id)
        entity_memory_counts[entity_id] = len(memory_ids)

        states = await store.get_entity_states_by_entity(entity_id)
        states.sort(key=lambda s: s.observed_at or s.created_at)

        lines = [
            f"# {name}",
            "",
            f"**Type:** {etype}  ",
            f"**ID:** `{entity_id}`  ",
            f"**Linked memories:** {len(memory_ids)}  ",
            "",
        ]

        if states:
            lines.append("## State Timeline")
            lines.append("")
            lines.append("| Date | Key | Value | Current |")
            lines.append("|------|-----|-------|---------|")
            for node in states:
                meta = EntityStateMeta.from_node(node)
                if meta:
                    obs = (node.observed_at or node.created_at).strftime("%Y-%m-%d %H:%M")
                    current = "Yes" if node.is_current else "No"
                    val = meta.state_value[:100]
                    lines.append(f"| {obs} | {meta.state_key} | {val} | {current} |")
            lines.append("")

        memory_ids_list = sorted(memory_ids)
        if memory_ids_list:
            lines.append("## Linked Memories")
            lines.append("")
            for mid in memory_ids_list[:50]:
                mem = await store.get_memory(mid)
                if mem:
                    preview = mem.content[:120].replace("\n", " ")
                    lines.append(f"- `{mid[:8]}` — {preview}")
            if len(memory_ids_list) > 50:
                lines.append(f"- ... and {len(memory_ids_list) - 50} more")
            lines.append("")

        (output_dir / "entities" / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")
        counts["entities"] += 1

    return entity_memory_counts


async def _generate_episode_pages(
    store: SQLiteStore,
    output_dir: Path,
    counts: dict[str, int],
) -> None:
    """Generate per-episode wiki pages with member lists and summaries."""
    all_episodes = await store.get_memory_nodes_by_type(NodeType.EPISODE)
    logger.info("[export] Generating %d episode pages...", len(all_episodes))

    for ep_node in all_episodes:
        ep_meta = EpisodeMeta.from_node(ep_node)
        if not ep_meta:
            continue

        slug = _slugify(ep_meta.episode_title or ep_node.id[:8])
        members = await store.get_episode_members(ep_node.id)

        lines = [
            f"# {ep_meta.episode_title or 'Untitled Episode'}",
            "",
            f"**Status:** {ep_meta.status}  ",
            f"**Members:** {ep_meta.member_count}  ",
            f"**Anchor:** {ep_meta.anchor_type}  ",
            "",
        ]

        if ep_meta.topic_entities:
            entity_links = [
                f"[{n}](../entities/{_slugify(n)}.md)" for n in ep_meta.topic_entities[:10]
            ]
            lines.append("**Topic entities:** " + ", ".join(entity_links))
            lines.append("")

        summary = await _find_episode_summary(store, ep_node.id)
        if summary:
            lines.extend(["## Summary", "", summary, ""])

        if members:
            lines.extend(["## Members", ""])
            for member in members:
                mem = await store.get_memory(member.memory_id)
                if mem:
                    preview = mem.content[:120].replace("\n", " ")
                    agent = mem.source_agent or "unknown"
                    lines.append(f"- [{agent}] `{mem.id[:8]}` — {preview}")
            lines.append("")

        (output_dir / "episodes" / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")
        counts["episodes"] += 1


async def _generate_agent_pages(
    store: SQLiteStore,
    output_dir: Path,
    counts: dict[str, int],
) -> None:
    """Generate per-agent wiki pages with memory counts and domain expertise."""
    all_memories = await store.list_memories(limit=10000)
    agent_memories: dict[str, list] = {}
    for mem in all_memories:
        agent = mem.source_agent or "anonymous"
        if agent not in agent_memories:
            agent_memories[agent] = []
        agent_memories[agent].append(mem)

    logger.info("[export] Generating %d agent pages...", len(agent_memories))

    for agent_id, memories in agent_memories.items():
        slug = _slugify(agent_id)
        domains: set[str] = set()
        for mem in memories:
            if mem.domains:
                domains.update(mem.domains)

        type_counts: dict[str, int] = {}
        for mem in memories:
            t = mem.type or "unknown"
            type_counts[t] = type_counts.get(t, 0) + 1

        lines = [
            f"# Agent: {agent_id}",
            "",
            f"**Total memories:** {len(memories)}  ",
            f"**Domains:** {', '.join(sorted(domains)) or 'none'}  ",
            "",
            "## Memory Types",
            "",
            "| Type | Count |",
            "|------|-------|",
        ]
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {t} | {c} |")
        lines.append("")

        lines.extend(["## Recent Memories", ""])
        recent = sorted(memories, key=lambda m: m.created_at, reverse=True)
        for mem in recent[:20]:
            preview = mem.content[:120].replace("\n", " ")
            created = mem.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"- [{created}] `{mem.id[:8]}` — {preview}")
        if len(memories) > 20:
            lines.append(f"- ... and {len(memories) - 20} more")
        lines.append("")

        (output_dir / "agents" / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")
        counts["agents"] += 1


async def _generate_insight_pages(
    store: SQLiteStore,
    output_dir: Path,
    counts: dict[str, int],
) -> None:
    """Generate insight/abstract wiki pages from consolidation output."""
    abstracts = await store.get_memory_nodes_by_type(NodeType.ABSTRACT)
    logger.info("[export] Generating %d insight pages...", len(abstracts))

    for node in abstracts:
        mem = await store.get_memory(node.memory_id)
        if not mem:
            continue

        node_meta = node.metadata or {}
        abstract_type = node_meta.get("abstract_type", "insight")
        slug = _slugify(f"{abstract_type}-{node.id[:8]}")

        lines = [
            f"# {abstract_type.replace('_', ' ').title()}",
            "",
            f"**Type:** {abstract_type}  ",
            f"**Created:** {node.created_at.strftime('%Y-%m-%d %H:%M')}  ",
            "",
            "## Content",
            "",
            mem.content,
            "",
        ]

        src_episodes = node_meta.get("source_episode_ids", [])
        if src_episodes:
            lines.extend(["## Source Episodes", ""])
            for ep_id in src_episodes:
                lines.append(f"- `{ep_id[:8]}`")
            lines.append("")

        key_ents = node_meta.get("key_entities", node_meta.get("topic_entities", []))
        if key_ents:
            entity_links = [f"[{n}](../entities/{_slugify(n)}.md)" for n in key_ents[:10]]
            lines.append("**Key entities:** " + ", ".join(entity_links))
            lines.append("")

        (output_dir / "insights" / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")
        counts["insights"] += 1


def _generate_index_page(
    output_dir: Path,
    counts: dict[str, int],
    entity_memory_counts: dict[str, int],
    all_entities: list,
) -> None:
    """Generate the top-level index.md page."""
    total = sum(counts.values())
    lines = [
        "# NCMS Knowledge Wiki",
        "",
        f"*Exported {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        f"**Total pages:** {total}",
        "",
        "## Contents",
        "",
        f"- **[Entities](entities/)** — {counts['entities']} entities",
        f"- **[Episodes](episodes/)** — {counts['episodes']} episodes",
        f"- **[Agents](agents/)** — {counts['agents']} agents",
        f"- **[Insights](insights/)** — {counts['insights']} insights",
        "",
    ]

    if entity_memory_counts:
        lines.extend(["## Top Entities by Memory Count", ""])
        sorted_ents = sorted(entity_memory_counts.items(), key=lambda x: -x[1])
        for eid, count in sorted_ents[:20]:
            ent = next((e for e in all_entities if e.id == eid), None)
            if ent:
                slug = _slugify(ent.name)
                lines.append(f"- [{ent.name}](entities/{slug}.md) ({count})")
        lines.append("")

    (output_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


async def _find_episode_summary(
    store: SQLiteStore,
    episode_node_id: str,
) -> str | None:
    """Find abstract summary for an episode."""
    try:
        edges = await store.get_graph_edges(episode_node_id)
    except Exception:
        return None
    for edge in edges:
        if edge.edge_type == "summarizes":
            summary_node = await store.get_memory_node(edge.source_id)
            if summary_node:
                memory = await store.get_memory(summary_node.memory_id)
                if memory:
                    return memory.content[:1000]
    return None


def _slugify(text: str) -> str:
    """Convert text to filesystem-safe slug."""
    import re

    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug[:80]  # Cap length
    return slug or "unnamed"
