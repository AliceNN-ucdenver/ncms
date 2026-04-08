"""LongMemEval dataset loader — download from HuggingFace and parse.

Data source: https://huggingface.co/datasets/xiaowu0162/LongMemEval
Paper: "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory"
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

HF_DATASET_ID = "xiaowu0162/LongMemEval"
# Direct raw download URLs for the dataset files on HuggingFace
HF_RAW_BASE = (
    "https://huggingface.co/datasets/xiaowu0162/LongMemEval/resolve/main"
)
DEFAULT_CACHE_DIR = Path("benchmarks/results/.cache")

# Known dataset files to attempt downloading
DATASET_FILES = [
    "data/longmemeval.json",
    "longmemeval.json",
    "data/test.json",
    "test.json",
    "data/questions.json",
    "data/longmemeval_s1.json",
    "data/longmemeval_s2.json",
    "data/longmemeval_s3.json",
]


@dataclass
class Session:
    """A chat session in LongMemEval."""

    session_id: str
    turns: list[SessionTurn] = field(default_factory=list)


@dataclass
class SessionTurn:
    """A single turn in a LongMemEval chat session."""

    turn_id: int
    role: str
    content: str
    session_id: str


@dataclass
class LongMemQuestion:
    """A question from the LongMemEval evaluation set."""

    question_id: str
    question: str
    answer: str
    category: str  # information_extraction, multi_session, temporal, knowledge_update, abstention
    session_ids: list[str] = field(default_factory=list)


def download_longmemeval(cache_dir: Path | None = None) -> Path:
    """Download the LongMemEval dataset from HuggingFace.

    Tries ``huggingface_hub`` first; falls back to direct HTTP download
    if the library is not installed.

    Args:
        cache_dir: Directory for cached data. Defaults to benchmarks/results/.cache.

    Returns:
        Path to the data directory containing JSON files.

    Raises:
        RuntimeError: If download fails via all methods.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    data_dir = cache_dir / "longmemeval"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Check if we already have data
    existing_json = list(data_dir.glob("*.json"))
    if existing_json:
        logger.info(
            "LongMemEval data already cached at %s (%d files)", data_dir, len(existing_json),
        )
        return data_dir

    # Try huggingface_hub first
    if _try_hf_hub_download(data_dir):
        return data_dir

    # Fallback: direct HTTP download
    logger.info("huggingface_hub not available, trying direct HTTP download...")
    if _try_http_download(data_dir):
        return data_dir

    msg = (
        "Failed to download LongMemEval dataset. "
        "Install huggingface_hub (`pip install huggingface_hub`) or "
        "manually download from https://huggingface.co/datasets/xiaowu0162/LongMemEval "
        f"and place JSON files in {data_dir}"
    )
    raise RuntimeError(msg)


def _try_hf_hub_download(data_dir: Path) -> bool:
    """Attempt download via huggingface_hub library."""
    try:
        from huggingface_hub import hf_hub_download, list_repo_files  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("huggingface_hub not installed")
        return False

    try:
        # List files in the dataset repo to find the right ones
        files = list_repo_files(HF_DATASET_ID, repo_type="dataset")
        json_files = [f for f in files if f.endswith(".json")]
        logger.info("Found %d JSON files in HF repo: %s", len(json_files), json_files[:10])

        downloaded = 0
        for fname in json_files:
            local_path = hf_hub_download(
                HF_DATASET_ID,
                filename=fname,
                repo_type="dataset",
                local_dir=str(data_dir),
            )
            logger.info("Downloaded %s -> %s", fname, local_path)
            downloaded += 1

        if downloaded > 0:
            logger.info("Downloaded %d files via huggingface_hub", downloaded)
            return True

        logger.warning("No JSON files found in HF dataset repo")
        return False

    except Exception:
        logger.warning("huggingface_hub download failed", exc_info=True)
        return False


