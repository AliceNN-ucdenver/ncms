"""Document Intelligence Service (Phase 2.5).

Orchestrates document persistence, entity extraction, traceability links,
review score parsing, and project lifecycle. This is the single entry point
for all document operations — the API layer calls this, not the store directly.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from ncms.domain.models import (
    AgentConfigSnapshot,
    ApprovalDecision,
    BusConversation,
    DocLinkType,
    Document,
    DocumentLink,
    GroundingLogEntry,
    GuardrailViolation,
    LLMCallRecord,
    PendingApproval,
    PipelineEvent,
    Project,
    ReviewScore,
)

logger = logging.getLogger(__name__)


class DocumentService:
    """Application-layer service for document intelligence.

    Wraps the DocumentStore with business logic:
    - Content hashing (SHA-256) at publish time
    - GLiNER entity extraction (delegates to memory_service)
    - Automatic document link creation
    - Review score parsing from expert responses
    - Project quality score aggregation
    - Pipeline event persistence
    """

    def __init__(
        self,
        store: Any,  # SQLiteDocumentStore — avoids circular import
        memory_svc: Any | None = None,  # MemoryService for GLiNER extraction
    ) -> None:
        self._store = store
        self._memory_svc = memory_svc

    # ── Projects ─────────────────────────────────────────────────────────

    async def create_project(
        self,
        topic: str,
        target: str = "",
        source_type: str = "research",
        repository_url: str | None = None,
        scope: list[str] | None = None,
    ) -> Project:
        """Create a new project and persist it."""
        project = Project(
            topic=topic,
            target=target,
            source_type=source_type,
            repository_url=repository_url,
            scope=scope or ["research", "prd", "design"],
        )
        await self._store.save_project(project)
        logger.info(
            "[doc-svc] Project created: %s — %s (%s)",
            project.id, topic[:60], source_type,
        )
        return project

    async def get_project(self, project_id: str) -> Project | None:
        return await self._store.get_project(project_id)

    async def list_projects(
        self, status: str | None = None, limit: int = 50,
    ) -> list[Project]:
        return await self._store.list_projects(status=status, limit=limit)

    async def update_project_phase(
        self, project_id: str, phase: str,
    ) -> None:
        """Update the current pipeline phase for a project."""
        project = await self._store.get_project(project_id)
        if project:
            project.phase = phase
            project.updated_at = datetime.now(UTC)
            await self._store.update_project(project)

    async def update_project_status(
        self, project_id: str, status: str,
    ) -> None:
        """Update the project status (active, completed, failed, archived)."""
        project = await self._store.get_project(project_id)
        if project:
            project.status = status
            project.updated_at = datetime.now(UTC)
            await self._store.update_project(project)

    async def update_project_quality(self, project_id: str) -> float | None:
        """Recompute project quality score from latest review scores."""
        scores = await self._store.get_review_scores(project_id=project_id)
        if not scores:
            return None
        avg = sum(s.score for s in scores if s.score is not None) / max(
            len([s for s in scores if s.score is not None]), 1,
        )
        project = await self._store.get_project(project_id)
        if project:
            project.quality_score = round(avg, 1)
            project.updated_at = datetime.now(UTC)
            await self._store.update_project(project)
        return avg

    # ── Documents ────────────────────────────────────────────────────────

    async def publish_document(
        self,
        title: str,
        content: str,
        from_agent: str | None = None,
        project_id: str | None = None,
        doc_type: str | None = None,
        parent_doc_id: str | None = None,
        entities: list[dict[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """Publish a new document with content hashing and entity extraction.

        If entities are not provided and memory_svc is available,
        GLiNER extraction runs automatically.
        """
        # Content hash for immutability verification
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Auto-extract entities if not provided
        if entities is None and self._memory_svc is not None:
            try:
                from ncms.domain.entity_extraction import resolve_labels

                doc_domains = [d for d in [from_agent, "software"] if d]
                cached = await self._memory_svc._get_cached_labels(doc_domains)
                labels = resolve_labels(doc_domains, cached_labels=cached)

                import asyncio

                from ncms.infrastructure.extraction.gliner_extractor import (
                    extract_entities_gliner,
                )

                entities = await asyncio.to_thread(
                    extract_entities_gliner, content, labels=labels,
                )
                logger.info(
                    "[doc-svc] GLiNER extracted %d entities for %s",
                    len(entities), title[:40],
                )
            except Exception as e:
                logger.warning("[doc-svc] Entity extraction failed: %s", e)
                entities = []
        elif entities is None:
            entities = []

        # Determine version
        version = 1
        if parent_doc_id:
            parent = await self._store.get_document(parent_doc_id)
            if parent:
                version = parent.version + 1

        doc = Document(
            project_id=project_id,
            title=title,
            content=content,
            from_agent=from_agent,
            doc_type=doc_type,
            version=version,
            parent_doc_id=parent_doc_id,
            size_bytes=len(content.encode("utf-8")),
            content_hash=content_hash,
            entities=entities,
            metadata=metadata or {},
        )
        await self._store.save_document(doc)

        # Auto-create supersedes link for version chains
        if parent_doc_id:
            await self._store.save_document_link(DocumentLink(
                source_doc_id=doc.id,
                target_doc_id=parent_doc_id,
                link_type=DocLinkType.SUPERSEDES,
            ))
            logger.info(
                "[doc-svc] Version chain: %s (v%d) supersedes %s",
                doc.id, version, parent_doc_id,
            )

        logger.info(
            "[doc-svc] Document published: %s (%s, %d bytes, %d entities, hash=%s)",
            doc.id, doc_type or "untyped", doc.size_bytes,
            len(entities), content_hash[:12],
        )
        return doc

    async def get_document(self, doc_id: str) -> Document | None:
        return await self._store.get_document(doc_id)

    async def list_documents(
        self,
        project_id: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> list[Document]:
        return await self._store.list_documents(
            project_id=project_id, doc_type=doc_type, limit=limit,
        )

    async def search_documents(
        self,
        entity: str | None = None,
        doc_type: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
    ) -> list[Document]:
        return await self._store.search_documents(
            entity=entity, doc_type=doc_type, min_score=min_score, limit=limit,
        )

    # ── Document Links ───────────────────────────────────────────────────

    async def create_link(
        self,
        source_doc_id: str,
        target_doc_id: str,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentLink:
        """Create a typed link between two documents."""
        link = DocumentLink(
            source_doc_id=source_doc_id,
            target_doc_id=target_doc_id,
            link_type=link_type,
            metadata=metadata or {},
        )
        await self._store.save_document_link(link)
        logger.info(
            "[doc-svc] Link: %s → %s → %s",
            source_doc_id[:8], link_type, target_doc_id[:8],
        )
        return link

    async def get_traceability_chain(self, doc_id: str) -> list[DocumentLink]:
        return await self._store.get_traceability_chain(doc_id)

    async def get_document_versions(self, doc_id: str) -> list[Document]:
        return await self._store.get_document_versions(doc_id)

    # ── Review Scores ────────────────────────────────────────────────────

    async def save_review_score(
        self,
        document_id: str,
        project_id: str | None,
        reviewer_agent: str,
        review_round: int,
        score: int | None = None,
        severity: str | None = None,
        covered: str | None = None,
        missing: str | None = None,
        changes: str | None = None,
        review_doc_id: str | None = None,
    ) -> ReviewScore:
        """Save a structured review score."""
        review = ReviewScore(
            document_id=document_id,
            project_id=project_id,
            reviewer_agent=reviewer_agent,
            review_round=review_round,
            score=score,
            severity=severity,
            covered=covered,
            missing=missing,
            changes=changes,
            review_doc_id=review_doc_id,
        )
        await self._store.save_review_score(review)

        # Create document link: review → document
        if review_doc_id:
            await self.create_link(
                review_doc_id, document_id, DocLinkType.REVIEWS,
                metadata={"score": score, "reviewer": reviewer_agent, "round": review_round},
            )

        # Update project quality score
        if project_id:
            avg = await self.update_project_quality(project_id)
            logger.info(
                "[doc-svc] Review: %s scored %s%% by %s (round %d, project avg: %s%%)",
                document_id[:8], score, reviewer_agent, review_round,
                round(avg, 1) if avg else "N/A",
            )

        return review

    async def get_review_scores(
        self,
        document_id: str | None = None,
        project_id: str | None = None,
    ) -> list[ReviewScore]:
        return await self._store.get_review_scores(
            document_id=document_id, project_id=project_id,
        )

    @staticmethod
    def parse_review_response(response_text: str) -> dict[str, Any]:
        """Parse SCORE/SEVERITY/COVERED/MISSING/CHANGES from expert response."""
        import re

        result: dict[str, Any] = {}
        score_match = re.search(r"SCORE:\s*(\d+)", response_text)
        if score_match:
            result["score"] = int(score_match.group(1))
        severity_match = re.search(r"SEVERITY:\s*(\w+)", response_text)
        if severity_match:
            result["severity"] = severity_match.group(1)
        covered_match = re.search(
            r"COVERED:\s*(.+?)(?=\nMISSING:|\nCHANGES:|\Z)", response_text, re.DOTALL,
        )
        if covered_match:
            result["covered"] = covered_match.group(1).strip()
        missing_match = re.search(r"MISSING:\s*(.+?)(?=\nCHANGES:|\Z)", response_text, re.DOTALL)
        if missing_match:
            result["missing"] = missing_match.group(1).strip()
        changes_match = re.search(r"CHANGES:\s*(.+)", response_text, re.DOTALL)
        if changes_match:
            result["changes"] = changes_match.group(1).strip()
        return result

    # ── Pipeline Events ──────────────────────────────────────────────────

    async def record_pipeline_event(
        self,
        project_id: str,
        agent: str,
        node: str,
        status: str,
        detail: str = "",
    ) -> None:
        """Record a pipeline node execution event."""
        event = PipelineEvent(
            project_id=project_id,
            agent=agent,
            node=node,
            status=status,
            detail=detail,
        )
        await self._store.save_pipeline_event(event)

    async def get_pipeline_events(self, project_id: str) -> list[PipelineEvent]:
        return await self._store.get_pipeline_events(project_id)

    # ── Audit Records ────────────────────────────────────────────────────

    async def record_approval(
        self, project_id: str | None, document_id: str,
        decision: str, approver: str, comment: str | None = None,
        policies_active: dict | None = None,
    ) -> ApprovalDecision:
        approval = ApprovalDecision(
            project_id=project_id, document_id=document_id,
            decision=decision, approver=approver, comment=comment,
            policies_active=policies_active or {},
        )
        await self._store.save_approval(approval)
        logger.info(
            "[doc-svc] Approval: %s %s by %s for %s",
            decision, document_id[:8], approver, project_id or "?",
        )
        return approval

    async def record_guardrail_violation(
        self, document_id: str | None, project_id: str | None,
        policy_type: str, rule: str, message: str,
        escalation: str, overridden: bool = False,
        override_reason: str | None = None,
    ) -> GuardrailViolation:
        violation = GuardrailViolation(
            document_id=document_id, project_id=project_id,
            policy_type=policy_type, rule=rule, message=message,
            escalation=escalation, overridden=overridden,
            override_reason=override_reason,
        )
        await self._store.save_guardrail_violation(violation)
        return violation

    # ── Guardrail Approval Gates ─────────────────────────────────────────

    async def create_approval_request(
        self, project_id: str | None, agent: str, node: str,
        violations: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> PendingApproval:
        """Create a pending approval for a guardrail gate.

        Returns the PendingApproval with its ID for polling.
        """
        approval = PendingApproval(
            project_id=project_id, agent=agent, node=node,
            violations=violations, context=context or {},
        )
        await self._store.create_pending_approval(approval)
        logger.info(
            "[doc-svc] Approval request created: %s for %s/%s (%d violations)",
            approval.id, agent, node, len(violations),
        )
        return approval

    async def get_approval_status(self, approval_id: str) -> PendingApproval | None:
        """Get the current state of an approval request (agent polls this)."""
        return await self._store.get_pending_approval(approval_id)

    async def list_pending_approvals(
        self, status: str | None = None, project_id: str | None = None,
    ) -> list[PendingApproval]:
        """List approval requests, optionally filtered by status or project."""
        return await self._store.list_pending_approvals(status=status, project_id=project_id)

    async def decide_approval(
        self, approval_id: str, decision: str, decided_by: str,
        comment: str | None = None,
    ) -> PendingApproval | None:
        """Record a human approval/denial decision.

        Also creates an ApprovalDecision audit record and updates project
        status if denied.
        """
        ok = await self._store.decide_approval(approval_id, decision, decided_by, comment)
        if not ok:
            return None
        approval = await self._store.get_pending_approval(approval_id)
        if not approval:
            return None

        # Record formal audit trail
        await self.record_approval(
            project_id=approval.project_id,
            document_id=approval.id,  # link to approval request
            decision=decision,
            approver=decided_by,
            comment=comment,
        )

        # If denied, mark project as failed
        if decision == "denied" and approval.project_id:
            try:
                project = await self._store.get_project(approval.project_id)
                if project and project.status == "active":
                    project.status = "denied"
                    project.updated_at = datetime.now(UTC)
                    await self._store.update_project(project)
                    logger.info(
                        "[doc-svc] Project %s denied by %s",
                        approval.project_id, decided_by,
                    )
            except Exception:
                logger.warning("[doc-svc] Failed to update project status on denial")

        logger.info(
            "[doc-svc] Approval %s decided: %s by %s",
            approval_id, decision, decided_by,
        )
        return approval

    async def record_grounding(
        self, document_id: str, memory_id: str,
        retrieval_score: float | None = None,
        entity_query: str | None = None,
        domain: str | None = None,
        review_score_id: str | None = None,
    ) -> GroundingLogEntry:
        entry = GroundingLogEntry(
            document_id=document_id, memory_id=memory_id,
            retrieval_score=retrieval_score, entity_query=entity_query,
            domain=domain, review_score_id=review_score_id,
        )
        await self._store.save_grounding_entry(entry)
        return entry

    async def record_llm_call(
        self, project_id: str | None, agent: str, node: str,
        prompt_size: int | None = None, response_size: int | None = None,
        reasoning_size: int = 0, model: str | None = None,
        thinking_enabled: bool = False, duration_ms: int | None = None,
        trace_id: str | None = None, prompt_hash: str | None = None,
    ) -> LLMCallRecord:
        record = LLMCallRecord(
            project_id=project_id, agent=agent, node=node,
            prompt_hash=prompt_hash, prompt_size=prompt_size,
            response_size=response_size, reasoning_size=reasoning_size,
            model=model, thinking_enabled=thinking_enabled,
            duration_ms=duration_ms, trace_id=trace_id,
        )
        await self._store.save_llm_call(record)
        return record

    async def record_config_snapshot(
        self, project_id: str | None, agent: str,
        config_hash: str | None = None, prompt_version: str | None = None,
        model_name: str | None = None, thinking_enabled: bool = False,
        max_tokens: int | None = None,
    ) -> AgentConfigSnapshot:
        snapshot = AgentConfigSnapshot(
            project_id=project_id, agent=agent,
            config_hash=config_hash, prompt_version=prompt_version,
            model_name=model_name, thinking_enabled=thinking_enabled,
            max_tokens=max_tokens,
        )
        await self._store.save_config_snapshot(snapshot)
        return snapshot

    async def record_bus_conversation(
        self, project_id: str | None, ask_id: str,
        from_agent: str, to_agent: str | None = None,
        question_preview: str | None = None,
        answer_preview: str | None = None,
        confidence: float | None = None,
        duration_ms: int | None = None,
    ) -> BusConversation:
        convo = BusConversation(
            project_id=project_id, ask_id=ask_id,
            from_agent=from_agent, to_agent=to_agent,
            question_preview=question_preview,
            answer_preview=answer_preview,
            confidence=confidence, duration_ms=duration_ms,
        )
        await self._store.save_bus_conversation(convo)
        return convo

    # ── Convenience: Project Summary ─────────────────────────────────────

    async def get_project_summary(self, project_id: str) -> dict[str, Any]:
        """Get a full project summary with documents, scores, links, and events."""
        project = await self._store.get_project(project_id)
        if not project:
            return {"error": "Project not found"}

        docs = await self._store.list_documents(project_id=project_id)
        scores = await self._store.get_review_scores(project_id=project_id)
        events = await self._store.get_pipeline_events(project_id)

        # Collect all document links for the project's documents
        doc_ids = {d.id for d in docs}
        all_links: list[DocumentLink] = []
        seen_link_ids: set[str] = set()
        for d in docs:
            links = await self._store.get_document_links(d.id)
            for link in links:
                if link.id not in seen_link_ids:
                    seen_link_ids.add(link.id)
                    all_links.append(link)

        return {
            "project": project.model_dump(mode="json"),
            "documents": [
                {
                    "id": d.id,
                    "title": d.title,
                    "doc_type": d.doc_type,
                    "version": d.version,
                    "from_agent": d.from_agent,
                    "size_bytes": d.size_bytes,
                    "content_hash": d.content_hash,
                    "entity_count": len(d.entities),
                    "parent_doc_id": d.parent_doc_id,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in docs
            ],
            "document_links": [
                {
                    "source_doc_id": link.source_doc_id,
                    "target_doc_id": link.target_doc_id,
                    "link_type": link.link_type,
                    "metadata": link.metadata,
                }
                for link in all_links
            ],
            "review_scores": [s.model_dump(mode="json") for s in scores],
            "pipeline_events": len(events),
            "quality_score": project.quality_score,
        }

    async def get_audit_timeline(self, project_id: str) -> list[dict]:
        """Get a unified chronological timeline of ALL audit events for a project.

        UNION query across 8 tables, normalized to a common schema.
        """
        db = self._store.db
        query = """
            SELECT timestamp, 'pipeline' as type, agent, node || ': ' || status as detail, '' as extra
            FROM pipeline_events WHERE project_id = ?
            UNION ALL
            SELECT timestamp, 'approval' as type, approver as agent, decision as detail, comment as extra
            FROM approval_decisions WHERE project_id = ?
            UNION ALL
            SELECT timestamp, 'guardrail' as type, '' as agent,
                   escalation || ' ' || policy_type || ': ' || COALESCE(message, rule) as detail, '' as extra
            FROM guardrail_violations WHERE project_id = ?
            UNION ALL
            SELECT timestamp, 'llm_call' as type, agent,
                   node || ' (' || COALESCE(prompt_size, 0) || '→' || COALESCE(response_size, 0) || ' chars, ' || COALESCE(duration_ms, 0) || 'ms)' as detail,
                   model as extra
            FROM llm_calls WHERE project_id = ?
            UNION ALL
            SELECT timestamp, 'bus' as type, from_agent as agent,
                   from_agent || '→' || COALESCE(to_agent, '?') || ' conf=' || COALESCE(confidence, 0) as detail,
                   SUBSTR(question_preview, 1, 80) as extra
            FROM bus_conversations WHERE project_id = ?
            UNION ALL
            SELECT rs.created_at as timestamp, 'review' as type, rs.reviewer_agent as agent,
                   'Round ' || rs.review_round || ': ' || COALESCE(rs.score, 0) || '%' as detail, rs.severity as extra
            FROM review_scores rs
            JOIN documents d ON rs.document_id = d.id
            WHERE d.project_id = ?
            UNION ALL
            SELECT timestamp, 'config' as type, agent,
                   COALESCE(model_name, '?') || ' thinking=' || thinking_enabled || ' max_tokens=' || COALESCE(max_tokens, 0) as detail,
                   '' as extra
            FROM agent_config_snapshots WHERE project_id = ?
            ORDER BY timestamp
        """
        cursor = await db.execute(query, (project_id,) * 7)
        rows = await cursor.fetchall()
        return [
            {"timestamp": r[0], "type": r[1], "agent": r[2] or "", "detail": r[3] or "", "extra": r[4] or ""}
            for r in rows
        ]

    async def verify_project_integrity(self, project_id: str) -> dict:
        """Verify hash chain integrity for all audit tables in a project."""
        results = {}
        for table in ["pipeline_events", "approval_decisions", "guardrail_violations", "llm_calls", "bus_conversations"]:
            results[table] = await self._store.verify_hash_chain(table)
        all_verified = all(r["verified"] for r in results.values())
        total_checked = sum(r["records_checked"] for r in results.values())
        return {
            "verified": all_verified,
            "records_checked": total_checked,
            "tables": results,
        }

    async def verify_document_integrity(self, doc_id: str) -> dict:
        """Re-compute SHA-256 of document content and compare to stored hash."""
        doc = await self._store.get_document(doc_id)
        if not doc:
            return {"verified": False, "error": "document not found"}
        import hashlib
        computed = hashlib.sha256(doc.content.encode()).hexdigest()
        stored = doc.content_hash
        return {
            "verified": computed == stored,
            "document_id": doc_id,
            "computed_hash": computed,
            "stored_hash": stored,
        }

    async def get_document_provenance(self, doc_id: str) -> dict:
        """Get complete provenance chain for a single document."""
        doc = await self._store.get_document(doc_id)
        if not doc:
            return {"error": "document not found"}

        # Lineage via BFS
        chain = await self._store.get_traceability_chain(doc_id)
        lineage = [
            {"source": l.source_doc_id, "target": l.target_doc_id, "link_type": l.link_type}
            for l in chain
        ]

        # Reviews
        scores = await self._store.get_review_scores(document_id=doc_id)

        # Guardrail violations for this doc's project
        violations = []
        if doc.project_id:
            db = self._store.db
            cursor = await db.execute(
                "SELECT policy_type, rule, message, escalation, timestamp "
                "FROM guardrail_violations WHERE project_id = ? ORDER BY timestamp",
                (doc.project_id,),
            )
            violations = [
                {"policy_type": r[0], "rule": r[1], "message": r[2], "escalation": r[3], "timestamp": r[4]}
                for r in await cursor.fetchall()
            ]

        # LLM calls that produced this doc (same agent, close timestamp)
        llm_calls = []
        if doc.project_id and doc.from_agent:
            cursor = await db.execute(
                "SELECT agent, node, prompt_size, response_size, duration_ms, model, timestamp "
                "FROM llm_calls WHERE project_id = ? AND agent = ? ORDER BY timestamp",
                (doc.project_id, doc.from_agent),
            )
            llm_calls = [
                {"agent": r[0], "node": r[1], "prompt_size": r[2], "response_size": r[3],
                 "duration_ms": r[4], "model": r[5], "timestamp": r[6]}
                for r in await cursor.fetchall()
            ]

        # Content integrity
        import hashlib
        computed_hash = hashlib.sha256(doc.content.encode()).hexdigest()

        return {
            "document": {
                "id": doc.id, "title": doc.title, "doc_type": doc.doc_type,
                "from_agent": doc.from_agent, "version": doc.version,
                "size_bytes": doc.size_bytes, "content_hash": doc.content_hash,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            },
            "lineage": lineage,
            "reviews": [s.model_dump(mode="json") for s in scores],
            "guardrail_findings": violations,
            "llm_calls": llm_calls,
            "integrity": {
                "content_hash_verified": computed_hash == doc.content_hash,
                "computed_hash": computed_hash,
                "stored_hash": doc.content_hash,
            },
        }

    async def compute_compliance_score(self, project_id: str) -> dict:
        """Compute composite compliance score for a project."""
        # Review score average (40%)
        scores = await self._store.get_review_scores(project_id=project_id)
        score_values = [s.score for s in scores if s.score is not None]
        review_avg = sum(score_values) / len(score_values) if score_values else 0

        # Guardrail violations penalty (20%)
        db = self._store.db
        cursor = await db.execute(
            "SELECT escalation FROM guardrail_violations WHERE project_id = ?",
            (project_id,),
        )
        violations = await cursor.fetchall()
        penalty = 0
        for v in violations:
            esc = v[0]
            if esc == "warn":
                penalty += 10
            elif esc == "block":
                penalty += 25
            elif esc == "reject":
                penalty += 50
        violation_score = max(0, 100 - penalty)

        # Document completeness (10%)
        docs = await self._store.list_documents(project_id=project_id)
        expected_types = {"research", "prd", "design"}
        present_types = {d.doc_type for d in docs if d.doc_type}
        completeness = len(present_types & expected_types) / len(expected_types) * 100

        # Grounding coverage (15%)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM grounding_log gl JOIN documents d ON gl.document_id = d.id WHERE d.project_id = ?",
            (project_id,),
        )
        grounding_count = (await cursor.fetchone())[0]
        grounding_score = min(100, grounding_count * 10)  # 10 citations = 100%

        # Approval gate (15%)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE project_id = ? AND status = 'denied'",
            (project_id,),
        )
        denied = (await cursor.fetchone())[0]
        approval_score = 0 if denied > 0 else 100

        # Weighted composite
        composite = (
            review_avg * 0.40
            + violation_score * 0.20
            + grounding_score * 0.15
            + approval_score * 0.15
            + completeness * 0.10
        )

        return {
            "composite_score": round(composite, 1),
            "breakdown": {
                "review_average": {"score": round(review_avg, 1), "weight": 0.40, "count": len(score_values)},
                "violations": {"score": round(violation_score, 1), "weight": 0.20, "count": len(violations)},
                "grounding": {"score": round(grounding_score, 1), "weight": 0.15, "citations": grounding_count},
                "approval_gate": {"score": round(approval_score, 1), "weight": 0.15, "denied": denied},
                "completeness": {"score": round(completeness, 1), "weight": 0.10, "types": list(present_types)},
            },
        }

    async def export_audit_report(self, project_id: str) -> str:
        """Generate a complete audit report as markdown for a project."""
        import hashlib as _hashlib
        from datetime import UTC, datetime

        project = await self._store.get_project(project_id)
        if not project:
            return "# Error\n\nProject not found."

        docs = await self._store.list_documents(project_id=project_id)
        scores = await self._store.get_review_scores(project_id=project_id)
        timeline = await self.get_audit_timeline(project_id)
        compliance = await self.compute_compliance_score(project_id)
        integrity = await self.verify_project_integrity(project_id)

        db = self._store.db

        cursor = await db.execute(
            "SELECT agent, model_name, thinking_enabled, max_tokens, timestamp "
            "FROM agent_config_snapshots WHERE project_id = ? ORDER BY timestamp",
            (project_id,),
        )
        configs = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT policy_type, rule, message, escalation, timestamp "
            "FROM guardrail_violations WHERE project_id = ? ORDER BY timestamp",
            (project_id,),
        )
        violations = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT agent, node, prompt_size, response_size, duration_ms, model, timestamp "
            "FROM llm_calls WHERE project_id = ? ORDER BY timestamp",
            (project_id,),
        )
        llm_calls = await cursor.fetchall()

        all_links: list = []
        seen: set = set()
        for d in docs:
            links = await self._store.get_document_links(d.id)
            for link in links:
                if link.id not in seen:
                    seen.add(link.id)
                    all_links.append(link)

        now = datetime.now(UTC).isoformat()

        md = []
        md.append(f"# Audit Report: {project.topic}")
        md.append(f"\n**Generated:** {now}")
        md.append(f"**Project ID:** {project_id}")
        md.append(f"**Status:** {project.status}")
        md.append(f"**Quality Score:** {project.quality_score or 'N/A'}%")
        md.append(f"**Created:** {project.created_at}")

        md.append("\n---\n\n## Compliance Score")
        md.append(f"\n**Composite:** {compliance['composite_score']}%\n")
        md.append("| Signal | Score | Weight |")
        md.append("|--------|-------|--------|")
        for key, val in compliance["breakdown"].items():
            md.append(f"| {key} | {val['score']}% | {val['weight']*100:.0f}% |")

        md.append("\n---\n\n## Integrity Verification")
        md.append(f"\n**Hash Chain Verified:** {'Yes' if integrity['verified'] else 'NO — CHAIN BROKEN'}")
        md.append(f"**Records Checked:** {integrity['records_checked']}")
        for table, result in integrity["tables"].items():
            status = "OK" if result["verified"] else f"BROKEN at row {result['break_at']}"
            md.append(f"- {table}: {status} ({result['records_checked']} records)")

        md.append("\n---\n\n## Document Inventory")
        md.append("\n| Doc Type | Title | Agent | Version | Size | Content Hash |")
        md.append("|----------|-------|-------|---------|------|-------------|")
        for d in docs:
            h = d.content_hash[:12] if d.content_hash else "N/A"
            md.append(f"| {d.doc_type} | {d.title[:50]} | {d.from_agent} | v{d.version} | {d.size_bytes:,} bytes | `{h}` |")

        md.append("\n---\n\n## Traceability Chain")
        if all_links:
            md.append("\n| Source | Target | Link Type |")
            md.append("|--------|--------|-----------|")
            for link in all_links:
                md.append(f"| {link.source_doc_id[:12]} | {link.target_doc_id[:12]} | {link.link_type} |")
        else:
            md.append("\nNo document links found.")

        md.append("\n---\n\n## Review History")
        if scores:
            md.append("\n| Reviewer | Round | Score | Severity |")
            md.append("|----------|-------|-------|----------|")
            for s in scores:
                md.append(f"| {s.reviewer_agent} | {s.review_round} | {s.score}% | {s.severity or 'N/A'} |")
        else:
            md.append("\nNo review scores recorded.")

        md.append("\n---\n\n## Guardrail Findings")
        if violations:
            md.append("\n| Escalation | Policy | Rule | Message | Time |")
            md.append("|-----------|--------|------|---------|------|")
            for v in violations:
                md.append(f"| {v[3]} | {v[0]} | {v[1]} | {(v[2] or '')[:60]} | {v[4]} |")
        else:
            md.append("\nNo guardrail violations.")

        md.append("\n---\n\n## Agent Configurations at Pipeline Start")
        if configs:
            md.append("\n| Agent | Model | Thinking | Max Tokens | Time |")
            md.append("|-------|-------|----------|-----------|------|")
            for c in configs:
                thinking = "ON" if c[2] else "OFF"
                md.append(f"| {c[0]} | {(c[1] or '?')[:30]} | {thinking} | {c[3] or '?'} | {c[4]} |")
        else:
            md.append("\nNo config snapshots recorded.")

        md.append("\n---\n\n## LLM Call Log")
        if llm_calls:
            md.append("\n| Agent | Node | Prompt | Response | Duration | Model |")
            md.append("|-------|------|--------|----------|----------|-------|")
            for c in llm_calls:
                md.append(f"| {c[0]} | {c[1]} | {c[2]:,} chars | {c[3]:,} chars | {c[4]:,}ms | {(c[5] or '?')[:25]} |")
        else:
            md.append("\nNo LLM calls recorded.")

        md.append("\n---\n\n## Audit Timeline (last 50 events)")
        md.append("\n| Time | Type | Agent | Detail |")
        md.append("|------|------|-------|--------|")
        for evt in timeline[-50:]:
            detail = (evt["detail"] or "")[:80]
            md.append(f"| {evt['timestamp']} | {evt['type']} | {evt['agent']} | {detail} |")

        report_content = "\n".join(md)
        report_hash = _hashlib.sha256(report_content.encode()).hexdigest()
        md.append(f"\n---\n\n*Report hash: `{report_hash}`*")
        md.append(f"*Generated by NCMS Document Intelligence at {now}*")

        return "\n".join(md)
