"""Hub replay fixture data -- 67 memories from the live NCMS hub.

Exported from /tmp/ncms-hub.db on 2026-04-08.
Memories loaded from hub_memories.json (same directory).
"""

from __future__ import annotations

import json
from pathlib import Path

HUB_QUERIES: dict[str, str] = {
    "fact_lookup": "What database does the IMDB Lite app use?",
    "state_lookup": "What is the current status of ADR-003?",
    "temporal": "What was decided after the security review?",
    "pattern": "What patterns emerged in the design review process?",
    "cross_agent": "What did the security agent flag about authentication?",
}

_FIXTURE_DIR = Path(__file__).parent

with open(_FIXTURE_DIR / "hub_memories.json") as _f:
    HUB_MEMORIES: list[dict] = json.load(_f)