def _try_http_download(data_dir: Path) -> bool:
    """Attempt direct HTTP download from HuggingFace raw URLs."""
    downloaded = 0
    for file_path in DATASET_FILES:
        url = f"{HF_RAW_BASE}/{file_path}"
        local_name = file_path.rsplit("/", 1)[-1]
        local_path = data_dir / local_name

        if local_path.exists():
            downloaded += 1
            continue

        try:
            logger.info("Trying %s ...", url)
            req = urllib.request.Request(url, headers={"User-Agent": "ncms-benchmark/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()

            # Validate JSON
            json.loads(content)
            local_path.write_bytes(content)
            logger.info("Downloaded %s (%d bytes)", local_name, len(content))
            downloaded += 1
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            logger.debug("HTTP download failed for %s: %s", url, e)
            continue
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Downloaded %s but invalid JSON: %s", url, e)
            local_path.unlink(missing_ok=True)
            continue

    if downloaded > 0:
        logger.info("Downloaded %d files via HTTP", downloaded)
        return True

    return False


def load_longmemeval_dataset(
    cache_dir: Path | None = None,
) -> tuple[list[Session], list[LongMemQuestion]]:
    """Download and parse the LongMemEval dataset.

    Args:
        cache_dir: Directory for cached data.

    Returns:
        Tuple of (sessions, questions).

    Raises:
        FileNotFoundError: If no data files are found after download.
    """
    data_dir = download_longmemeval(cache_dir)

    # Find JSON files
    json_files = sorted(data_dir.rglob("*.json"))
    if not json_files:
        msg = f"No JSON files found in {data_dir}"
        raise FileNotFoundError(msg)

    logger.info("Found %d JSON files: %s", len(json_files), [f.name for f in json_files])

    sessions: list[Session] = []
    questions: list[LongMemQuestion] = []

    for json_file in json_files:
        logger.info("Parsing %s...", json_file.name)
        with open(json_file) as f:
            raw_data = json.load(f)

        _parse_longmemeval_data(raw_data, sessions, questions, json_file.stem)

    logger.info(
        "Loaded %d sessions (%d total turns) and %d questions",
        len(sessions),
        sum(len(s.turns) for s in sessions),
        len(questions),
    )

    return sessions, questions


def _parse_longmemeval_data(
    raw_data: object,
    sessions: list[Session],
    questions: list[LongMemQuestion],
    file_stem: str,
) -> None:
    """Parse raw JSON data into sessions and questions.

    Handles multiple possible data formats:
    - List of question objects with embedded chat history
    - Dict with separate sessions and questions keys
    - List of session objects
    """
    if isinstance(raw_data, list):
        for idx, item in enumerate(raw_data):
            if not isinstance(item, dict):
                continue

            # Check if this is a question entry (has question + answer fields)
            if "question" in item or "query" in item:
                _parse_question_entry(item, questions, sessions, file_stem, idx)
            elif "turns" in item or "dialog" in item or "messages" in item:
                _parse_session_entry(item, sessions, file_stem, idx)

    elif isinstance(raw_data, dict):
        # Dict format: may have "data", "sessions", "questions" keys
        data_list = raw_data.get("data", raw_data.get("examples", []))
        if isinstance(data_list, list):
            _parse_longmemeval_data(data_list, sessions, questions, file_stem)

        raw_sessions = raw_data.get("sessions", raw_data.get("conversations", []))
        if isinstance(raw_sessions, list):
            for idx, s in enumerate(raw_sessions):
                if isinstance(s, dict):
                    _parse_session_entry(s, sessions, file_stem, idx)

        raw_questions = raw_data.get("questions", raw_data.get("queries", []))
        if isinstance(raw_questions, list):
            for idx, q in enumerate(raw_questions):
                if isinstance(q, dict):
                    _parse_question_entry(q, questions, sessions, file_stem, idx)


def _parse_session_entry(
    item: dict,
    sessions: list[Session],
    file_stem: str,
    idx: int,
) -> None:
    """Parse a single session entry."""
    sid = str(item.get("session_id", item.get("id", f"{file_stem}_s{idx}")))
    session = Session(session_id=sid)

    raw_turns = item.get("turns", item.get("dialog", item.get("messages", [])))
    for t_idx, turn in enumerate(raw_turns):
        if isinstance(turn, dict):
            role = str(turn.get("role", turn.get("speaker", "user")))
            content = str(turn.get("content", turn.get("text", turn.get("utterance", ""))))
        elif isinstance(turn, str):
            role = "user" if t_idx % 2 == 0 else "assistant"
            content = turn
        else:
            continue

        session.turns.append(SessionTurn(
            turn_id=t_idx,
            role=role,
            content=content,
            session_id=sid,
        ))

    if session.turns:
        sessions.append(session)


def _parse_question_entry(
    item: dict,
    questions: list[LongMemQuestion],
    sessions: list[Session],
    file_stem: str,
    idx: int,
) -> None:
    """Parse a question entry, which may also contain chat history."""
    qid = str(item.get("question_id", item.get("id", f"{file_stem}_q{idx}")))
    question_text = str(item.get("question", item.get("query", "")))
    answer_text = str(item.get("answer", item.get("response", item.get("ground_truth", ""))))
    category = str(item.get("category", item.get("type", item.get("ability", "unknown"))))

    # Session IDs associated with this question
    session_ids_raw = item.get("session_ids", item.get("sessions", []))
    session_ids = [str(s) for s in session_ids_raw] if isinstance(session_ids_raw, list) else []

    if question_text:
        questions.append(LongMemQuestion(
            question_id=qid,
            question=question_text,
            answer=answer_text,
            category=category,
            session_ids=session_ids,
        ))

    # If this entry has embedded chat history, extract as sessions
    chat_history = item.get(
        "chat_history",
        item.get("history", item.get("sessions_detail", item.get("context"))),
    )
    if chat_history is None:
        return

    if isinstance(chat_history, list):
        for s_idx, session_data in enumerate(chat_history):
            if isinstance(session_data, dict):
                _parse_session_entry(
                    session_data, sessions, f"{file_stem}_q{idx}", s_idx,
                )
            elif isinstance(session_data, list):
                # List of turns directly
                sid = f"{file_stem}_q{idx}_s{s_idx}"
                session = Session(session_id=sid)
                for t_idx, turn in enumerate(session_data):
                    if isinstance(turn, dict):
                        role = str(turn.get("role", "user"))
                        content = str(turn.get("content", turn.get("text", "")))
                    elif isinstance(turn, str):
                        role = "user" if t_idx % 2 == 0 else "assistant"
                        content = turn
                    else:
                        continue
                    session.turns.append(SessionTurn(
                        turn_id=t_idx, role=role, content=content, session_id=sid,
                    ))
                if session.turns:
                    sessions.append(session)
