"""Helpers for :meth:`MemoryService.store_memory`.

Extracted from ``memory_service.py`` in the Phase F MI cleanup so the
orchestrator stays under the A-grade maintainability bar.

Public entry points:

* :func:`auto_populate_topic_domain` — append SLM topic to domains
* :func:`bake_intent_slot_payload`  — fold SLM outputs into structured
* :func:`run_slm_and_admission`     — SLM extraction + admission gate
* :func:`finalize_inline_store`     — tail of inline ingestion path
* :func:`try_enqueue_indexing`      — async-pool enqueue or fall back

**Design principle**: every collaborator the helper reaches for is
named in the signature.  No ``svc=...`` whole-service injection — the
parameter list documents the dependency surface (and makes each
helper independently testable).  When a helper genuinely uses many
collaborators (e.g. :func:`finalize_inline_store` touches 7), every
one is still named — the long signature signals "this is a
pipeline-tail orchestrator", which is useful information.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from ncms.domain.models import AccessRecord, Memory, Relationship

logger = logging.getLogger(__name__)


def auto_populate_topic_domain(
    *,
    config,
    intent_slot_label: Any,
    domains: list[str] | None,
) -> list[str] | None:
    """Auto-append SLM topic-head label to ``Memory.domains``.

    Replaces the "user hands us a domain string" flow with
    "SLM-classifies-content-against-learned-taxonomy" when
    ``slm_populate_domains`` is enabled.  Topic must clear the
    confidence floor.
    """
    if (
        intent_slot_label.topic is None
        or not intent_slot_label.is_topic_confident(config.slm_confidence_threshold)
        or not config.slm_populate_domains
    ):
        return domains
    result = list(domains or [])
    if intent_slot_label.topic not in result:
        result.append(intent_slot_label.topic)
    return result


def bake_intent_slot_payload(
    *,
    intent_slot_label: Any,
    structured: dict | None,
) -> dict:
    """Serialise the SLM extraction output into ``memory.structured``.

    Threads role_spans (v7+) and cue_tags (v8+) through as JSON-ready
    list[dict] so the ingest pipeline downstream consumers (L2
    ENTITY_STATE builder, ``_extract_and_persist_causal_edges``) can
    read them without per-row conversion.
    """
    result = dict(structured or {})
    result["intent_slot"] = {
        "intent": intent_slot_label.intent,
        "intent_confidence": intent_slot_label.intent_confidence,
        "topic": intent_slot_label.topic,
        "topic_confidence": intent_slot_label.topic_confidence,
        "admission": intent_slot_label.admission,
        "admission_confidence": intent_slot_label.admission_confidence,
        "state_change": intent_slot_label.state_change,
        "state_change_confidence": intent_slot_label.state_change_confidence,
        "method": intent_slot_label.method,
        "latency_ms": intent_slot_label.latency_ms,
        "role_spans": [dict(r) for r in getattr(intent_slot_label, "role_spans", ()) or ()],
        "slots": dict(getattr(intent_slot_label, "slots", {}) or {}),
        "cue_tags": list(getattr(intent_slot_label, "cue_tags", ()) or ()),
    }
    return result


async def run_slm_and_admission(
    *,
    config,
    ingestion,
    admission,
    content: str,
    domains: list[str] | None,
    tags: list[str] | None,
    source_agent: str | None,
    project: str | None,
    memory_type: str,
    importance: float,
    structured: dict | None,
    emit_stage: Callable,
    pipeline_start: float,
):
    """Run SLM extraction + admission gate in sequence.

    Returns a ``Memory`` for early-exit (admission discard / ephemeral)
    or a tuple ``(intent_slot_label, domains, admission_route,
    admission_features, structured)`` to continue the persist path.
    """
    domain_hint = (domains or [""])[0]
    intent_slot_label = await ingestion.run_intent_slot_extraction(
        content,
        domain=domain_hint,
    )
    if intent_slot_label is not None:
        emit_stage(
            "intent_slot",
            intent_slot_label.latency_ms,
            {
                "method": intent_slot_label.method,
                "intent": intent_slot_label.intent,
                "topic": intent_slot_label.topic,
                "admission": intent_slot_label.admission,
                "state_change": intent_slot_label.state_change,
                "n_slots": len(intent_slot_label.slots),
            },
        )
        domains = auto_populate_topic_domain(
            config=config, intent_slot_label=intent_slot_label, domains=domains
        )

    admission_route: str | None = None
    admission_features: object | None = None
    if admission is not None and config.admission_enabled:
        result = await ingestion.gate_admission(
            content=content,
            domains=domains,
            tags=tags,
            source_agent=source_agent,
            project=project,
            memory_type=memory_type,
            importance=importance,
            structured=structured,
            intent_slot_label=intent_slot_label,
            emit_stage=emit_stage,
            pipeline_start=pipeline_start,
        )
        if isinstance(result, Memory):
            return result
        admission_route, admission_features, structured = result

    if intent_slot_label is not None:
        structured = bake_intent_slot_payload(
            intent_slot_label=intent_slot_label, structured=structured
        )

    return (
        intent_slot_label,
        domains,
        admission_route,
        admission_features,
        structured,
    )


async def finalize_inline_store(
    *,
    store,
    graph,
    event_log,
    config,
    ingestion,
    episode,
    tlg_vocab_cache,
    memory: Memory,
    content: str,
    memory_type: str,
    relationships: list[dict] | None,
    all_entities: list[dict],
    linked_entity_ids: list[str],
    admission_route: str | None,
    admission_features: object | None,
    source_agent: str | None,
    subject: str | None,
    pipeline_id: str,
    pipeline_start: float,
    emit_stage: Callable,
) -> None:
    """Tail of the inline-indexing path: contradictions + edges +
    access log + L1/L2 nodes + completion event + memory.stored.

    Long signature is intentional — every collaborator is named so
    the helper is independently testable and the dependency surface
    is visible at the call site.
    """
    if config.contradiction_detection_enabled:
        asyncio.create_task(
            ingestion.deferred_contradiction_check(
                memory=memory,
                all_entities=all_entities,
                pipeline_id=pipeline_id,
                source_agent=source_agent,
            )
        )

    if relationships:
        for r_data in relationships:
            rel = Relationship(
                source_entity_id=r_data["source"],
                target_entity_id=r_data["target"],
                type=r_data.get("type", "related_to"),
                source_memory_id=memory.id,
            )
            await store.save_relationship(rel)
            graph.add_relationship(rel)

    await store.log_access(
        AccessRecord(memory_id=memory.id, accessing_agent=source_agent),
    )

    total_ms = (time.perf_counter() - pipeline_start) * 1000
    emit_stage(
        "complete",
        total_ms,
        {
            "memory_id": memory.id,
            "entity_count": len(all_entities),
            "total_duration_ms": round(total_ms, 2),
        },
        memory_id=memory.id,
    )

    should_create_node = (
        admission_route == "persist"
        or admission_route is None
        or (config.temporal_enabled and episode is not None)
    )
    if should_create_node:
        try:
            await ingestion.create_memory_nodes(
                memory=memory,
                content=content,
                all_entities=all_entities,
                linked_entity_ids=linked_entity_ids,
                admission_features=admission_features,
                emit_stage=emit_stage,
                subject=subject,
            )
        except Exception:
            logger.warning(
                "MemoryNode creation failed for %s, continuing", memory.id, exc_info=True
            )
        if config.temporal_enabled:
            tlg_vocab_cache.invalidate()

    logger.info("Stored memory %s: %s", memory.id, content[:80])
    event_log.memory_stored(
        memory_id=memory.id,
        content_preview=content,
        memory_type=memory_type,
        domains=memory.domains,
        entity_count=len(all_entities),
        agent_id=source_agent,
    )


def try_enqueue_indexing(
    *,
    index_pool,
    memory: Memory,
    content: str,
    memory_type: str,
    domains: list[str] | None,
    tags: list[str] | None,
    source_agent: str | None,
    importance: float,
    entities: list[dict] | None,
    relationships: list[dict] | None,
    admission_features: object | None,
    admission_route: str | None,
    pipeline_start: float,
    emit_stage: Callable,
    subject: str | None = None,
    slot_entities_present: bool = False,
) -> bool:
    """Try to hand indexing off to the background worker pool.

    Returns True if accepted (caller returns immediately), False if
    pool is absent or queue full (caller falls through to inline).
    """
    if index_pool is None:
        return False

    from ncms.application.index_worker import IndexTask

    task = IndexTask(
        memory_id=memory.id,
        content=content,
        memory_type=memory_type,
        domains=domains or [],
        tags=tags or [],
        source_agent=source_agent,
        importance=importance,
        entities_manual=list(entities or []),
        relationships=list(relationships or []),
        admission_features=admission_features,
        admission_route=admission_route,
        subject=subject,
        slot_entities_present=slot_entities_present,
    )
    enqueued = index_pool.enqueue(task)
    if not enqueued:
        logger.warning("Index queue full, falling back to inline for %s", memory.id)
        return False

    emit_stage(
        "enqueued",
        (time.perf_counter() - pipeline_start) * 1000,
        {
            "task_id": task.task_id,
            "queue_depth": index_pool.stats().queue_depth,
        },
        memory_id=memory.id,
    )
    memory.structured = {**(memory.structured or {}), "indexing": "queued"}
    logger.info("Stored+enqueued memory %s: %s", memory.id, content[:80])
    return True
