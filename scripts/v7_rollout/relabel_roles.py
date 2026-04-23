"""Re-label existing gold with v7 role_spans in place.

For each row in ``gold_<domain>.jsonl``:
  1. Keep intent / slots / topic / state_change / admission unchanged
     (they were validated at v6 label time).
  2. Run the gazetteer + role-classification pipeline from
     :mod:`llm_slot_labeler` to add ``role_spans``.
  3. Write to the same path.

Usage::

    uv run python scripts/v7_rollout/relabel_roles.py \
        --domain software_dev --limit 200 --log-every 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ncms.application.adapters.corpus.llm_slot_labeler import _classify_roles
from ncms.application.adapters.corpus.loader import dump_jsonl, load_jsonl
from ncms.application.adapters.schemas import get_domain_manifest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("relabel_roles")


async def relabel(
    *, domain: str, model: str, api_base: str | None,
    limit: int | None, log_every: int,
) -> None:
    manifest = get_domain_manifest(domain)  # type: ignore[arg-type]
    path = manifest.gold_jsonl
    if not path.exists():
        raise SystemExit(f"gold file missing: {path}")
    rows = load_jsonl(path)
    if limit is not None:
        rows = rows[:limit]
    log.info("relabel: domain=%s rows=%d → %s", domain, len(rows), path)

    t0 = time.perf_counter()
    n_with_spans = 0
    n_empty = 0
    primary_ct = 0
    alt_ct = 0
    casual_ct = 0
    notrel_ct = 0

    for i, ex in enumerate(rows):
        try:
            role_spans = await _classify_roles(
                content=ex.text, domain=ex.domain,
                slots=ex.slots, model=model, api_base=api_base,
            )
        except Exception as exc:
            log.warning("row %d relabel failed: %s", i, exc)
            role_spans = []
        ex.role_spans = role_spans
        if role_spans:
            n_with_spans += 1
            for rs in role_spans:
                if rs.role == "primary": primary_ct += 1
                elif rs.role == "alternative": alt_ct += 1
                elif rs.role == "casual": casual_ct += 1
                elif rs.role == "not_relevant": notrel_ct += 1
        else:
            n_empty += 1
        if (i + 1) % log_every == 0:
            elapsed = time.perf_counter() - t0
            rate = (i + 1) / elapsed
            log.info(
                "  %d/%d rows  (%.2f rows/s, ETA %ds)  primary=%d alt=%d casual=%d not_relevant=%d",
                i + 1, len(rows), rate,
                int((len(rows) - i - 1) / max(rate, 1e-9)),
                primary_ct, alt_ct, casual_ct, notrel_ct,
            )

    dump_jsonl(rows, path)
    elapsed = time.perf_counter() - t0
    log.info(
        "done: %d rows (%d with spans, %d without) in %.1fs",
        len(rows), n_with_spans, n_empty, elapsed,
    )
    log.info(
        "role distribution: primary=%d alternative=%d casual=%d not_relevant=%d",
        primary_ct, alt_ct, casual_ct, notrel_ct,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True)
    p.add_argument(
        "--model",
        default=os.environ.get(
            "NCMS_LLM_MODEL",
            "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
        ),
    )
    p.add_argument(
        "--api-base",
        default=os.environ.get(
            "NCMS_LLM_API_BASE", "http://spark-ee7d.local:8000/v1",
        ),
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--log-every", type=int, default=5)
    args = p.parse_args()
    asyncio.run(relabel(
        domain=args.domain, model=args.model, api_base=args.api_base,
        limit=args.limit, log_every=args.log_every,
    ))


if __name__ == "__main__":
    main()
