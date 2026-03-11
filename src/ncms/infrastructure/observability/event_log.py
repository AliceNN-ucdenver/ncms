"""EventLog - ring buffer event log with async SSE subscriber support.

Captures events from the Knowledge Bus and Memory Service.
Subscribers receive events in real-time via async generators (for SSE streaming).
Zero external dependencies — pure asyncio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DashboardEvent:
    """A single observable event in the NCMS system."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    type: str = ""  # e.g. "agent.registered", "bus.ask", "memory.stored"
    agent_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Format as a Server-Sent Event string."""
        payload = json.dumps(asdict(self), default=str)
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


class EventLog:
    """Ring buffer event log with async subscriber support.

    Events are stored in a bounded deque (default 2000).
    SSE subscribers receive a copy of each event via an asyncio.Queue.
    """

    def __init__(self, max_events: int = 2000) -> None:
        self._events: deque[DashboardEvent] = deque(maxlen=max_events)
        self._subscribers: list[asyncio.Queue[DashboardEvent]] = []
        self._lock = asyncio.Lock()

    def emit(self, event: DashboardEvent) -> None:
        """Append event to the log and notify all subscribers."""
        self._events.append(event)
        dead: list[asyncio.Queue[DashboardEvent]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        # Remove overflowed subscribers
        for q in dead:
            self._subscribers.remove(q)

    # ── Convenience emitters ──────────────────────────────────────────────

    def agent_registered(
        self, agent_id: str, domains: list[str],
    ) -> None:
        self.emit(DashboardEvent(
            type="agent.registered",
            agent_id=agent_id,
            data={"domains": domains},
        ))

    def agent_deregistered(self, agent_id: str) -> None:
        self.emit(DashboardEvent(
            type="agent.deregistered",
            agent_id=agent_id,
        ))

    def agent_status(self, agent_id: str, status: str) -> None:
        self.emit(DashboardEvent(
            type="agent.status",
            agent_id=agent_id,
            data={"status": status},
        ))

    def bus_ask(
        self,
        ask_id: str,
        from_agent: str,
        question: str,
        domains: list[str],
        targets: list[str],
    ) -> None:
        self.emit(DashboardEvent(
            type="bus.ask",
            agent_id=from_agent,
            data={
                "ask_id": ask_id,
                "question": question[:200],
                "domains": domains,
                "targets": targets,
            },
        ))

    def bus_response(
        self,
        ask_id: str,
        from_agent: str,
        source_mode: str,
        confidence: float,
        answer: str = "",
    ) -> None:
        self.emit(DashboardEvent(
            type="bus.response",
            agent_id=from_agent,
            data={
                "ask_id": ask_id,
                "source_mode": source_mode,
                "confidence": round(confidence, 3),
                "answer": answer[:200],
            },
        ))

    def bus_announce(
        self,
        announce_id: str,
        from_agent: str,
        event: str,
        domains: list[str],
        severity: str,
        recipients: list[str],
        content: str = "",
    ) -> None:
        self.emit(DashboardEvent(
            type="bus.announce",
            agent_id=from_agent,
            data={
                "announce_id": announce_id,
                "event": event,
                "domains": domains,
                "severity": severity,
                "recipients": recipients,
                "content": content[:200],
            },
        ))

    def bus_surrogate(
        self,
        ask_id: str,
        from_agent: str,
        confidence: float,
        snapshot_age_seconds: float | None,
        answer: str = "",
    ) -> None:
        self.emit(DashboardEvent(
            type="bus.surrogate",
            agent_id=from_agent,
            data={
                "ask_id": ask_id,
                "confidence": round(confidence, 3),
                "snapshot_age_seconds": snapshot_age_seconds,
                "answer": answer[:200],
            },
        ))

    def memory_stored(
        self,
        memory_id: str,
        content_preview: str,
        memory_type: str,
        domains: list[str],
        entity_count: int,
        agent_id: str | None = None,
    ) -> None:
        self.emit(DashboardEvent(
            type="memory.stored",
            agent_id=agent_id,
            data={
                "memory_id": memory_id,
                "content": content_preview[:120],
                "type": memory_type,
                "domains": domains,
                "entity_count": entity_count,
            },
        ))

    def pipeline_stage(
        self,
        pipeline_id: str,
        pipeline_type: str,
        stage: str,
        duration_ms: float,
        data: dict[str, Any] | None = None,
        agent_id: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        """Emit a pipeline stage event for store/search observability."""
        event_data: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "pipeline_type": pipeline_type,
            "stage": stage,
            "duration_ms": round(duration_ms, 2),
        }
        if memory_id:
            event_data["memory_id"] = memory_id
        if data:
            event_data.update(data)
        self.emit(DashboardEvent(
            type=f"pipeline.{pipeline_type}.{stage}",
            agent_id=agent_id,
            data=event_data,
        ))

    def memory_searched(
        self,
        query: str,
        result_count: int,
        top_score: float | None,
        agent_id: str | None = None,
    ) -> None:
        self.emit(DashboardEvent(
            type="memory.searched",
            agent_id=agent_id,
            data={
                "query": query[:200],
                "result_count": result_count,
                "top_score": round(top_score, 3) if top_score else None,
            },
        ))

    # ── Query ─────────────────────────────────────────────────────────────

    def recent(self, limit: int = 100) -> list[DashboardEvent]:
        """Return the most recent events (newest first)."""
        events = list(self._events)
        events.reverse()
        return events[:limit]

    def count(self) -> int:
        return len(self._events)

    # ── SSE Subscription ──────────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue[DashboardEvent]:
        """Create a new subscriber queue. Caller should read from it in a loop."""
        queue: asyncio.Queue[DashboardEvent] = asyncio.Queue(maxsize=500)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DashboardEvent]) -> None:
        """Remove a subscriber queue."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)
