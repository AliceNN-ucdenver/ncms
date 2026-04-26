"""LongMemEval dataset loader — download from HuggingFace and parse.

Data source: https://huggingface.co/datasets/xiaowu0162/LongMemEval
Paper: "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory"

The dataset format embeds sessions inside each question entry:
    {
        "question_id": "gpt4_...",
        "question_type": "temporal-reasoning",
        "question": "...",
        "answer": "...",
        "question_date": "2023/04/10 (Mon) 23:07",
        "haystack_dates": ["2023/03/15", ...],
        "haystack_session_ids": ["session_1", ...],
        "haystack_sessions": [[{role, content}, ...], ...],
        "answer_session_ids": ["session_2"]
    }

Each question has its OWN haystack — the sessions to ingest for that question.
Different questions may have different (overlapping) session sets.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

HF_DATASET_ID = "xiaowu0162/longmemeval-cleaned"
# Direct raw download URLs for the cleaned dataset on HuggingFace
HF_RAW_BASE = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
DEFAULT_CACHE_DIR = Path("benchmarks/results/.cache")

# Known dataset files (per README: oracle, small-cleaned, medium-cleaned)
DATASET_FILES = [
    "longmemeval_oracle.json",
    "longmemeval_s_cleaned.json",
    "longmemeval_m_cleaned.json",
]

# Also check the kumiho-benchmarks submodule as a local fallback
KUMIHO_LONGMEMEVAL_DIR = Path("/tmp/kumiho-benchmarks/LongMemEval/data")


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
    category: str  # question_type: temporal-reasoning, knowledge-update, etc.
    question_date: str = ""
    haystack_dates: list[str] = field(default_factory=list)
    haystack_session_ids: list[str] = field(default_factory=list)
    answer_session_ids: list[str] = field(default_factory=list)


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
            "LongMemEval data already cached at %s (%d files)",
            data_dir,
            len(existing_json),
        )
        return data_dir

    # Try kumiho-benchmarks local clone first (already has the data as submodule)
    kumiho_data = KUMIHO_LONGMEMEVAL_DIR
    if kumiho_data.is_dir():
        json_files = list(kumiho_data.glob("*.json"))
        if json_files:
            logger.info(
                "Found LongMemEval in kumiho-benchmarks at %s (%d files)",
                kumiho_data,
                len(json_files),
            )
            for f in json_files:
                dest = data_dir / f.name
                if not dest.exists():
                    import shutil

                    shutil.copy2(f, dest)
            return data_dir

    # Try huggingface_hub
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
    dataset_file: str = "longmemeval_oracle.json",
) -> tuple[dict[str, list[Session]], list[LongMemQuestion]]:
    """Download and parse the LongMemEval dataset.

    Each question entry contains its own haystack_sessions, so we return
    sessions keyed by question_id rather than a flat list.

    Args:
        cache_dir: Directory for cached data.
        dataset_file: Which JSON file to load (default: oracle with 500 entries).

    Returns:
        Tuple of (sessions_by_question, questions) where sessions_by_question
        maps question_id to the list of Session objects for that question.

    Raises:
        FileNotFoundError: If no data files are found after download.
    """
    data_dir = download_longmemeval(cache_dir)

    # Prefer the specified dataset file
    target = data_dir / dataset_file
    if not target.exists():
        # Try any JSON file
        json_files = sorted(data_dir.rglob("*.json"))
        if not json_files:
            msg = f"No JSON files found in {data_dir}"
            raise FileNotFoundError(msg)
        target = json_files[0]
        logger.warning("Requested %s not found, using %s", dataset_file, target.name)

    logger.info("Parsing %s...", target.name)
    with open(target) as f:
        raw_data = json.load(f)

    if not isinstance(raw_data, list):
        msg = f"Expected a JSON array in {target.name}, got {type(raw_data).__name__}"
        raise ValueError(msg)

    sessions_by_question: dict[str, list[Session]] = {}
    questions: list[LongMemQuestion] = []

    for entry in raw_data:
        if not isinstance(entry, dict):
            continue

        qid = str(entry.get("question_id", ""))
        if not qid:
            continue

        # Parse question fields
        question = LongMemQuestion(
            question_id=qid,
            question=str(entry.get("question", "")),
            answer=str(entry.get("answer", "")),
            category=str(entry.get("question_type", entry.get("category", "unknown"))),
            question_date=str(entry.get("question_date", "")),
            haystack_dates=entry.get("haystack_dates", []),
            haystack_session_ids=entry.get("haystack_session_ids", []),
            answer_session_ids=entry.get("answer_session_ids", []),
        )
        if question.question:
            questions.append(question)

        # Parse haystack_sessions: list of sessions, each session is a list of turns
        raw_sessions = entry.get("haystack_sessions", [])
        session_ids = entry.get("haystack_session_ids", [])
        question_sessions: list[Session] = []

        for s_idx, session_turns in enumerate(raw_sessions):
            if not isinstance(session_turns, list):
                continue

            # Use the matching session_id if available, else generate one
            sid = str(session_ids[s_idx]) if s_idx < len(session_ids) else f"{qid}_s{s_idx}"

            session = Session(session_id=sid)
            for t_idx, turn in enumerate(session_turns):
                if isinstance(turn, dict):
                    role = str(turn.get("role", "user"))
                    content = str(turn.get("content", turn.get("text", "")))
                elif isinstance(turn, str):
                    role = "user" if t_idx % 2 == 0 else "assistant"
                    content = turn
                else:
                    continue

                if content.strip():
                    session.turns.append(
                        SessionTurn(
                            turn_id=t_idx,
                            role=role,
                            content=content,
                            session_id=sid,
                        )
                    )

            if session.turns:
                question_sessions.append(session)

        sessions_by_question[qid] = question_sessions

    total_sessions = sum(len(s) for s in sessions_by_question.values())
    total_turns = sum(sum(len(s.turns) for s in sess) for sess in sessions_by_question.values())
    logger.info(
        "Loaded %d questions, %d total session instances (%d total turns)",
        len(questions),
        total_sessions,
        total_turns,
    )

    return sessions_by_question, questions
