"""MemoryAgentBench dataset loader.

Downloads and loads the MemoryAgentBench dataset from HuggingFace
(ai-hyz/MemoryAgentBench). Handles graceful fallback when the dataset
or required libraries are not available.

Each competency split contains memory chunks and evaluation queries:
- AR (Accurate Retrieval): standard retrieval with relevance judgments
- TTL (Test-Time Learning): classification from retrieved context
- LRU (Long-Range Understanding): cross-topic connection queries
- SF (Selective Forgetting): outdated/superseded memory filtering
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATASET_REPO = "ai-hyz/MemoryAgentBench"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET_REPO}"

# Expected split names in the dataset
COMPETENCY_SPLITS = ("ar", "ttl", "lru", "sf")

# HuggingFace split names → our short names
_HF_SPLIT_MAP: dict[str, str] = {
    "accurate_retrieval": "ar",
    "test_time_learning": "ttl",
    "long_range_understanding": "lru",
    "conflict_resolution": "sf",  # CR maps to selective forgetting evaluation
}


def _default_cache_dir() -> Path:
    """Return the default cache directory for MAB data."""
    return Path.home() / ".ncms" / "benchmarks" / "memoryagentbench"


def download_mab(cache_dir: Path | None = None) -> Path | None:
    """Download the MemoryAgentBench dataset from HuggingFace.

    Tries HuggingFace datasets library first, then huggingface_hub
    for direct file download, then raw URL fetch.

    Args:
        cache_dir: Directory to cache downloaded data. Defaults to
            ~/.ncms/benchmarks/memoryagentbench/

    Returns:
        Path to cached dataset directory, or None if unavailable.
    """
    cache = cache_dir or _default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    # Strategy 1: HuggingFace datasets library
    try:
        from datasets import load_dataset

        logger.info("Downloading MemoryAgentBench via HuggingFace datasets...")
        ds = load_dataset(DATASET_REPO, trust_remote_code=True)

        # Cache as JSON for subsequent loads without datasets library
        for split_name in ds:
            split_path = cache / f"{split_name}.json"
            if not split_path.exists():
                rows = [dict(row) for row in ds[split_name]]
                split_path.write_text(json.dumps(rows, default=str))
                logger.info("  Cached split '%s': %d rows", split_name, len(rows))

        logger.info("MemoryAgentBench downloaded to %s", cache)
        return cache

    except ImportError:
        logger.debug("datasets library not installed, trying huggingface_hub")
    except Exception as exc:
        logger.warning(
            "Failed to download via datasets library: %s. "
            "The dataset may not be publicly available yet.",
            exc,
        )

    # Strategy 2: huggingface_hub snapshot download
    try:
        from huggingface_hub import snapshot_download

        logger.info("Downloading MemoryAgentBench via huggingface_hub...")
        path = snapshot_download(
            repo_id=DATASET_REPO,
            repo_type="dataset",
            local_dir=str(cache / "raw"),
        )
        logger.info("MemoryAgentBench downloaded to %s", path)
        return Path(path)

    except ImportError:
        logger.debug("huggingface_hub not installed, trying direct URL")
    except Exception as exc:
        logger.warning(
            "Failed to download via huggingface_hub: %s. "
            "The dataset may not be publicly available yet.",
            exc,
        )

    # Strategy 3: Direct URL fetch (for JSON-based datasets)
    try:
        import urllib.request

        for split in COMPETENCY_SPLITS:
            url = f"{DATASET_URL}/resolve/main/data/{split}.json"
            dest = cache / f"{split}.json"
            if dest.exists():
                continue
            try:
                logger.info("  Fetching %s ...", url)
                urllib.request.urlretrieve(url, dest)
                logger.info("  Downloaded %s", split)
            except Exception:
                logger.debug("  Could not fetch %s (may not exist at this URL)", split)
                continue

        # Check if we got at least one split
        json_files = list(cache.glob("*.json"))
        if json_files:
            logger.info("Downloaded %d splits via direct URL", len(json_files))
            return cache

    except Exception as exc:
        logger.warning("Direct URL download failed: %s", exc)

    logger.warning(
        "MemoryAgentBench dataset not available. "
        "This may be an unreleased ICLR 2026 dataset. "
        "Install with: pip install datasets && "
        "python -c \"from datasets import load_dataset; "
        "load_dataset('%s')\"",
        DATASET_REPO,
    )
    return None


def load_mab_dataset(cache_dir: Path | None = None) -> dict[str, Any] | None:
    """Load all 4 MemoryAgentBench competency splits.

    Attempts to load from local cache first, then downloads if needed.

    Args:
        cache_dir: Directory containing cached MAB data.

    Returns:
        Dict with keys 'ar', 'ttl', 'lru', 'sf', each containing
        the split's data (list of dicts). Returns None if dataset
        is unavailable.
    """
    cache = cache_dir or _default_cache_dir()

    # Try loading from cached JSON first
    result: dict[str, Any] = {}
    cached_any = False

    for split in COMPETENCY_SPLITS:
        # Try short name first (ar.json), then HF names (Accurate_Retrieval.json)
        candidates = [cache / f"{split}.json"]
        for hf_name, short in _HF_SPLIT_MAP.items():
            if short == split:
                # Try both capitalized and lowercase HF names
                capitalized = hf_name.replace("_", " ").title().replace(" ", "_")
                candidates.append(cache / f"{capitalized}.json")
                candidates.append(cache / f"{hf_name}.json")

        for split_path in candidates:
            if split_path.exists():
                try:
                    data = json.loads(split_path.read_text())
                    result[split] = data
                    cached_any = True
                    logger.info(
                        "Loaded cached split '%s' from %s: %d items",
                        split, split_path.name, len(data),
                    )
                    break
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read cached %s: %s", split_path, exc)

    if cached_any and len(result) > 0:
        logger.info(
            "Loaded %d/%d MAB splits from cache",
            len(result), len(COMPETENCY_SPLITS),
        )
        return result

    # Try HuggingFace datasets library directly (no cache)
    try:
        from datasets import load_dataset

        logger.info("Loading MemoryAgentBench from HuggingFace...")
        ds = load_dataset(DATASET_REPO, trust_remote_code=True)

        for split_name in ds:
            normalized = split_name.lower()
            if normalized in COMPETENCY_SPLITS:
                result[normalized] = [dict(row) for row in ds[split_name]]
                logger.info("Loaded split '%s': %d items", normalized, len(result[normalized]))

            # Cache for next time
            cache.mkdir(parents=True, exist_ok=True)
            split_path = cache / f"{normalized}.json"
            if not split_path.exists():
                split_path.write_text(json.dumps(result[normalized], default=str))

        if result:
            return result

    except ImportError:
        logger.debug("datasets library not installed")
    except Exception as exc:
        logger.warning("Failed to load from HuggingFace: %s", exc)

    # Try downloading
    downloaded = download_mab(cache_dir)
    if downloaded is None:
        return None

    # Retry loading from cache after download
    for split in COMPETENCY_SPLITS:
        if split in result:
            continue
        split_path = downloaded / f"{split}.json"
        if split_path.exists():
            try:
                data = json.loads(split_path.read_text())
                result[split] = data
            except (json.JSONDecodeError, OSError):
                pass

    return result if result else None
