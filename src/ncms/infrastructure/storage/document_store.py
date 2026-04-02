"""SQLite implementation of the DocumentStore protocol (Phase 2.5).

Persistent storage for projects, documents, reviews, traceability,
and audit records. Uses the same aiosqlite database as the core
MemoryStore — tables created by V6 migration.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from ncms.domain.models import (
    AgentConfigSnapshot,
    ApprovalDecision,
    BusConversation,
    Document,
    DocumentLink,
    GroundingLogEntry,
    GuardrailViolation,
    LLMCallRecord,
    PipelineEvent,
    Project,
    ReviewScore,
)

logger = logging.getLogger(__name__)


class SQLiteDocumentStore:
    """SQLite-backed document intelligence storage."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ── Projects ─────────────────────────────────────────────────────────

    async def save_project(self, project: Project) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO projects
               (id, topic, target, source_type, repository_url, scope,
                status, phase, quality_score, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project.id, project.topic, project.target,
                project.source_type, project.repository_url,
                json.dumps(project.scope), project.status, project.phase,
                project.quality_score,
                project.created_at.isoformat(), project.updated_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_project(self, project_id: str) -> Project | None:
        cursor = await self.db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_project(row, cursor.description)

    async def list_projects(
        self, status: str | None = None, limit: int = 50,
    ) -> list[Project]:
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_project(r, cursor.description) for r in rows]

    async def update_project(self, project: Project) -> None:
        await self.save_project(project)  # INSERT OR REPLACE handles upsert

    def _row_to_project(self, row: Any, description: Any) -> Project:
        cols = [d[0] for d in description]
        d = dict(zip(cols, row, strict=False))
        d["scope"] = json.loads(d.get("scope", "[]"))
        return Project(**d)

    # ── Documents ────────────────────────────────────────────────────────

    async def save_document(self, doc: Document) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO documents
               (id, project_id, title, content, from_agent, doc_type,
                version, parent_doc_id, format, size_bytes, content_hash,
                entities, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.id, doc.project_id, doc.title, doc.content,
                doc.from_agent, doc.doc_type, doc.version,
                doc.parent_doc_id, doc.format, doc.size_bytes,
                doc.content_hash, json.dumps(doc.entities),
                json.dumps(doc.metadata), doc.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_document(self, doc_id: str) -> Document | None:
        cursor = await self.db.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_document(row, cursor.description)

    async def list_documents(
        self,
        project_id: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> list[Document]:
        conditions: list[str] = []
        params: list[Any] = []
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if doc_type:
            conditions.append("doc_type = ?")
            params.append(doc_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        cursor = await self.db.execute(
            f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_document(r, cursor.description) for r in rows]

    async def search_documents(
        self,
        entity: str | None = None,
        doc_type: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
    ) -> list[Document]:
        """Search documents by entity name, doc_type, or minimum review score."""
        conditions: list[str] = []
        params: list[Any] = []

        if entity:
            # JSON search: entity name appears in the entities JSON array
            conditions.append("entities LIKE ?")
            params.append(f"%{entity}%")
        if doc_type:
            conditions.append("doc_type = ?")
            params.append(doc_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        if min_score is not None:
            # Join with review_scores to filter by score
            query = f"""
                SELECT DISTINCT d.* FROM documents d
                JOIN review_scores r ON d.id = r.document_id
                {where}{' AND' if conditions else 'WHERE'} r.score >= ?
                ORDER BY d.created_at DESC LIMIT ?
            """
            params.extend([min_score, limit])
        else:
            query = f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_document(r, cursor.description) for r in rows]

    async def get_document_versions(self, doc_id: str) -> list[Document]:
        """Get all versions of a document following the parent_doc_id chain."""
        versions: list[Document] = []
        current_id: str | None = doc_id

        # Walk backward to find the root
        root_id = doc_id
        while True:
            cursor = await self.db.execute(
                "SELECT parent_doc_id FROM documents WHERE id = ?", (root_id,),
            )
            row = await cursor.fetchone()
            if not row or not row[0]:
                break
            root_id = row[0]

        # Walk forward from root collecting all versions
        current_id = root_id
        while current_id:
            doc = await self.get_document(current_id)
            if doc:
                versions.append(doc)
            # Find the next version that has this as parent
            cursor = await self.db.execute(
                "SELECT id FROM documents WHERE parent_doc_id = ?", (current_id,),
            )
            row = await cursor.fetchone()
            current_id = row[0] if row else None

        return versions

    def _row_to_document(self, row: Any, description: Any) -> Document:
        cols = [d[0] for d in description]
        d = dict(zip(cols, row, strict=False))
        d["entities"] = json.loads(d.get("entities", "[]"))
        d["metadata"] = json.loads(d.get("metadata", "{}"))
        return Document(**d)

    # ── Document Links ───────────────────────────────────────────────────

    async def save_document_link(self, link: DocumentLink) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO document_links
               (id, source_doc_id, target_doc_id, link_type, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                link.id, link.source_doc_id, link.target_doc_id,
                link.link_type, json.dumps(link.metadata),
                link.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_document_links(
        self, doc_id: str, direction: str = "both",
    ) -> list[DocumentLink]:
        if direction == "outgoing":
            cursor = await self.db.execute(
                "SELECT * FROM document_links WHERE source_doc_id = ?", (doc_id,),
            )
        elif direction == "incoming":
            cursor = await self.db.execute(
                "SELECT * FROM document_links WHERE target_doc_id = ?", (doc_id,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM document_links WHERE source_doc_id = ? OR target_doc_id = ?",
                (doc_id, doc_id),
            )
        rows = await cursor.fetchall()
        return [self._row_to_link(r, cursor.description) for r in rows]

    async def get_traceability_chain(self, doc_id: str) -> list[DocumentLink]:
        """Get the full traceability chain for a document (BFS traversal)."""
        visited: set[str] = set()
        chain: list[DocumentLink] = []
        queue = [doc_id]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            links = await self.get_document_links(current)
            for link in links:
                if link.id not in {existing.id for existing in chain}:
                    chain.append(link)
                # Follow the chain in both directions
                other = link.target_doc_id if link.source_doc_id == current else link.source_doc_id
                if other not in visited:
                    queue.append(other)

        return chain

    def _row_to_link(self, row: Any, description: Any) -> DocumentLink:
        cols = [d[0] for d in description]
        d = dict(zip(cols, row, strict=False))
        d["metadata"] = json.loads(d.get("metadata", "{}"))
        return DocumentLink(**d)

    # ── Review Scores ────────────────────────────────────────────────────

    async def save_review_score(self, score: ReviewScore) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO review_scores
               (id, document_id, project_id, reviewer_agent, review_round,
                score, severity, covered, missing, changes, review_doc_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                score.id, score.document_id, score.project_id,
                score.reviewer_agent, score.review_round,
                score.score, score.severity, score.covered,
                score.missing, score.changes, score.review_doc_id,
                score.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_review_scores(
        self,
        document_id: str | None = None,
        project_id: str | None = None,
    ) -> list[ReviewScore]:
        conditions: list[str] = []
        params: list[Any] = []
        if document_id:
            conditions.append("document_id = ?")
            params.append(document_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self.db.execute(
            f"SELECT * FROM review_scores {where} ORDER BY review_round, created_at",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_review(r, cursor.description) for r in rows]

    def _row_to_review(self, row: Any, description: Any) -> ReviewScore:
        cols = [d[0] for d in description]
        return ReviewScore(**dict(zip(cols, row, strict=False)))

    # ── Pipeline Events ──────────────────────────────────────────────────

    async def save_pipeline_event(self, event: PipelineEvent) -> None:
        await self.db.execute(
            """INSERT INTO pipeline_events
               (project_id, agent, node, status, detail, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.project_id, event.agent, event.node,
                event.status, event.detail,
                event.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_pipeline_events(self, project_id: str) -> list[PipelineEvent]:
        cursor = await self.db.execute(
            "SELECT project_id, agent, node, status, detail, timestamp "
            "FROM pipeline_events WHERE project_id = ? ORDER BY seq",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [
            PipelineEvent(
                project_id=r[0], agent=r[1], node=r[2],
                status=r[3], detail=r[4], timestamp=r[5],
            )
            for r in rows
        ]

    # ── Audit Records (simple insert-only) ───────────────────────────────

    async def save_approval(self, decision: ApprovalDecision) -> None:
        await self.db.execute(
            """INSERT INTO approval_decisions
               (id, project_id, document_id, decision, approver, comment,
                policies_active, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.id, decision.project_id, decision.document_id,
                decision.decision, decision.approver, decision.comment,
                json.dumps(decision.policies_active),
                decision.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def save_guardrail_violation(self, violation: GuardrailViolation) -> None:
        await self.db.execute(
            """INSERT INTO guardrail_violations
               (id, document_id, project_id, policy_type, rule, message,
                escalation, overridden, override_reason, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                violation.id, violation.document_id, violation.project_id,
                violation.policy_type, violation.rule, violation.message,
                violation.escalation, int(violation.overridden),
                violation.override_reason, violation.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def save_grounding_entry(self, entry: GroundingLogEntry) -> None:
        await self.db.execute(
            """INSERT INTO grounding_log
               (id, document_id, review_score_id, memory_id, retrieval_score,
                entity_query, domain, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id, entry.document_id, entry.review_score_id,
                entry.memory_id, entry.retrieval_score,
                entry.entity_query, entry.domain,
                entry.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def save_llm_call(self, record: LLMCallRecord) -> None:
        await self.db.execute(
            """INSERT INTO llm_calls
               (id, project_id, agent, node, prompt_hash, prompt_size,
                response_size, reasoning_size, model, thinking_enabled,
                duration_ms, trace_id, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id, record.project_id, record.agent, record.node,
                record.prompt_hash, record.prompt_size,
                record.response_size, record.reasoning_size,
                record.model, int(record.thinking_enabled),
                record.duration_ms, record.trace_id,
                record.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def save_config_snapshot(self, snapshot: AgentConfigSnapshot) -> None:
        await self.db.execute(
            """INSERT INTO agent_config_snapshots
               (id, project_id, agent, config_hash, prompt_version,
                model_name, thinking_enabled, max_tokens, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.id, snapshot.project_id, snapshot.agent,
                snapshot.config_hash, snapshot.prompt_version,
                snapshot.model_name, int(snapshot.thinking_enabled),
                snapshot.max_tokens, snapshot.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def save_bus_conversation(self, convo: BusConversation) -> None:
        await self.db.execute(
            """INSERT INTO bus_conversations
               (id, project_id, ask_id, from_agent, to_agent,
                question_preview, answer_preview, confidence,
                duration_ms, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                convo.id, convo.project_id, convo.ask_id,
                convo.from_agent, convo.to_agent,
                convo.question_preview, convo.answer_preview,
                convo.confidence, convo.duration_ms,
                convo.timestamp.isoformat(),
            ),
        )
        await self.db.commit()
