"""Hand-curated queries with gold answer memory IDs.

Each query is hand-labeled with the single memory the system should
surface at rank 1.  Query shapes cover the taxonomy that motivates the
experiment:

* ``current_state``: current-X / X-now / unqualified-but-implicitly-current.
  These are the main target for the path-rerank — expected answer is
  the end-of-chain.
* ``ordinal_last``: "latest X" — already handled by Phase B ordinal.
  Included so we verify path-rerank doesn't regress these.
* ``ordinal_first``: "first X" / "original X" — already handled by
  Phase B ordinal.  Regression guard.
* ``causal_chain``: "what led to X" / "what came before X".  A
  stretch goal for the path-rerank (chain predecessors).  Included
  to see if the algorithm accidentally handles them too.
* ``noise``: query with no obvious chronological answer (BM25
  baseline should do fine).  Regression guard.

Gold answers chosen so that:

* All queries' answers are in the corpus (no retrieval ceiling).
* BM25 alone will often pick the wrong memory (especially for current-
  state questions where all auth ADRs match the query tokens).
* Ordinal primitive will get ordinal_first/last correct but may miss
  current_state.
* Path-rerank should get current_state AND ordinal by walking the
  chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QueryShape = Literal[
    "current_state",
    "ordinal_last",
    "ordinal_first",
    "causal_chain",
    "noise",
    "sequence",
    "predecessor",
    "interval",
    "before_named",
    "transitive_cause",
    "concurrent",
]


@dataclass(frozen=True)
class Query:
    text: str
    shape: QueryShape
    gold_mid: str
    notes: str = ""


QUERIES: list[Query] = [
    # ── Current-state — main target ───────────────────────────────────
    Query(
        text="What authentication does the system currently use?",
        shape="current_state",
        gold_mid="ADR-029",
        notes="Bare 'currently' marker. End of auth chain.",
    ),
    Query(
        text="What is the current authentication mechanism?",
        shape="current_state",
        gold_mid="ADR-029",
        notes="Classic 'current X' — passkeys + WebAuthn.",
    ),
    Query(
        text="As of today, how do users authenticate?",
        shape="current_state",
        gold_mid="ADR-029",
        notes="'as of today' marker.",
    ),
    Query(
        text="Do we still use JWT for API authentication?",
        shape="current_state",
        gold_mid="ADR-021",
        notes=(
            "Asks about JWT specifically — the answer is the ADR that "
            "retired long-lived JWTs (ADR-021).  JWT still appears in "
            "ADR-012 (introduced) and ADR-021 (retired); the current "
            "state is captured in ADR-021."
        ),
    ),

    # ── Ordinal last — regression guard ───────────────────────────────
    Query(
        text="What was the latest authentication decision?",
        shape="ordinal_last",
        gold_mid="ADR-029",
        notes="'latest' → ORDINAL_LAST.  Should still work.",
    ),

    # ── Ordinal first — regression guard ──────────────────────────────
    Query(
        text="What was the original authentication design?",
        shape="ordinal_first",
        gold_mid="ADR-001",
        notes="'original' → ORDINAL_FIRST.  Session cookies.",
    ),

    # ── Causal chain — stretch goal ───────────────────────────────────
    Query(
        text="What led to the decision to retire JWT?",
        shape="causal_chain",
        gold_mid="ADR-021",
        notes=(
            "The memory DESCRIBING the retirement is the answer.  "
            "Path-rerank could surface predecessors too; we only check "
            "rank-1 for now."
        ),
    ),

    # ── Noise — regression guard ──────────────────────────────────────
    Query(
        text="What is our logging format?",
        shape="noise",
        gold_mid="ADR-005",
        notes="Non-authentication query.  BM25 alone should nail it.",
    ),

    # ══════════════════════════════════════════════════════════════════
    # Medical timeline (knee_injury subject)
    # ══════════════════════════════════════════════════════════════════

    Query(
        text="What is the current status of the knee?",
        shape="current_state",
        gold_mid="MED-06",
        notes="Current knee state = recovered.  Terminal of zone.",
    ),
    Query(
        text="Is the patient currently in physical therapy?",
        shape="current_state",
        gold_mid="MED-04",
        notes=(
            "'Currently in PT?' asks about the PT state's currency.  "
            "PT was ended at MED-04.  So the memory that ended PT is "
            "the authoritative answer."
        ),
    ),
    Query(
        text="When was the knee injury first reported?",
        shape="ordinal_first",
        gold_mid="MED-01",
        notes="Origin of knee_injury zone.",
    ),
    Query(
        text="What was the initial diagnosis for the knee?",
        shape="ordinal_first",
        gold_mid="MED-02",
        notes=(
            "Ambiguous — 'initial diagnosis' could be the onset "
            "(MED-01) or the first diagnostic finding (MED-02, MRI).  "
            "We label MED-02 as gold since it's the literal diagnosis "
            "memory.  This tests intent-classifier disambiguation."
        ),
    ),
    Query(
        text="What led to the surgery?",
        shape="causal_chain",
        gold_mid="MED-04",
        notes=(
            "Memory where surgery was decided upon (PT insufficient)."
        ),
    ),
    Query(
        text="Do we still have the patient on NSAIDs?",
        shape="current_state",
        gold_mid="MED-04",
        notes=(
            "NSAIDs were part of PT (MED-03), retired at MED-04 when "
            "surgery replaced PT.  Grammar should identify MED-04 as "
            "the retirement memory."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════
    # Project lifecycle (payments_project subject)
    # ══════════════════════════════════════════════════════════════════

    Query(
        text="What is the current status of the payments project?",
        shape="current_state",
        gold_mid="PROJ-06",
        notes="Current = GA-launched.  Terminal of payments_project zone.",
    ),
    Query(
        text="Is the payments project still blocked?",
        shape="current_state",
        gold_mid="PROJ-04",
        notes=(
            "'Still blocked?' — blocker was retired at PROJ-04.  "
            "Grammar should identify the retirement memory."
        ),
    ),
    Query(
        text="When did the payments project start?",
        shape="ordinal_first",
        gold_mid="PROJ-01",
        notes="Origin of payments_project zone.",
    ),
    Query(
        text="What caused the delay on payments?",
        shape="causal_chain",
        gold_mid="PROJ-03",
        notes=(
            "Memory identifying the blocker.  Stress-tests whether "
            "the intent classifier recognizes 'what caused' as "
            "causal-chain shape."
        ),
    ),

    # ── Adversarial: subject mismatch ─────────────────────────────────
    Query(
        text="What is the current status of the knee injury patient?",
        shape="current_state",
        gold_mid="MED-06",
        notes=(
            "Same as current-status-knee but with distracting "
            "'patient' token.  Tests subject inference robustness."
        ),
    ),
    Query(
        text="What is our next project on the roadmap?",
        shape="current_state",
        gold_mid="PROJ-99",
        notes=(
            "Different subject (identity_project) than the payments "
            "one.  Should be answered by the identity_project zone's "
            "terminal — PROJ-99 is a single-memory zone.  Tests "
            "multi-subject classifier."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════
    # Range intent (NEW — proves the grammar extends beyond the
    # original 5 intent families).  Query returns memories in subject
    # with observed_at ∈ range.  gold_mid is the memory expected at
    # rank 1 within the filtered set.
    # ══════════════════════════════════════════════════════════════════

    Query(
        text="What authentication decisions did we make in 2024?",
        shape="current_state",  # reuse shape taxonomy; new intent in LG
        gold_mid="ADR-010",
        notes=(
            "Range intent: 'in 2024' restricts to authentication "
            "memories with observed_at in 2024.  Auth-subject 2024 "
            "memories: ADR-010 (Jan 15), ADR-012 (Feb 20).  Earliest "
            "(ADR-010) ranks 1 under range's chronological ordering."
        ),
    ),
    Query(
        text="What happened on the payments project in Q2 2024?",
        shape="current_state",
        gold_mid="PROJ-02a",
        notes=(
            "Range intent: Q2 2024 = April-June.  Payments-subject "
            "memories in Q2: PROJ-02a (April 10), PROJ-03 (May 2), "
            "PROJ-04 (May 28).  Earliest (PROJ-02a) ranks 1."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════
    # Memory-return intents (new grammar productions)
    # ══════════════════════════════════════════════════════════════════

    # ── sequence: "what came after X" ────────────────────────────────
    Query(
        text="What came right after OAuth in authentication?",
        shape="sequence",
        gold_mid="ADR-010",
        notes=(
            "Direct chain successor of the OAuth-introducing memory "
            "(ADR-007).  Edge ADR-007→ADR-010 (refines)."
        ),
    ),
    Query(
        text="What happened after the knee MRI?",
        shape="sequence",
        gold_mid="MED-03",
        notes="Successor of MED-02 (MRI) = MED-03 (PT initiated).",
    ),

    # ── predecessor: "what came before X" ────────────────────────────
    Query(
        text="What came before MFA?",
        shape="predecessor",
        gold_mid="ADR-021",
        notes=(
            "ADR-027 introduced MFA; predecessor via "
            "ADR-021→ADR-027 refines edge = ADR-021."
        ),
    ),
    Query(
        text="What was the step before surgery?",
        shape="predecessor",
        gold_mid="MED-03a",
        notes=(
            "MED-05 is the surgery-performed memory; its direct "
            "predecessor is MED-04 (scheduling).  But we're asking "
            "about 'surgery' generally — which lands on MED-04 first "
            "(the scheduling memory contains 'surgery' in entities).  "
            "Predecessor of MED-04 via MED-03a→MED-04 supersedes = "
            "MED-03a (mid-PT check-in)."
        ),
    ),

    # ── interval: "between X and Y" ──────────────────────────────────
    Query(
        text="What happened between the kickoff and the blocker on payments?",
        shape="interval",
        gold_mid="PROJ-02",
        notes=(
            "Payments memories strictly between PROJ-01 (kickoff, "
            "Feb 5) and PROJ-03 (blocker, May 2): PROJ-02 (March 20) "
            "and PROJ-02a (April 10).  PROJ-02 is earliest."
        ),
    ),
    Query(
        text="What happened between the MRI and the surgery?",
        shape="interval",
        gold_mid="MED-03",
        notes=(
            "Knee memories strictly between MED-02 (MRI, April 25) "
            "and MED-05 (surgery performed, July 8): MED-03, MED-03a, "
            "MED-04.  MED-03 is earliest."
        ),
    ),

    # ── before_named: "did X come before Y" ──────────────────────────
    Query(
        text="Did OAuth come before JWT?",
        shape="before_named",
        gold_mid="ADR-007",
        notes=(
            "ADR-007 (OAuth, Nov 2023) observed_at < ADR-012 (JWT, "
            "Feb 2024).  Verdict: yes.  Grammar answer = X's memory "
            "(ADR-007) to let user inspect."
        ),
    ),

    # ── transitive_cause: "what eventually led to X" ─────────────────
    Query(
        text="What eventually led to passkeys?",
        shape="transitive_cause",
        gold_mid="ADR-001",
        notes=(
            "Walk predecessors of ADR-029 (passkeys) through "
            "admissible edges: ADR-029 ← ADR-021 ← ADR-012 ← ADR-010 "
            "← ADR-007 ← ADR-001 (session cookies, the root).  "
            "Root = ADR-001."
        ),
    ),
    Query(
        text="What eventually led to recovery?",
        shape="transitive_cause",
        gold_mid="MED-01",
        notes=(
            "Walk predecessors of MED-06 (recovery) through "
            "admissible edges to root MED-01 (injury report)."
        ),
    ),

    # ── concurrent: "what else was happening during X" ───────────────
    Query(
        text="What else was happening during the Stripe blocker?",
        shape="concurrent",
        gold_mid="MED-03",
        notes=(
            "PROJ-03 (blocker, May 2 2024) in payments_project.  "
            "Closest cross-subject memory within ±30d: MED-03 "
            "(physical therapy initiated, May 5 2024) in knee_injury."
        ),
    ),
]


def queries_by_shape(shape: QueryShape) -> list[Query]:
    return [q for q in QUERIES if q.shape == shape]
