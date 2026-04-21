"""Episode Service — hybrid episode linker for automatic episode formation.

Groups related memory fragments into bounded event arcs (episodes) using
a multi-signal approach:

- **BM25** for lexical overlap with episode profiles
- **SPLADE** for sparse semantic similarity (optional, when enabled)
- **GLiNER entity overlap** for topic structure
- **Temporal/domain/agent signals** for contextual continuity
- **Structured anchors** as a supplementary bonus (JIRA-123, etc.)

Each episode maintains a compact profile (backing Memory) indexed in BM25
and optionally SPLADE, enabling cheap candidate generation via existing
search infrastructure.

Feature-flagged via config.temporal_enabled (default False).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ncms.config import NCMSConfig
from ncms.domain.models import (
    EdgeType,
    EpisodeMeta,
    EpisodeStatus,
    GraphEdge,
    Memory,
    MemoryNode,
    NodeType,
)
from ncms.domain.protocols import IndexEngine, MemoryStore
from ncms.infrastructure.observability.event_log import EventLog, NullEventLog

logger = logging.getLogger(__name__)


# ── Structured Anchor Detection (bonus signal) ────────────────────────────

# Issue/ticket/PR IDs: JIRA-123, GH-456, PROJ-789, #123, PR-567
_ISSUE_ID_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9]{1,10}-\d{1,6})\b"  # JIRA-123, PROJ-456
    r"|(?:^|\s)#(\d{2,6})\b"  # #123 (GitHub-style, 2+ digits)
    r"|\bPR[- ]?(\d{1,6})\b"  # PR-567, PR 567
    r"|\b(?:pull request|merge request)\s*#?(\d+)\b",  # pull request #123
    re.IGNORECASE,
)

# Release/version markers
_RELEASE_PATTERN = re.compile(
    r"\b(?:release[ds]?|deploy(?:ed|ing)?|version|bump(?:ed)?)\s+"
    r"(?:to\s+)?v?\d+\.\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

# Incident markers
_INCIDENT_MARKERS: frozenset[str] = frozenset({
    "incident", "outage", "downtime", "error spike", "rollback",
    "rolled back", "p0", "p1", "sev1", "sev2", "postmortem",
    "post-mortem", "root cause", "rca",
})

# Migration markers
_MIGRATION_MARKERS: frozenset[str] = frozenset({
    "migration", "migrating", "migrated", "data migration",
    "schema migration", "cutover", "blue-green", "canary deploy",
})

# Resolution markers (for episode closure detection)
_RESOLUTION_MARKERS: frozenset[str] = frozenset({
    "resolved", "fixed", "completed", "closed", "done",
    "merged", "shipped", "deployed successfully", "migration complete",
    "incident resolved", "postmortem complete",
})


@dataclass
class _CandidateScores:
    """BM25 + SPLADE search scores for an episode candidate."""

    bm25: float = 0.0
    splade: float = 0.0


class EpisodeService:
    """Hybrid episode linker: incremental episode formation via multi-signal matching.

    Instead of relying on a single mechanism (regex), uses the tools already
    available today:

    - **BM25** for fast lexical matching against episode profiles
    - **SPLADE** for broader sparse semantic overlap (optional)
    - **GLiNER entity overlap** for shared entities and topic structure
    - **Temporal/domain/agent signals** for contextual continuity
    - **Structured anchors** as supplementary bonus (not required)

    Each episode maintains a compact profile indexed in BM25/SPLADE, enabling
    cheap candidate generation via existing search infrastructure.
    """

    def __init__(
        self,
        store: MemoryStore,
        index: IndexEngine | None = None,
        config: NCMSConfig | None = None,
        event_log: EventLog | NullEventLog | None = None,
        splade: object | None = None,
    ) -> None:
        self._store = store
        self._index = index  # Tantivy BM25 engine
        self._config = config or NCMSConfig()
        self._event_log: EventLog | NullEventLog = event_log or NullEventLog()
        self._splade = splade  # Optional SPLADE engine
        # In-memory cache of open episode profiles, batch-loaded at the
        # start of each assign_or_create() call to replace per-candidate
        # sequential DB queries.  Keys are episode node IDs.
        self._profile_cache: dict[str, dict[str, Any]] = {}

    # ── Structured Anchor Detection (bonus signal) ────────────────────────

    @staticmethod
    def detect_anchor(content: str) -> tuple[str, str | None] | None:
        """Detect structured anchor signals in content (bonus signal).

        Returns (anchor_type, anchor_id) or None if no anchor found.
        Structured anchors provide a bonus matching signal but are NOT
        required for episode formation.
        """
        # Issue/ticket/PR IDs (most specific)
        match = _ISSUE_ID_PATTERN.search(content)
        if match:
            anchor_id = (
                match.group(1) or match.group(2)
                or match.group(3) or match.group(4)
            )
            return ("issue_id", anchor_id)

        # Release/version markers
        if _RELEASE_PATTERN.search(content):
            version_match = re.search(r"v?\d+\.\d+(?:\.\d+)?", content)
            version_id = version_match.group(0) if version_match else None
            return ("release", version_id)

        text_lower = content.lower()

        # Incident markers
        for marker in _INCIDENT_MARKERS:
            if marker in text_lower:
                return ("incident", None)

        # Migration markers
        for marker in _MIGRATION_MARKERS:
            if marker in text_lower:
                return ("migration", None)

        return None

    # ── Entity Overlap ────────────────────────────────────────────────────

    @staticmethod
    def compute_entity_overlap(
        fragment_entities: list[str],
        episode_entities: list[str],
    ) -> float:
        """Overlap coefficient: |shared| / min(|fragment|, |episode|).

        Better than Jaccard for asymmetric sets — a small fragment with 2
        entities matching 2 of an episode's 20 entities scores 1.0, which
        is correct (the fragment is fully topically covered).
        """
        if not fragment_entities or not episode_entities:
            return 0.0
        frag_set = set(fragment_entities)
        ep_set = set(episode_entities)
        shared = frag_set & ep_set
        if not shared:
            return 0.0
        return len(shared) / min(len(frag_set), len(ep_set))

    # ── Candidate Generation ─────────────────────────────────────────────

    async def _generate_candidates(
        self,
        fragment_memory: Memory,
        fragment_entity_ids: list[str],
        open_episodes: list[MemoryNode],
    ) -> dict[str, _CandidateScores]:
        """Generate episode candidates via BM25 + SPLADE + entity overlap.

        Searches existing indices for the fragment content, filtering results
        to episode backing memory IDs. Also includes episodes with entity
        overlap that didn't appear in search results.

        Returns {episode_node_id: _CandidateScores}.
        """
        cfg = self._config

        # Build memory_id → episode_node_id map
        ep_memory_map: dict[str, str] = {
            ep.memory_id: ep.id for ep in open_episodes
        }

        candidates: dict[str, _CandidateScores] = {}

        # BM25 search — find episodes whose profile matches fragment content
        if self._index is not None:
            try:
                bm25_results = self._index.search(  # type: ignore[attr-defined]
                    fragment_memory.content,
                    limit=cfg.episode_candidate_limit,
                )
                for mid, score in bm25_results:
                    if mid in ep_memory_map:
                        ep_id = ep_memory_map[mid]
                        candidates.setdefault(ep_id, _CandidateScores()).bm25 = score
            except Exception:
                logger.debug("BM25 candidate search failed", exc_info=True)

        # SPLADE search — broader semantic overlap
        if self._splade is not None:
            try:
                splade_results = self._splade.search(  # type: ignore[attr-defined]
                    fragment_memory.content,
                    limit=cfg.episode_candidate_limit,
                )
                for mid, score in splade_results:
                    if mid in ep_memory_map:
                        ep_id = ep_memory_map[mid]
                        candidates.setdefault(ep_id, _CandidateScores()).splade = score
            except Exception:
                logger.debug("SPLADE candidate search failed", exc_info=True)

        # Entity overlap — catch episodes not found via search
        if fragment_entity_ids:
            frag_entity_set = set(fragment_entity_ids)
            for ep in open_episodes:
                if ep.id not in candidates:
                    ep_entity_ids = ep.metadata.get("topic_entity_ids", [])
                    if frag_entity_set & set(ep_entity_ids):
                        candidates.setdefault(ep.id, _CandidateScores())

        return candidates

    # ── Weighted Multi-Signal Scoring ─────────────────────────────────────

    def _compute_match_score(
        self,
        fragment_memory: Memory,
        fragment_entity_ids: list[str],
        episode_node: MemoryNode,
        episode_memory: Memory | None,
        episode_member_entities: list[str],
        last_member_time: datetime | None,
        candidate_scores: _CandidateScores,
    ) -> tuple[float, dict[str, float]]:
        """Score fragment against episode using all available signals.

        Returns (score, signal_breakdown).
        """
        cfg = self._config
        now = datetime.now(UTC)
        window = timedelta(minutes=cfg.episode_window_minutes)

        # 1. BM25 score — normalize via sigmoid (BM25 is unbounded)
        bm25_norm = candidate_scores.bm25 / (1.0 + candidate_scores.bm25)

        # 2. SPLADE score — same sigmoid normalization
        splade_norm = candidate_scores.splade / (1.0 + candidate_scores.splade)

        # 3. Entity overlap coefficient
        entity_overlap = self.compute_entity_overlap(
            fragment_entity_ids, episode_member_entities,
        )

        # 4. Domain overlap
        ep_domains = episode_memory.domains if episode_memory else []
        frag_domains = fragment_memory.domains
        domain_overlap = 0.0
        if frag_domains and ep_domains:
            shared_domains = set(frag_domains) & set(ep_domains)
            if shared_domains:
                domain_overlap = len(shared_domains) / min(
                    len(frag_domains), len(ep_domains),
                )

        # 5. Temporal proximity — linear decay within window
        temporal = 0.0
        if last_member_time is not None:
            age_secs = (now - last_member_time).total_seconds()
            window_secs = window.total_seconds()
            if window_secs > 0 and age_secs <= window_secs:
                temporal = 1.0 - (age_secs / window_secs)

        # 6. Source agent match
        agent_match = 0.0
        ep_agent = episode_memory.source_agent if episode_memory else None
        if (
            fragment_memory.source_agent
            and ep_agent
            and fragment_memory.source_agent == ep_agent
        ):
            agent_match = 1.0

        # 7. Structured anchor match (bonus)
        anchor_match = 0.0
        fragment_anchor = self.detect_anchor(fragment_memory.content)
        if fragment_anchor is not None:
            ep_meta = EpisodeMeta.from_node(episode_node)
            if (
                ep_meta and ep_meta.anchor_id
                and fragment_anchor[1]
                and fragment_anchor[1] == ep_meta.anchor_id
            ):
                anchor_match = 1.0

        # Compute weighted score with redistribution when SPLADE disabled
        splade_available = self._splade is not None
        if splade_available:
            w_bm25 = cfg.episode_weight_bm25
            w_splade = cfg.episode_weight_splade
            w_entity = cfg.episode_weight_entity_overlap
            w_domain = cfg.episode_weight_domain
            w_temporal = cfg.episode_weight_temporal
            w_agent = cfg.episode_weight_agent
            w_anchor = cfg.episode_weight_anchor
        else:
            # Redistribute SPLADE weight proportionally
            non_splade_total = (
                cfg.episode_weight_bm25 + cfg.episode_weight_entity_overlap
                + cfg.episode_weight_domain + cfg.episode_weight_temporal
                + cfg.episode_weight_agent + cfg.episode_weight_anchor
            )
            scale = 1.0 / non_splade_total if non_splade_total > 0 else 1.0
            w_bm25 = cfg.episode_weight_bm25 * scale
            w_splade = 0.0
            w_entity = cfg.episode_weight_entity_overlap * scale
            w_domain = cfg.episode_weight_domain * scale
            w_temporal = cfg.episode_weight_temporal * scale
            w_agent = cfg.episode_weight_agent * scale
            w_anchor = cfg.episode_weight_anchor * scale

        score = (
            w_bm25 * bm25_norm
            + w_splade * splade_norm
            + w_entity * entity_overlap
            + w_domain * domain_overlap
            + w_temporal * temporal
            + w_agent * agent_match
            + w_anchor * anchor_match
        )

        breakdown = {
            "bm25": round(w_bm25 * bm25_norm, 4),
            "splade": round(w_splade * splade_norm, 4),
            "entity_overlap": round(w_entity * entity_overlap, 4),
            "domain_overlap": round(w_domain * domain_overlap, 4),
            "temporal": round(w_temporal * temporal, 4),
            "agent_match": round(w_agent * agent_match, 4),
            "anchor_match": round(w_anchor * anchor_match, 4),
            "total": round(score, 4),
        }

        return score, breakdown

    # ── Episode Profile Cache ──────────────────────────────────────────

    async def _ensure_profile_cache(
        self, open_episodes: list[MemoryNode],
    ) -> None:
        """Batch-load profiles for all open episodes into the cache.

        Called at the start of each ``assign_or_create()`` invocation.
        The cache is populated per-call via batch queries (replacing the
        old per-candidate sequential queries).  Episodes already present
        in the cache from a previous call are refreshed to pick up any
        membership changes.
        """
        ep_ids = [ep.id for ep in open_episodes]
        if not ep_ids:
            self._profile_cache.clear()
            return

        # Batch-load members and entities for all open episodes
        try:
            members_map = await self._store.get_episode_members_batch(ep_ids)  # type: ignore[attr-defined]
        except AttributeError:
            members_map = {}
            for eid in ep_ids:
                members_map[eid] = await self._store.get_episode_members(eid)  # type: ignore[attr-defined]

        try:
            entities_map = await self._store.get_episode_member_entities_batch(ep_ids)  # type: ignore[attr-defined]
        except AttributeError:
            entities_map = {}
            for eid in ep_ids:
                entities_map[eid] = await self._collect_member_entities(
                    members_map.get(eid, []),
                )

        memory_ids = [
            ep.memory_id for ep in open_episodes if ep.memory_id
        ]
        try:
            memories_map = await self._store.get_memories_batch(memory_ids)  # type: ignore[attr-defined]
        except AttributeError:
            memories_map = {}
            for mid in memory_ids:
                mem = await self._store.get_memory(mid)  # type: ignore[attr-defined]
                if mem:
                    memories_map[mid] = mem

        for ep in open_episodes:
            members = members_map.get(ep.id, [])
            self._profile_cache[ep.id] = {
                "memory": memories_map.get(ep.memory_id) if ep.memory_id else None,
                "members": members,
                "member_entities": entities_map.get(ep.id, []),
                "last_member_time": self._get_last_member_time(members),
            }


    def _invalidate_profile(self, episode_id: str) -> None:
        """Remove a single episode from the profile cache."""
        self._profile_cache.pop(episode_id, None)

    # ── Main Entry Point ─────────────────────────────────────────────────

    async def assign_or_create(
        self,
        fragment_node: MemoryNode,
        fragment_memory: Memory,
        entity_ids: list[str],
    ) -> MemoryNode | None:
        """Assign fragment to an existing episode or create a new one.

        Algorithm:
        1. Generate candidates via BM25 + SPLADE search + entity overlap
        2. Score each candidate with weighted multi-signal matching
        3. Best score >= threshold → assign to that episode
        4. No match + >= min_entities → create new episode
        5. Otherwise → return None (fragment stays unattached)

        Args:
            fragment_node: The MemoryNode for the fragment (already saved).
            fragment_memory: The Memory record for the fragment.
            entity_ids: Entity IDs extracted from the fragment content.

        Returns:
            The episode MemoryNode (existing or newly created), or None.
        """
        open_episodes = await self._store.get_open_episodes()  # type: ignore[attr-defined]

        # Populate profile cache on first call (batch-loads all open
        # episode data in a few queries instead of per-candidate).
        await self._ensure_profile_cache(open_episodes)

        # Generate candidates via BM25 + SPLADE + entity overlap
        candidates = await self._generate_candidates(
            fragment_memory, entity_ids, open_episodes,
        )

        # Build episode lookup for scoring
        ep_lookup = {ep.id: ep for ep in open_episodes}

        best_ep: MemoryNode | None = None
        best_score = 0.0

        for ep_id, candidate_scores in candidates.items():
            ep_node = ep_lookup.get(ep_id)
            if ep_node is None:
                continue

            # Use cached profile data (avoids per-candidate DB queries)
            profile = self._profile_cache.get(ep_id, {})
            ep_memory = profile.get("memory")
            ep_member_entities = profile.get("member_entities", [])
            last_member_time = profile.get("last_member_time")

            score, breakdown = self._compute_match_score(
                fragment_memory=fragment_memory,
                fragment_entity_ids=entity_ids,
                episode_node=ep_node,
                episode_memory=ep_memory,
                episode_member_entities=ep_member_entities,
                last_member_time=last_member_time,
                candidate_scores=candidate_scores,
            )

            if score > best_score:
                best_score = score
                best_ep = ep_node

        # Assign to best matching episode if above threshold
        if best_ep is not None and best_score >= self._config.episode_match_threshold:
            # Resolve entity names for profile update
            entity_names = await self._resolve_entity_names(entity_ids)

            await self._assign_to_episode(
                fragment_node, best_ep,
                match_score=best_score,
            )

            # Enrich episode profile with new member's entities
            await self._update_episode_profile(
                best_ep, entity_names, fragment_memory.domains,
            )

            return best_ep

        # LLM fallback: when heuristic scoring misses, ask LLM to suggest links
        if (
            self._config.episode_llm_fallback_enabled
            and open_episodes
            and (best_ep is None or best_score < self._config.episode_match_threshold)
        ):
            llm_ep = await self._try_llm_episode_link(
                fragment_node, fragment_memory, entity_ids,
                open_episodes, ep_lookup,
            )
            if llm_ep is not None:
                return llm_ep

        # Create new episode if fragment has enough entities
        if len(entity_ids) >= self._config.episode_create_min_entities:
            entity_names = await self._resolve_entity_names(entity_ids)
            anchor = self.detect_anchor(fragment_memory.content)

            if anchor:
                anchor_type = f"structured:{anchor[0]}"
                anchor_id = anchor[1]
            else:
                anchor_type = "entity_cluster"
                anchor_id = self._make_topic_key(entity_names)

            return await self._create_episode(
                fragment_node, fragment_memory,
                anchor_type=anchor_type,
                anchor_id=anchor_id,
                topic_entities=entity_names,
                topic_entity_ids=entity_ids,
            )

        return None

    # ── LLM Fallback ──────────────────────────────────────────────────────

    async def _try_llm_episode_link(
        self,
        fragment_node: MemoryNode,
        fragment_memory: Memory,
        entity_ids: list[str],
        open_episodes: list[MemoryNode],
        ep_lookup: dict[str, MemoryNode],
    ) -> MemoryNode | None:
        """Try LLM fallback to find an episode match.

        Calls the LLM with fragment + episode summaries.  On success,
        assigns the fragment to the suggested episode.  On failure or
        no-match, logs a miss event and returns None.
        """
        from ncms.infrastructure.llm.episode_linker_llm import suggest_episode_links

        # Build episode summaries for the LLM prompt
        entity_names = await self._resolve_entity_names(entity_ids)
        summaries: list[dict[str, str]] = []
        for ep in open_episodes[:5]:
            meta = EpisodeMeta.model_validate(ep.metadata or {})
            summaries.append({
                "id": ep.id,
                "topic": meta.episode_title,
                "entities": ", ".join(meta.topic_entities[:10]),
                "domains": ", ".join(ep.metadata.get("domains", [])[:5])
                if ep.metadata else "",
            })

        suggestions = await suggest_episode_links(
            fragment_content=fragment_memory.content,
            fragment_entities=entity_names,
            fragment_domains=fragment_memory.domains,
            fragment_agent=fragment_memory.source_agent,
            episode_summaries=summaries,
            model=self._config.llm_model,
            api_base=self._config.llm_api_base,
        )

        # Find the highest-confidence valid suggestion
        best_suggestion = None
        best_confidence = -1.0
        for s in suggestions:
            ep_id = str(s["episode_id"])
            raw_conf = s["confidence"]
            confidence = float(raw_conf) if isinstance(raw_conf, (int, float, str)) else 0.0
            if ep_id in ep_lookup and confidence > best_confidence:
                best_suggestion = s
                best_confidence = confidence

        if best_suggestion is not None:
            ep_id = str(best_suggestion["episode_id"])
            ep_node = ep_lookup[ep_id]
            logger.info(
                "LLM fallback linked fragment %s to episode %s (confidence=%.2f)",
                fragment_node.id, ep_id, best_confidence,
            )
            await self._assign_to_episode(
                fragment_node, ep_node,
                match_score=best_confidence,
            )
            await self._update_episode_profile(
                ep_node, entity_names, fragment_memory.domains,
            )
            return ep_node

        # Log miss for tuning
        logger.debug(
            "Episode LLM fallback found no match for fragment %s", fragment_node.id,
        )
        return None

    # ── Episode Creation ─────────────────────────────────────────────────

    async def _create_episode(
        self,
        first_fragment: MemoryNode,
        fragment_memory: Memory,
        anchor_type: str,
        anchor_id: str | None,
        topic_entities: list[str] | None = None,
        topic_entity_ids: list[str] | None = None,
    ) -> MemoryNode:
        """Create a new episode from a fragment."""
        entities = topic_entities or []
        entity_ids = topic_entity_ids or []
        domain_str = fragment_memory.domains[0] if fragment_memory.domains else "general"

        # Generate title from entities + domain
        if anchor_type.startswith("structured:") and anchor_id:
            entities_str = ", ".join(entities[:3])
            title = f"Episode: {anchor_id} {entities_str} [{domain_str}]"
        elif entities:
            entities_str = ", ".join(entities[:3])
            title = f"Episode: {entities_str} [{domain_str}]"
        else:
            title = f"Episode: {fragment_memory.content[:60]} [{domain_str}]"

        # Build searchable episode profile content
        profile = self._build_profile_content(
            entities, fragment_memory.domains,
            {"anchor_id": anchor_id},
        )

        # Create backing Memory record (indexed for BM25/SPLADE searchability)
        episode_memory = Memory(
            content=profile,
            type="fact",
            domains=fragment_memory.domains,
            source_agent=fragment_memory.source_agent,
            tags=["episode"],
        )
        await self._store.save_memory(episode_memory)  # type: ignore[attr-defined]

        # Index in Tantivy for BM25 search
        if self._index is not None:
            try:
                self._index.index_memory(episode_memory)  # type: ignore[attr-defined]
            except Exception:
                logger.debug(
                    "Failed to index episode memory %s", episode_memory.id,
                    exc_info=True,
                )

        # Index in SPLADE for semantic search
        if self._splade is not None:
            try:
                self._splade.index_memory(episode_memory)  # type: ignore[attr-defined]
            except Exception:
                logger.debug(
                    "Failed to SPLADE-index episode memory %s", episode_memory.id,
                    exc_info=True,
                )

        # Create episode MemoryNode
        episode_meta: dict[str, Any] = {
            "episode_title": title,
            "status": EpisodeStatus.OPEN.value,
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "topic_entities": entities,
            "topic_entity_ids": entity_ids,
            "member_count": 1,
        }
        episode_node = MemoryNode(
            memory_id=episode_memory.id,
            node_type=NodeType.EPISODE,
            importance=fragment_memory.importance,
            metadata=episode_meta,
        )
        await self._store.save_memory_node(episode_node)  # type: ignore[attr-defined]

        # Assign the first fragment
        await self._assign_to_episode(
            first_fragment, episode_node, increment=False,
            match_score=0.0,
        )

        self._event_log.episode_created(  # type: ignore[attr-defined]
            episode_id=episode_node.id,
            title=title,
            anchor_type=anchor_type,
            agent_id=fragment_memory.source_agent,
        )

        # Seed the profile cache for this new episode so subsequent
        # assign_or_create calls can score against it without a DB reload.
        self._profile_cache[episode_node.id] = {
            "memory": episode_memory,
            "members": [first_fragment],
            "member_entities": topic_entity_ids or [],
            "last_member_time": first_fragment.created_at,
        }

        logger.info("Created episode %s: %s", episode_node.id, title)
        return episode_node

    # ── Episode Assignment ───────────────────────────────────────────────

    async def _assign_to_episode(
        self,
        fragment_node: MemoryNode,
        episode_node: MemoryNode,
        *,
        increment: bool = True,
        match_score: float = 0.0,
    ) -> None:
        """Assign a fragment to an episode (set parent_id + create edge)."""
        # Set parent_id on the fragment
        fragment_node.parent_id = episode_node.id
        await self._store.update_memory_node(fragment_node)  # type: ignore[attr-defined]

        # Create BELONGS_TO_EPISODE edge: fragment → episode
        edge = GraphEdge(
            source_id=fragment_node.id,
            target_id=episode_node.id,
            edge_type=EdgeType.BELONGS_TO_EPISODE,
            metadata={
                "assigned_at": datetime.now(UTC).isoformat(),
                "match_score": round(match_score, 4),
            },
        )
        await self._store.save_graph_edge(edge)  # type: ignore[attr-defined]

        # Increment member count on episode
        if increment:
            ep_meta = dict(episode_node.metadata)
            ep_meta["member_count"] = ep_meta.get("member_count", 0) + 1
            episode_node.metadata = ep_meta
            await self._store.update_memory_node(episode_node)  # type: ignore[attr-defined]

        # Invalidate cached profile so next scoring picks up new member
        self._invalidate_profile(episode_node.id)

        self._event_log.episode_assigned(  # type: ignore[attr-defined]
            episode_id=episode_node.id,
            fragment_id=fragment_node.id,
            signals_count=int(match_score * 100),
            match_score=match_score,
        )

        logger.debug(
            "Assigned fragment %s to episode %s (score=%.3f)",
            fragment_node.id, episode_node.id, match_score,
        )

    # ── Episode Profile Management ────────────────────────────────────────

    async def _update_episode_profile(
        self,
        episode_node: MemoryNode,
        new_entity_names: list[str],
        new_domains: list[str],
    ) -> None:
        """Enrich episode backing memory with new member's entities and re-index."""
        current_entities = list(episode_node.metadata.get("topic_entities", []))
        current_set = {e.lower() for e in current_entities}
        new_unique = [n for n in new_entity_names if n.lower() not in current_set]

        if not new_unique:
            return

        # Update metadata
        all_entities = current_entities + new_unique
        ep_meta = dict(episode_node.metadata)
        ep_meta["topic_entities"] = all_entities
        episode_node.metadata = ep_meta

        # Rebuild profile content
        existing_domains: list[str] = []
        if episode_node.memory_id:
            ep_mem = await self._store.get_memory(episode_node.memory_id)  # type: ignore[attr-defined]
            if ep_mem is not None:
                existing_domains = ep_mem.domains
        all_domains = list(set(new_domains + existing_domains))
        profile = self._build_profile_content(all_entities, all_domains, ep_meta)

        # Update backing memory content
        ep_memory = await self._store.get_memory(  # type: ignore[attr-defined]
            episode_node.memory_id,
        )
        if ep_memory:
            ep_memory.content = profile
            await self._store.update_memory(ep_memory)  # type: ignore[attr-defined]

            # Re-index in Tantivy (must remove first to avoid duplicates)
            if self._index is not None:
                try:
                    self._index.remove(ep_memory.id)  # type: ignore[attr-defined]
                    self._index.index_memory(ep_memory)  # type: ignore[attr-defined]
                except Exception:
                    logger.debug("Failed to re-index episode profile", exc_info=True)

            # Re-index in SPLADE (auto-replaces)
            if self._splade is not None:
                try:
                    self._splade.index_memory(ep_memory)  # type: ignore[attr-defined]
                except Exception:
                    logger.debug("Failed to SPLADE re-index episode profile", exc_info=True)

        # Persist updated metadata
        await self._store.update_memory_node(episode_node)  # type: ignore[attr-defined]

        # Invalidate cached profile so next scoring picks up enriched data
        self._invalidate_profile(episode_node.id)

    @staticmethod
    def _build_profile_content(
        entities: list[str],
        domains: list[str],
        metadata: dict[str, Any],
    ) -> str:
        """Build searchable episode profile text from entities + domains + anchors."""
        parts: list[str] = []
        if entities:
            parts.append(", ".join(entities[:20]))
        if domains:
            parts.append(f"domains: {', '.join(sorted(set(domains)))}")
        anchor_id = metadata.get("anchor_id")
        if anchor_id:
            parts.append(f"anchors: {anchor_id}")
        return " | ".join(parts) if parts else "episode"

    # ── Episode Closure ──────────────────────────────────────────────────

    async def close_stale_episodes(self) -> list[str]:
        """Close episodes with no new members within T_close.

        Returns list of closed episode IDs.
        """
        open_episodes = await self._store.get_open_episodes()  # type: ignore[attr-defined]
        close_window = timedelta(minutes=self._config.episode_close_minutes)
        now = datetime.now(UTC)
        closed_ids: list[str] = []

        for ep_node in open_episodes:
            members = await self._store.get_episode_members(  # type: ignore[attr-defined]
                ep_node.id,
            )
            last_time = self._get_last_member_time(members)

            if last_time is None:
                # Episode with no members — use episode creation time
                last_time = ep_node.created_at

            if (now - last_time) > close_window:
                await self._close_episode(ep_node, reason="timeout")
                closed_ids.append(ep_node.id)

        return closed_ids

    async def check_resolution_closure(
        self, fragment_content: str, episode_node: MemoryNode,
    ) -> bool:
        """Check if fragment content contains resolution markers.

        If so, close the episode. Returns True if closed.
        """
        content_lower = fragment_content.lower()
        for marker in _RESOLUTION_MARKERS:
            if marker in content_lower:
                await self._close_episode(
                    episode_node, reason=f"resolution: {marker}",
                )
                return True
        return False

    async def _close_episode(
        self, episode_node: MemoryNode, reason: str,
    ) -> None:
        """Close an episode by updating its metadata."""
        ep_meta = dict(episode_node.metadata)
        ep_meta["status"] = EpisodeStatus.CLOSED.value
        ep_meta["closed_reason"] = reason
        ep_meta["closed_at"] = datetime.now(UTC).isoformat()
        episode_node.metadata = ep_meta
        await self._store.update_memory_node(episode_node)  # type: ignore[attr-defined]

        # Remove from profile cache (no longer open)
        self._invalidate_profile(episode_node.id)

        member_count = ep_meta.get("member_count", 0)
        self._event_log.episode_closed(  # type: ignore[attr-defined]
            episode_id=episode_node.id,
            reason=reason,
            member_count=member_count,
        )
        logger.info(
            "Closed episode %s (reason=%s, members=%d)",
            episode_node.id, reason, member_count,
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _collect_member_entities(
        self, members: list[MemoryNode],
    ) -> list[str]:
        """Collect all entity IDs linked to episode member memories."""
        all_entity_ids: list[str] = []
        seen: set[str] = set()
        for member in members:
            eids = await self._store.get_memory_entities(  # type: ignore[attr-defined]
                member.memory_id,
            )
            for eid in eids:
                if eid not in seen:
                    all_entity_ids.append(eid)
                    seen.add(eid)
        return all_entity_ids

    async def _resolve_entity_names(self, entity_ids: list[str]) -> list[str]:
        """Resolve entity IDs to entity names via store."""
        names: list[str] = []
        for eid in entity_ids:
            entity = await self._store.get_entity(eid)  # type: ignore[attr-defined]
            if entity:
                names.append(entity.name)
        return names

    @staticmethod
    def _make_topic_key(entity_names: list[str]) -> str:
        """Generate a deterministic topic key from sorted entity names."""
        sorted_names = sorted(set(n.lower() for n in entity_names))
        return "+".join(sorted_names[:5])  # Cap at 5 for readability

    @staticmethod
    def _get_last_member_time(members: list[MemoryNode]) -> datetime | None:
        """Get the most recent created_at among members."""
        if not members:
            return None
        return max(m.created_at for m in members)
