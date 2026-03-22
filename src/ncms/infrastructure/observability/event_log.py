"""EventLog - ring buffer event log with async SSE subscriber support.

Captures events from the Knowledge Bus and Memory Service.
Subscribers receive events in real-time via async generators (for SSE streaming).
Optionally persists events to SQLite for historical replay / time-travel debugging.
Zero external dependencies — pure asyncio.
"""

from __future__ import annotations

import asyncio
import contextlib
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


class NullEventLog:
    """No-op event log that silently discards all events.

    Drop-in replacement for :class:`EventLog` when dashboard observability
    is not needed.  Eliminates ``if self._event_log:`` null checks in
    application services — callers can always call methods unconditionally.
    """

    def __getattr__(self, name: str) -> object:
        """Return a no-op callable for any method."""
        return _noop


def _noop(*args: object, **kwargs: object) -> None:
    """Shared no-op function for NullEventLog method calls."""


class EventLog:
    """Ring buffer event log with async subscriber support.

    Events are stored in a bounded deque (default 2000).
    SSE subscribers receive a copy of each event via an asyncio.Queue.
    Optionally persists events to SQLite for time-travel replay.
    """

    def __init__(self, max_events: int = 2000, db: object | None = None) -> None:
        self._events: deque[DashboardEvent] = deque(maxlen=max_events)
        self._subscribers: list[asyncio.Queue[DashboardEvent]] = []
        self._lock = asyncio.Lock()
        self._db = db  # aiosqlite.Connection or None
        self._write_queue: asyncio.Queue[DashboardEvent] = asyncio.Queue(maxsize=10000)
        self._persist_task: asyncio.Task[None] | None = None

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
        # Queue for SQLite persistence (non-blocking, drop on overflow)
        if self._db is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._write_queue.put_nowait(event)

    async def start_persistence(self) -> None:
        """Background coroutine that drains the write queue into SQLite.

        Call as an asyncio task. Batches up to 50 inserts at a time.
        """
        if self._db is None:
            return
        while True:
            try:
                # Wait for at least one event
                event = await self._write_queue.get()
                batch = [event]
                # Drain up to 49 more without waiting
                for _ in range(49):
                    try:
                        batch.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self._persist_batch(batch)
            except asyncio.CancelledError:
                # Flush remaining
                remaining: list[DashboardEvent] = []
                while not self._write_queue.empty():
                    try:
                        remaining.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if remaining:
                    await self._persist_batch(remaining)
                return
            except Exception:
                logger.exception("Event persistence error")

    async def _persist_batch(self, batch: list[DashboardEvent]) -> None:
        """Insert a batch of events into the dashboard_events table."""
        if not self._db or not batch:
            return
        try:
            await self._db.executemany(
                "INSERT OR IGNORE INTO dashboard_events"
                " (id, timestamp, type, agent_id, data)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        e.id,
                        e.timestamp,
                        e.type,
                        e.agent_id,
                        json.dumps(e.data, default=str),
                    )
                    for e in batch
                ],
            )
            await self._db.commit()
        except Exception:
            logger.exception("Failed to persist %d events", len(batch))

    # ── In-Memory Persistent List ─────────────────────────────────────

    @property
    def event_count(self) -> int:
        """Total number of events in the ring buffer."""
        return len(self._events)

    def get_all_events(self) -> list[DashboardEvent]:
        """Return all events in the ring buffer (oldest first)."""
        return list(self._events)

    def get_events_in_range(
        self,
        start_ts: float | None = None,
        end_ts: float | None = None,
        limit: int = 500,
    ) -> list[DashboardEvent]:
        """Return events filtered by Unix timestamp range from the ring buffer.

        Args:
            start_ts: Minimum event timestamp (Unix epoch seconds), inclusive.
            end_ts: Maximum event timestamp (Unix epoch seconds), inclusive.
            limit: Maximum events to return.
        """
        from datetime import datetime as _dt

        results: list[DashboardEvent] = []
        for evt in self._events:
            try:
                evt_ts = _dt.fromisoformat(evt.timestamp).timestamp()
            except (ValueError, TypeError):
                continue
            if start_ts is not None and evt_ts < start_ts:
                continue
            if end_ts is not None and evt_ts > end_ts:
                continue
            results.append(evt)
            if len(results) >= limit:
                break
        return results

    # ── Historical Queries ─────────────────────────────────────────────

    async def query_events(
        self,
        after_seq: int = 0,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return events after the given seq, ordered ascending.

        Returns list of dicts with 'seq' plus all DashboardEvent fields.
        """
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT seq, id, timestamp, type, agent_id, data"
            " FROM dashboard_events"
            " WHERE seq > ?"
            " ORDER BY seq ASC LIMIT ?",
            (after_seq, limit),
        )
        rows = await cursor.fetchall()
        results = []
        for seq, eid, ts, etype, agent_id, data_str in rows:
            try:
                data = json.loads(data_str) if data_str else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            results.append({
                "seq": seq,
                "id": eid,
                "timestamp": ts,
                "type": etype,
                "agent_id": agent_id,
                "data": data,
            })
        return results

    async def query_time_range(
        self,
        start: str,
        end: str,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return events within [start, end] ISO timestamps, ordered by seq."""
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT seq, id, timestamp, type, agent_id, data"
            " FROM dashboard_events"
            " WHERE timestamp >= ? AND timestamp <= ?"
            " ORDER BY seq ASC LIMIT ?",
            (start, end, limit),
        )
        rows = await cursor.fetchall()
        results = []
        for seq, eid, ts, etype, agent_id, data_str in rows:
            try:
                data = json.loads(data_str) if data_str else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            results.append({
                "seq": seq,
                "id": eid,
                "timestamp": ts,
                "type": etype,
                "agent_id": agent_id,
                "data": data,
            })
        return results

    async def event_count_persisted(self) -> int:
        """Return total number of persisted events."""
        if not self._db:
            return 0
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM dashboard_events"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def prune_old_events(self, retention_hours: int = 72) -> int:
        """Delete events older than retention_hours. Returns count deleted."""
        if not self._db:
            return 0
        cutoff = datetime.now(UTC).isoformat()
        # Simple approach: delete by timestamp comparison
        from datetime import timedelta
        cutoff_dt = datetime.now(UTC) - timedelta(hours=retention_hours)
        cutoff = cutoff_dt.isoformat()
        cursor = await self._db.execute(
            "DELETE FROM dashboard_events WHERE timestamp < ?",
            (cutoff,),
        )
        await self._db.commit()
        return cursor.rowcount or 0

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

    def admission_scored(
        self,
        memory_id: str | None,
        score: float,
        route: str,
        features: dict[str, Any],
        agent_id: str | None = None,
    ) -> None:
        """Emit an admission scoring event for dashboard observability."""
        self.emit(DashboardEvent(
            type="admission.scored",
            agent_id=agent_id,
            data={
                "memory_id": memory_id,
                "score": round(score, 3),
                "route": route,
                "features": {k: round(v, 3) for k, v in features.items()},
            },
        ))

    def reconciliation_applied(
        self,
        new_node_id: str,
        existing_node_id: str,
        relation: str,
        agent_id: str | None = None,
    ) -> None:
        """Emit a reconciliation event when entity states are classified."""
        self.emit(DashboardEvent(
            type="reconciliation.applied",
            agent_id=agent_id,
            data={
                "new_node_id": new_node_id,
                "existing_node_id": existing_node_id,
                "relation": relation,
            },
        ))

    def episode_created(
        self,
        episode_id: str,
        title: str,
        anchor_type: str,
        agent_id: str | None = None,
    ) -> None:
        """Emit an episode creation event."""
        self.emit(DashboardEvent(
            type="episode.created",
            agent_id=agent_id,
            data={
                "episode_id": episode_id,
                "title": title[:200],
                "anchor_type": anchor_type,
            },
        ))

    def episode_assigned(
        self,
        episode_id: str,
        fragment_id: str,
        signals_count: int,
        match_score: float = 0.0,
        agent_id: str | None = None,
    ) -> None:
        """Emit an episode fragment assignment event."""
        self.emit(DashboardEvent(
            type="episode.assigned",
            agent_id=agent_id,
            data={
                "episode_id": episode_id,
                "fragment_id": fragment_id,
                "signals_count": signals_count,
                "match_score": round(match_score, 3),
            },
        ))

    def episode_closed(
        self,
        episode_id: str,
        reason: str,
        member_count: int,
        agent_id: str | None = None,
    ) -> None:
        """Emit an episode closure event."""
        self.emit(DashboardEvent(
            type="episode.closed",
            agent_id=agent_id,
            data={
                "episode_id": episode_id,
                "reason": reason,
                "member_count": member_count,
            },
        ))

    def consolidation_abstract_created(
        self,
        abstract_type: str,
        node_id: str,
        source_count: int,
    ) -> None:
        """Emit an event when a consolidation abstract is created."""
        self.emit(DashboardEvent(
            type="consolidation.abstract_created",
            data={
                "abstract_type": abstract_type,
                "node_id": node_id,
                "source_count": source_count,
            },
        ))

    def consolidation_pass_complete(
        self,
        results: dict[str, int],
    ) -> None:
        """Emit a summary event when a consolidation pass finishes."""
        self.emit(DashboardEvent(
            type="consolidation.pass_complete",
            data=results,
        ))

    def dream_cycle_complete(
        self,
        results: dict[str, int],
    ) -> None:
        """Emit a summary event when a dream cycle finishes."""
        self.emit(DashboardEvent(
            type="dream.cycle_complete",
            data=results,
        ))

    # ── Filesystem Watch Events ──────────────────────────────────────────

    def watch_file_detected(
        self,
        path: str,
        domain: str,
        source: str,
    ) -> None:
        """Emit when a file change is detected by the watcher."""
        self.emit(DashboardEvent(
            type="watch.file_detected",
            data={
                "path": path,
                "domain": domain,
                "classification_source": source,
            },
        ))

    def watch_file_ingested(
        self,
        path: str,
        domain: str,
        memories_created: int,
    ) -> None:
        """Emit after a watched file is successfully ingested."""
        self.emit(DashboardEvent(
            type="watch.file_ingested",
            data={
                "path": path,
                "domain": domain,
                "memories_created": memories_created,
            },
        ))

    def watch_file_skipped(
        self,
        path: str,
        reason: str,
    ) -> None:
        """Emit when a watched file is skipped (unchanged, unsupported, etc.)."""
        self.emit(DashboardEvent(
            type="watch.file_skipped",
            data={
                "path": path,
                "reason": reason,
            },
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
