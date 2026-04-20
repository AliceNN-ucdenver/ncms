"""MSEB-Convo miner — LongMemEval sessions → message tuples.

Phase 1 of the MSEB-Convo pipeline.  Loads LongMemEval's cached
JSON files (already downloaded by
``benchmarks.longmemeval.loader.download_longmemeval``) and
explodes each user's sessions into one **raw message JSONL** per
user.

LongMemEval structure (cleaned variant)::

    [
      {
        "question_id": "...",
        "question": "...",
        "question_type": "single-session-preference" | ...,
        "haystack_session_ids": [...],
        "haystack_dates":       [...],
        "haystack_sessions":    [[{"role":"user","content":"..."}, ...], ...],
        "answer_session_ids":   [...],
        "answer":               "..."
      }, ...
    ]

We flatten each question's haystack into the subject chain
(``convo-<user_id>``).  One memory = one assistant-or-user turn.
The accompanying question row is exported separately into
``raw/_questions.jsonl`` so ``label.py`` + the gold-query author
can see which sessions each LMEval question points at.

Phase 1 emits **un-labeled** messages; Phase 2 (``label.py``)
adds the ``MemoryKind`` + ``PreferenceKind`` classifications.

Output layout::

    raw/
    ├── convo-<user_id>.jsonl     ← one file per subject chain
    ├── _questions.jsonl          ← the LMEval question rows (for gold authoring)
    └── _stats.json               ← mining summary

Usage::

    # pilot — 50 users
    uv run python -m benchmarks.mseb_convo.mine --limit 50

    # full scale — all 500 users
    uv run python -m benchmarks.mseb_convo.mine --limit 500
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from benchmarks.env import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger("mseb_convo.mine")

DEFAULT_OUT = Path(__file__).parent / "raw"
DEFAULT_SOURCE = Path(
    "benchmarks/results/.cache/longmemeval/longmemeval_oracle.json",
)


def _iso(ts: str | None) -> str:
    """Normalise LongMemEval session dates to ISO-8601 UTC.

    LMEval ``haystack_dates`` are e.g. ``"2023/05/20 (Sat) 10:00"``
    — we keep only the date part, pin to UTC midnight.
    """
    if not ts:
        return "1970-01-01T00:00:00Z"
    try:
        date_part = ts.split("(")[0].strip().split()[0]  # "2023/05/20"
        dt = datetime.strptime(date_part, "%Y/%m/%d").replace(tzinfo=UTC)
        return dt.isoformat().replace("+00:00", "Z")
    except (ValueError, IndexError):
        logger.debug("unparseable LMEval date %r → epoch fallback", ts)
        return "1970-01-01T00:00:00Z"


def _user_id_from_question(q: dict) -> str:
    """Derive a stable subject ID from an LMEval question row.

    LMEval doesn't carry explicit user IDs — each question is a
    self-contained haystack.  We key the subject by the
    ``question_id`` truncated to 8 chars so one question == one
    subject chain.  (Preference questions can share a user's
    full haystack; separating by question keeps provenance clean.)
    """
    qid = q.get("question_id", "unknown")
    return f"user-{qid[:8]}"


def _session_to_messages(
    user: str,
    session_id: str,
    date_str: str,
    turns: list[dict],
    *,
    msg_seq_start: int,
) -> tuple[list[dict], int]:
    """Explode one session's turns into message tuples.

    Returns ``(messages, next_msg_seq)``.  Each message carries:
    - ``message_id`` — stable per-user-per-turn ID
    - ``text`` — turn content
    - ``timestamp`` — session date (LMEval granularity stops here)
    - ``source`` — ``user_turn`` or ``assistant_turn``
    - ``session_id`` + ``turn_index`` for traceability
    """
    ts = _iso(date_str)
    messages: list[dict] = []
    seq = msg_seq_start

    for turn_index, turn in enumerate(turns):
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        role = turn.get("role", "unknown")
        source = {
            "user": "user_turn",
            "assistant": "assistant_turn",
        }.get(role, f"{role}_turn")
        seq += 1
        messages.append({
            "message_id": f"{user}::m{seq:04d}",
            "text": content[:4000],
            "timestamp": ts,
            "source": source,
            "session_id": session_id,
            "turn_index": turn_index,
            "role": role,
        })
    return messages, seq


def mine(
    *,
    limit: int,
    out_dir: Path,
    source: Path = DEFAULT_SOURCE,
    question_types: list[str] | None = None,
    shuffle_seed: int | None = None,
) -> dict:
    """Load LMEval JSON, explode sessions, emit per-user JSONL.

    Args:
        question_types: If set, only mine questions whose
            ``question_type`` is in this list (e.g.
            ``["single-session-preference"]`` to hit the
            preference subset).  Useful for pilots that want
            preference-sub-type coverage without scanning all 500.
        shuffle_seed: If set, shuffle the LMEval rows with this
            seed before slicing to ``limit``.  The default
            (``None``) preserves LMEval's native order — which is
            sorted by ``question_type``, so an unshuffled pilot
            gets only the first type's questions.

    Returns stats dict with ``users``, ``messages``, ``per_source``,
    ``per_question_type``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        # Fall back to the loader — downloads if missing.
        from benchmarks.longmemeval.loader import download_longmemeval
        data_dir = download_longmemeval()
        source = data_dir / "longmemeval_oracle.json"
        if not source.exists():
            raise FileNotFoundError(
                f"LongMemEval cache missing at {source} — run "
                "`uv run python -m benchmarks longmemeval --test` "
                "to populate it first.",
            )

    logger.info("Loading LongMemEval from %s …", source)
    data = json.loads(source.read_text())
    logger.info("Loaded %d question rows", len(data))

    if question_types:
        kept = [q for q in data if q.get("question_type") in question_types]
        logger.info(
            "filtered to question_types=%s → %d rows (of %d)",
            question_types, len(kept), len(data),
        )
        data = kept

    if shuffle_seed is not None:
        import random
        random.Random(shuffle_seed).shuffle(data)
        logger.info("shuffled %d rows with seed=%d", len(data), shuffle_seed)

    questions_out = out_dir / "_questions.jsonl"
    stats = {
        "users": 0,
        "messages": 0,
        "per_source": {},
        "per_question_type": {},
        "total_questions_seen": len(data),
    }

    with questions_out.open("w", encoding="utf-8") as qfh:
        for i, q in enumerate(data):
            if i >= limit:
                break

            user = _user_id_from_question(q)
            sessions = q.get("haystack_sessions") or []
            session_ids = q.get("haystack_session_ids") or []
            dates = q.get("haystack_dates") or []

            # Guard against schema drift.
            if len(sessions) != len(session_ids) or len(sessions) != len(dates):
                logger.warning(
                    "question %s: haystack length mismatch sessions=%d "
                    "ids=%d dates=%d — skipping",
                    q.get("question_id"), len(sessions),
                    len(session_ids), len(dates),
                )
                continue

            messages: list[dict] = []
            seq = 0
            for sid, sdate, turns in zip(
                session_ids, dates, sessions, strict=False,
            ):
                sess_msgs, seq = _session_to_messages(
                    user=user,
                    session_id=str(sid),
                    date_str=str(sdate),
                    turns=turns or [],
                    msg_seq_start=seq,
                )
                messages.extend(sess_msgs)

            if not messages:
                logger.warning(
                    "question %s produced 0 messages — skipping", user,
                )
                continue

            # Emit per-user JSONL.
            out_path = out_dir / f"{user}.jsonl"
            with out_path.open("w", encoding="utf-8") as fh:
                for msg in messages:
                    fh.write(json.dumps(msg, ensure_ascii=False))
                    fh.write("\n")

            # Record the question itself — the gold author + labeler
            # both need this to produce gold queries that point at
            # specific messages.
            qfh.write(json.dumps({
                "question_id": q.get("question_id"),
                "question": q.get("question"),
                "question_type": q.get("question_type"),
                "answer": q.get("answer"),
                "answer_session_ids": q.get("answer_session_ids"),
                "subject": user,
                "message_count": len(messages),
            }, ensure_ascii=False))
            qfh.write("\n")

            stats["users"] += 1
            stats["messages"] += len(messages)
            for msg in messages:
                stats["per_source"].setdefault(msg["source"], 0)
                stats["per_source"][msg["source"]] += 1
            qtype = q.get("question_type", "unknown")
            stats["per_question_type"].setdefault(qtype, 0)
            stats["per_question_type"][qtype] += 1

            if (i + 1) % 25 == 0 or (i + 1) == limit:
                logger.info(
                    "[%d/%d] subject=%s msgs=%d qtype=%s",
                    i + 1, limit, user, len(messages), qtype,
                )

    (out_dir / "_stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True),
    )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="MSEB-Convo miner: LongMemEval sessions → raw messages",
    )
    parser.add_argument("--limit", type=int, default=50,
                        help="Max questions/subjects to mine (default 50)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="LMEval JSON file (default: oracle variant)")
    parser.add_argument(
        "--question-types", nargs="*", default=None,
        help="Only mine questions whose type is in this list "
             "(e.g. --question-types single-session-preference temporal-reasoning)",
    )
    parser.add_argument(
        "--shuffle-seed", type=int, default=None,
        help="Shuffle LMEval rows with this seed before slicing to --limit "
             "(so pilot gets a balanced question-type mix)",
    )
    args = parser.parse_args()

    stats = mine(
        limit=args.limit,
        out_dir=args.out_dir,
        source=args.source,
        question_types=args.question_types,
        shuffle_seed=args.shuffle_seed,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
