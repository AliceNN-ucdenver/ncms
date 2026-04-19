"""Curated ADR corpus for the temporal-trajectory experiment.

10 ADRs spanning 2023-01 → 2026-01, forming a clear authentication
evolution trajectory.  Each ADR references its predecessor where
appropriate — the cross-references are what the path-rerank is meant
to pick up.

Why ADRs?  Because:
* They're a real production use case for this type of retrieval.
* The chronology is unambiguous (every ADR has a date).
* The "current state" has an objectively correct answer (the last ADR
  that isn't superseded).
* Unqualified queries ("What auth does the system use?") can't be
  answered by BM25 alone — all 10 mention "authentication".

Each memory has:
* ``mid``: stable short id for assertion targets
* ``content``: ADR text
* ``observed_at``: ISO date
* ``entities``: hand-labeled entity set, matching what GLiNER would
  plausibly extract (used by the path graph).
* ``subject``: the zone this memory belongs to (for the LG grammar).
  ``None`` means the memory is not grammatically part of any
  evolution zone — e.g., procedural docs, tangential topics.

These hand labels are the *same across all retrievers* — we're
comparing rerank strategies, not extraction strategies.

**LG Phase E addition:** `EDGES` lists typed transitions between
memories.  The LG retriever uses these as the trajectory grammar's
admissible moves.  A memory without any edge in/out of an auth-
subject zone is grammatically unreachable from that zone, even if it
mentions "authentication" in its content — which is the structural
mechanism that demotes adversarial noise (like ADR-033) below the
zone-admitted memories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Memory:
    mid: str
    content: str
    observed_at: datetime
    entities: frozenset[str]
    subject: str | None = None  # Zone membership for LG grammar


@dataclass(frozen=True)
class Edge:
    """Typed edge between two memories, interpreted as a G_tr
    production.  ``transition`` is one of the generic state-evolution
    labels — see grammar.py for admissibility rules.

    ``retires_entities`` is the explicit annotation of which
    sub-entities this transition retires.  This is what NCMS's
    ``ReconciliationService`` produces at ingest — e.g., when ADR-021
    supersedes ADR-012, the reconciliation records that "JWT" is
    retired.  Without this annotation, the grammar can't distinguish
    "entity still mentioned but retired" from "entity still active."
    """

    src: str
    dst: str
    transition: str   # 'refines' | 'supersedes' | 'introduces' | 'retires'
    retires_entities: frozenset[str] = frozenset()


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


ADR_CORPUS: list[Memory] = [
    Memory(
        mid="ADR-001",
        content=(
            "ADR-001 (2023-02-10): Initial authentication design uses "
            "session cookies for user authentication.  Simple, "
            "server-side state."
        ),
        observed_at=_utc(2023, 2, 10),
        entities=frozenset({"authentication", "session cookies", "ADR-001"}),
        subject="authentication",
    ),
    Memory(
        mid="ADR-005",
        content=(
            "ADR-005 (2023-07-14): Migrate logging to structured JSON.  "
            "Unrelated to authentication — included to add BM25 noise."
        ),
        observed_at=_utc(2023, 7, 14),
        entities=frozenset({"logging", "JSON", "ADR-005"}),
        subject="logging",
    ),
    Memory(
        mid="ADR-007",
        content=(
            "ADR-007 (2023-11-03): Authentication moves from session "
            "cookies to OAuth 2.0 with third-party identity providers.  "
            "Supersedes ADR-001."
        ),
        observed_at=_utc(2023, 11, 3),
        entities=frozenset({
            "authentication", "OAuth", "OAuth 2.0", "ADR-007", "ADR-001",
        }),
        subject="authentication",
    ),
    Memory(
        mid="ADR-010",
        content=(
            "ADR-010 (2024-01-15): OAuth integration review — refresh "
            "handling gaps identified.  Refines ADR-007 with stronger "
            "refresh semantics."
        ),
        observed_at=_utc(2024, 1, 15),
        entities=frozenset({
            "authentication", "OAuth", "refresh", "ADR-010", "ADR-007",
        }),
        subject="authentication",
    ),
    Memory(
        mid="ADR-012",
        content=(
            "ADR-012 (2024-02-20): Add JSON Web Tokens alongside OAuth "
            "for API authentication.  Extends ADR-007 for programmatic "
            "access."
        ),
        observed_at=_utc(2024, 2, 20),
        entities=frozenset({
            "authentication", "JWT", "JSON Web Tokens", "OAuth",
            "ADR-012", "ADR-007",
        }),
        subject="authentication",
    ),
    Memory(
        mid="ADR-014",
        content=(
            "ADR-014 (2024-04-05): Database migration plan for user "
            "sessions storage.  Distinct from authentication decisions."
        ),
        observed_at=_utc(2024, 4, 5),
        entities=frozenset({"database", "sessions", "ADR-014"}),
        subject="database",
    ),
    Memory(
        mid="ADR-017",
        content=(
            "ADR-017 (2024-08-12): Rate-limiting strategy for API "
            "endpoints.  Not authentication-related."
        ),
        observed_at=_utc(2024, 8, 12),
        entities=frozenset({"rate limiting", "API", "ADR-017"}),
        subject="rate_limiting",
    ),
    Memory(
        mid="ADR-021",
        content=(
            "ADR-021 (2025-03-10): Retire long-lived JWTs.  "
            "Authentication now uses short-lived access tokens + "
            "refresh tokens.  Supersedes ADR-012."
        ),
        observed_at=_utc(2025, 3, 10),
        entities=frozenset({
            "authentication", "JWT", "access tokens", "refresh tokens",
            "ADR-021", "ADR-012",
        }),
        subject="authentication",
    ),
    Memory(
        mid="ADR-024",
        content=(
            "ADR-024 (2025-06-18): Introduce cache layer for session "
            "validation.  Works with ADR-021 tokens."
        ),
        observed_at=_utc(2025, 6, 18),
        entities=frozenset({
            "cache", "session validation", "ADR-024", "ADR-021",
        }),
        subject="caching",
    ),
    Memory(
        mid="ADR-027",
        content=(
            "ADR-027 (2025-10-05): Multi-factor authentication added on "
            "top of existing token flow.  Extends ADR-021."
        ),
        observed_at=_utc(2025, 10, 5),
        entities=frozenset({
            "authentication", "MFA", "multi-factor authentication",
            "ADR-027", "ADR-021",
        }),
        subject="authentication",
    ),
    Memory(
        mid="ADR-029",
        content=(
            "ADR-029 (2026-01-08): Authentication flow moves to "
            "passkeys and WebAuthn.  Password authentication is fully "
            "retired.  Supersedes ADR-021 and ADR-027."
        ),
        observed_at=_utc(2026, 1, 8),
        entities=frozenset({
            "authentication", "passkeys", "WebAuthn", "password",
            "ADR-029", "ADR-021", "ADR-027",
        }),
        subject="authentication",
    ),
    # ── Stress-test memory: dated AFTER ADR-029 but NOT in the auth
    # evolution chain.  Mentions 'authentication' peripherally so it
    # matches BM25.  A naive 'latest-entity-linked' sort surfaces THIS
    # as the answer for 'current authentication?' — but it's wrong.
    # Path-rerank should detect it isn't on the long chain (short
    # predecessor path — no entity-overlap with ADR-021/027/029 main
    # line) and demote it.
    Memory(
        mid="ADR-033",
        content=(
            "ADR-033 (2026-03-15): Operational playbook for "
            "authentication incident response.  Covers token-rotation "
            "procedures and rollback steps.  Procedural, not a design "
            "decision."
        ),
        observed_at=_utc(2026, 3, 15),
        entities=frozenset({
            # Intentionally thin overlap: 'authentication' only.
            # No link back to ADR-029 / passkeys / WebAuthn / password.
            "authentication", "incident response", "playbook", "ADR-033",
        }),
        # Critical for the LG experiment: ADR-033 is NOT in the
        # authentication zone — it's in 'auth_ops', a separate
        # subject.  No grammatical edge in/out of the auth evolution
        # chain.  A grammar-constrained retriever will refuse to
        # consider it as an answer to auth-evolution queries.
        subject="auth_ops",
    ),

    # ── Medical timeline subject — knee injury → surgery → recovery ──
    Memory(
        mid="MED-01",
        content=(
            "2024-04-15: Patient reports right knee pain following a "
            "jogging incident.  Unable to bear full weight."
        ),
        observed_at=_utc(2024, 4, 15),
        entities=frozenset({"knee", "knee pain", "injury", "MED-01"}),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-02",
        content=(
            "2024-04-25: MRI reveals medial meniscus tear.  "
            "Conservative treatment recommended first."
        ),
        observed_at=_utc(2024, 4, 25),
        entities=frozenset({
            "knee", "meniscus tear", "MRI", "diagnosis", "MED-02",
        }),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-03",
        content=(
            "2024-05-05: Physical therapy initiated.  Six-week "
            "conservative plan.  Ice, NSAIDs, guided exercises."
        ),
        observed_at=_utc(2024, 5, 5),
        entities=frozenset({
            "knee", "physical therapy", "PT", "NSAIDs", "MED-03",
        }),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-03a",
        content=(
            "2024-05-25: Mid-PT check-in.  Symptoms improving but not "
            "resolved.  Continue program."
        ),
        observed_at=_utc(2024, 5, 25),
        entities=frozenset({
            "knee", "physical therapy", "PT", "check-in", "MED-03a",
        }),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-04",
        content=(
            "2024-06-20: PT has not resolved symptoms.  Arthroscopic "
            "surgery scheduled for early July."
        ),
        observed_at=_utc(2024, 6, 20),
        entities=frozenset({
            "knee", "arthroscopic surgery", "PT", "MED-04",
        }),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-05",
        content=(
            "2024-07-08: Arthroscopic surgery performed successfully.  "
            "Meniscus repaired.  Hospital discharge the same day."
        ),
        observed_at=_utc(2024, 7, 8),
        entities=frozenset({
            "knee", "arthroscopic surgery", "meniscus", "MED-05",
        }),
        subject="knee_injury",
    ),
    Memory(
        mid="MED-06",
        content=(
            "2024-10-15: Full recovery confirmed.  Cleared to resume "
            "full weight-bearing activity and running."
        ),
        observed_at=_utc(2024, 10, 15),
        entities=frozenset({
            "knee", "recovery", "running", "MED-06",
        }),
        subject="knee_injury",
    ),
    # Tangential memory: different subject, dated after recovery.
    Memory(
        mid="MED-99",
        content=(
            "2025-01-10: Annual physical exam.  All vitals normal.  "
            "Brief note on prior knee issue but unrelated."
        ),
        observed_at=_utc(2025, 1, 10),
        entities=frozenset({
            "annual physical", "vitals", "knee", "MED-99",
        }),
        subject="routine_checkup",
    ),

    # ── Project lifecycle subject — feature rollout ───────────────
    Memory(
        mid="PROJ-01",
        content=(
            "2024-02-05: Payments modernization project kickoff.  "
            "Team of 6, target Q3 ship."
        ),
        observed_at=_utc(2024, 2, 5),
        entities=frozenset({
            "payments project", "kickoff", "planning", "PROJ-01",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-02",
        content=(
            "2024-03-20: Sprint 3 complete.  Core payment flow "
            "integrated.  On schedule."
        ),
        observed_at=_utc(2024, 3, 20),
        entities=frozenset({
            "payments project", "sprint 3", "payment flow", "PROJ-02",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-02a",
        content=(
            "2024-04-10: Stripe v1 deprecation notice received from "
            "vendor.  Engineering review underway."
        ),
        observed_at=_utc(2024, 4, 10),
        entities=frozenset({
            "payments project", "Stripe", "deprecation notice", "PROJ-02a",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-03",
        content=(
            "2024-05-02: Blocker identified — Stripe API deprecation "
            "affects settlement reconciliation."
        ),
        observed_at=_utc(2024, 5, 2),
        entities=frozenset({
            "payments project", "blocker", "Stripe", "PROJ-03",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-04",
        content=(
            "2024-05-28: Blocker resolved by migrating to Stripe v2 "
            "API.  Project back on track."
        ),
        observed_at=_utc(2024, 5, 28),
        entities=frozenset({
            "payments project", "blocker", "Stripe v2", "PROJ-04",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-05",
        content=(
            "2024-07-18: Beta launch to 5% of traffic.  Metrics "
            "within target."
        ),
        observed_at=_utc(2024, 7, 18),
        entities=frozenset({
            "payments project", "beta launch", "traffic", "PROJ-05",
        }),
        subject="payments_project",
    ),
    Memory(
        mid="PROJ-06",
        content=(
            "2024-09-15: General availability launch complete.  "
            "Project concluded."
        ),
        observed_at=_utc(2024, 9, 15),
        entities=frozenset({
            "payments project", "GA launch", "PROJ-06",
        }),
        subject="payments_project",
    ),
    # Tangential memory: different project same suffix.
    Memory(
        mid="PROJ-99",
        content=(
            "2024-11-02: Q4 roadmap planning — identity service "
            "modernization is the next major project."
        ),
        observed_at=_utc(2024, 11, 2),
        entities=frozenset({
            "roadmap", "identity service", "Q4", "PROJ-99",
        }),
        subject="identity_project",
    ),
]


# ── Typed edges (G_tr admissible transitions) ──────────────────────
#
# The authentication-subject zone evolves in four stages:
#   Zone 1 (session cookies):   ADR-001
#   Zone 2 (OAuth, then +JWT):   ADR-007 → ADR-012
#   Zone 3 (tokens, then +MFA):  ADR-021 → ADR-027
#   Zone 4 (passkeys):           ADR-029
#
# Zone boundaries = ``supersedes`` edges.  Within-zone extension =
# ``refines`` edges.  The logging subject has one memory (ADR-005)
# introduced with no successor — it's in its own trivial zone.
#
# ADR-033 gets NO edge — it's in a different subject ('auth_ops'), so
# it's not in the authentication G_tr at all.  This is the structural
# mechanism the LG retriever uses to exclude it.

EDGES: list[Edge] = [
    # ── authentication subject ────────────────────────────────────
    # Zone 1 → Zone 2: cookies retired, OAuth begins
    Edge(
        src="ADR-001", dst="ADR-007",
        transition="supersedes",
        retires_entities=frozenset({"session cookies"}),
    ),
    # Zone 2 internal: OAuth review, then JWT refinement
    Edge(src="ADR-007", dst="ADR-010", transition="refines"),
    Edge(src="ADR-010", dst="ADR-012", transition="refines"),
    # Zone 2 → Zone 3: JWT retired, short-lived tokens begin
    Edge(
        src="ADR-012", dst="ADR-021",
        transition="supersedes",
        retires_entities=frozenset({"JWT", "JSON Web Tokens"}),
    ),
    # Zone 3 internal: tokens refined with MFA
    Edge(src="ADR-021", dst="ADR-027", transition="refines"),
    # Zone 3 → Zone 4: tokens + MFA retired, passkeys begin
    Edge(
        src="ADR-021", dst="ADR-029",
        transition="supersedes",
        retires_entities=frozenset({
            "access tokens", "refresh tokens", "password",
        }),
    ),
    Edge(
        src="ADR-027", dst="ADR-029",
        transition="supersedes",
        retires_entities=frozenset({"MFA", "multi-factor authentication"}),
    ),

    # ── logging subject ───────────────────────────────────────────
    # ADR-005 is introduced with no predecessor and has no successor
    # in the corpus — single-memory zone.  No edges needed.

    # ── other subjects: database, rate_limiting, caching, auth_ops ─
    # Single memories each, no edges.

    # ── medical (knee_injury) ─────────────────────────────────────
    # Injury onset → diagnosis → PT → surgery → recovery
    Edge(src="MED-01", dst="MED-02", transition="refines"),    # diagnosis
    Edge(src="MED-02", dst="MED-03", transition="refines"),    # PT initiated
    Edge(src="MED-03", dst="MED-03a", transition="refines"),   # mid-PT check-in
    Edge(
        src="MED-03a", dst="MED-04",
        transition="supersedes",
        # PT decision reversed at MED-04: surgery scheduled, NSAIDs
        # discontinued.  Both aliases are named so the retirement
        # query resolves for either surface form.
        retires_entities=frozenset({
            "NSAIDs", "PT", "physical therapy",
        }),
    ),
    Edge(src="MED-04", dst="MED-05", transition="refines"),    # surgery
    Edge(
        src="MED-05", dst="MED-06",
        transition="supersedes",
        retires_entities=frozenset({"arthroscopic surgery"}),
    ),   # recovery

    # ── project lifecycle (payments_project) ──────────────────────
    # Kickoff → sprint → blocker → resolution → beta → GA
    Edge(src="PROJ-01", dst="PROJ-02", transition="refines"),
    Edge(src="PROJ-02", dst="PROJ-02a", transition="refines"),   # stripe deprecation
    Edge(src="PROJ-02a", dst="PROJ-03", transition="refines"),   # blocker reported
    Edge(
        src="PROJ-03", dst="PROJ-04",
        transition="supersedes",
        # "Delay" as an alias for "blocker" — an explicit synonym
        # recorded by (would-be) reconciliation when the user's
        # mental model differs from the ingested entity name.
        retires_entities=frozenset({"blocker", "delay"}),
    ),   # blocker resolved
    Edge(src="PROJ-04", dst="PROJ-05", transition="refines"),    # beta launched
    Edge(
        src="PROJ-05", dst="PROJ-06",
        transition="supersedes",
        retires_entities=frozenset({"beta launch"}),
    ),   # GA
]


def corpus_by_id() -> dict[str, Memory]:
    return {m.mid: m for m in ADR_CORPUS}


def auth_trajectory() -> list[str]:
    """The ground-truth authentication trajectory — used by evaluation
    to sanity-check that the path-rerank finds the right chain.

    Not passed to any retriever; just a reference.  Note ADR-033 is
    deliberately EXCLUDED — it's a procedural doc dated after ADR-029
    but not part of the authentication design evolution.  A correct
    path-rerank should rank ADR-029 (not ADR-033) at top for
    'current authentication?' queries."""
    return ["ADR-001", "ADR-007", "ADR-012", "ADR-021", "ADR-027", "ADR-029"]
