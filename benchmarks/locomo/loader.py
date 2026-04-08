"""LoCoMo dataset loader — clone repo and parse locomo10.json.

Data source: https://github.com/snap-research/locomo
Paper: "LoCoMo: Long Context Conversational Memory Benchmark"
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

LOCOMO_REPO_URL = "https://github.com/snap-research/locomo.git"
DEFAULT_CACHE_DIR = Path("benchmarks/results/.cache")


@dataclass
class ConversationTurn:
    """A single turn in a LoCoMo conversation."""

    turn_id: int
    role: str
    content: str
    session_id: str
    conversation_id: str


@dataclass
class Conversation:
    """A LoCoMo conversation with multiple turns."""

    conversation_id: str
    turns: list[ConversationTurn] = field(default_factory=list)


@dataclass
class QAQuestion:
    """A question from the LoCoMo evaluation set."""

    question_id: str
    question: str
    answer: str
    category: str
    conversation_id: str
    evidence_turn_ids: list[int] = field(default_factory=list)


def download_locomo(cache_dir: Path | None = None) -> Path:
    """Clone or update the LoCoMo repository.

    Args:
        cache_dir: Directory for cached data. Defaults to benchmarks/results/.cache.

    Returns:
        Path to the cloned repository root.

    Raises:
        RuntimeError: If git clone/pull fails.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    repo_dir = cache_dir / "locomo"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if (repo_dir / ".git").is_dir():
        logger.info("LoCoMo repo already cloned at %s, pulling latest...", repo_dir)
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.warning("git pull failed (non-fatal): %s", result.stderr.strip())
    else:
        logger.info("Cloning LoCoMo repo to %s...", repo_dir)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", LOCOMO_REPO_URL, str(repo_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            msg = f"Failed to clone LoCoMo repo: {result.stderr.strip()}"
            raise RuntimeError(msg)
        logger.info("Clone complete.")

    return repo_dir


def load_locomo_dataset(
    cache_dir: Path | None = None,
) -> tuple[list[Conversation], list[QAQuestion]]:
    """Download and parse the LoCoMo dataset.

    Parses ``data/locomo10.json`` from the LoCoMo repo.  Each entry in the
    JSON array represents one conversation with embedded QA pairs.

    Args:
        cache_dir: Directory for cached data.

    Returns:
        Tuple of (conversations, questions).

    Raises:
        FileNotFoundError: If the data file cannot be found after download.
    """
    repo_dir = download_locomo(cache_dir)

    # The dataset file may be at data/locomo10.json or locomo10.json
    candidates = [
        repo_dir / "data" / "locomo10.json",
        repo_dir / "locomo10.json",
    ]
    data_file: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            data_file = candidate
            break

    if data_file is None:
        # List what files exist to aid debugging
        existing = list(repo_dir.rglob("*.json"))[:20]
        existing_str = ", ".join(str(f.relative_to(repo_dir)) for f in existing)
        msg = (
            f"Cannot find locomo10.json in {repo_dir}. "
            f"Available JSON files: {existing_str or 'none'}"
        )
        raise FileNotFoundError(msg)

    logger.info("Loading LoCoMo data from %s", data_file)
    with open(data_file) as f:
        raw_data = json.load(f)

    conversations: list[Conversation] = []
    questions: list[QAQuestion] = []

    # Handle both list-of-conversations and dict-keyed-by-id formats
    if isinstance(raw_data, dict):
        items = list(raw_data.items())
    elif isinstance(raw_data, list):
        items = [(str(i), entry) for i, entry in enumerate(raw_data)]
    else:
        msg = f"Unexpected top-level JSON type: {type(raw_data)}"
        raise ValueError(msg)

    for conv_id, entry in items:
        # If items came from a list, entry is the conversation dict and conv_id is index
        if isinstance(entry, dict):
            cid = entry.get("conversation_id", entry.get("id", str(conv_id)))
        else:
            continue

        # Parse conversation turns
        conv = Conversation(conversation_id=str(cid))

        raw_turns = entry.get("conversation", entry.get("turns", entry.get("dialog", [])))
        for turn_idx, turn in enumerate(raw_turns):
            if isinstance(turn, dict):
                role = turn.get("role", turn.get("speaker", "unknown"))
                content = turn.get("content", turn.get("text", turn.get("utterance", "")))
                session_id = str(turn.get("session_id", turn.get("session", "0")))
            elif isinstance(turn, str):
                role = "user" if turn_idx % 2 == 0 else "assistant"
                content = turn
                session_id = "0"
            else:
                continue

            conv.turns.append(ConversationTurn(
                turn_id=turn_idx,
                role=str(role),
                content=str(content),
                session_id=session_id,
                conversation_id=str(cid),
            ))

        conversations.append(conv)

        # Parse QA questions
        raw_questions = entry.get("questions", entry.get("qa", entry.get("qas", [])))
        for q_idx, q in enumerate(raw_questions):
            if not isinstance(q, dict):
                continue

            qid = q.get("question_id", q.get("id", f"{cid}_q{q_idx}"))
            question_text = q.get("question", q.get("query", ""))
            answer_text = q.get("answer", q.get("response", ""))
            category = q.get("category", q.get("type", q.get("reasoning", "unknown")))

            # Evidence turn IDs may be stored under various keys
            evidence = q.get("evidence_turn_ids", q.get("evidence", q.get("turns", [])))
            if isinstance(evidence, list):
                evidence_ids = [int(e) for e in evidence if _is_int(e)]
            else:
                evidence_ids = []

            questions.append(QAQuestion(
                question_id=str(qid),
                question=str(question_text),
                answer=str(answer_text),
                category=str(category),
                conversation_id=str(cid),
                evidence_turn_ids=evidence_ids,
            ))

    logger.info(
        "Loaded %d conversations (%d total turns) and %d questions",
        len(conversations),
        sum(len(c.turns) for c in conversations),
        len(questions),
    )

    return conversations, questions


def _is_int(value: object) -> bool:
    """Check if a value can be safely converted to int."""
    try:
        int(value)  # type: ignore[arg-type]
        return True
    except (ValueError, TypeError):
        return False
