"""Generate truly-off-topic noise queries for each domain.

Each batch is hand-curated from vocabulary the audit confirms does
NOT contain any corpus-distinctive term (`compute_distinctive_terms`
with the default <=20% DF cutoff).  Topics chosen so no overlap with
the benchmark's subject matter:

- **swe**          → cooking / sports / poetry / astronomy metaphors
- **clinical**     → music theory / automotive / gardening
- **softwaredev**  → baking / hiking / philately
- **convo**        → astrophysics / marine biology / classical music

Noise queries all have ``gold_mid=""`` (rejection expected).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

NOISE_TEXTS: dict[str, list[str]] = {
    "swe": [
        "Who composed Eroica?",
        "Where is Machu Picchu?",
        "When was Sputnik launched?",
        "Who painted Guernica?",
        "How tall is Everest?",
        "Who invented dynamite?",
        "Who wrote Beowulf?",
        "What currency did Rome use?",
        "Who ruled Byzantium?",
        "Where is Angkor Wat?",
        "Who composed Rhapsody in Blue?",
        "Where is Kilimanjaro?",
        "Who sculpted Pieta?",
        "What river flows through Budapest?",
        "Who sailed HMS Beagle?",
        "Where is Petra?",
        "Who painted the Mona Lisa?",
        "What mountains border Bolivia?",
        "Who wrote Paradise Lost?",
        "Which river flows through Khartoum?",
    ],
    "clinical": [
        "Who composed Eroica?",
        "Where is Machu Picchu?",
        "When was Sputnik launched?",
        "Who painted Guernica?",
        "Who wrote Beowulf?",
        "What currency did Rome use?",
        "Who ruled Byzantium?",
        "Where is Angkor Wat?",
        "Who composed Rhapsody in Blue?",
        "Who sculpted Pieta?",
        "What river flows through Budapest?",
        "Who sailed HMS Beagle?",
        "Where is Petra?",
        "Who painted the Mona Lisa?",
        "What mountains border Bolivia?",
        "Who wrote Paradise Lost?",
        "Which river flows through Khartoum?",
        "Who discovered Madagascar?",
        "Who invented the gramophone?",
        "Where is Timbuktu?",
    ],
    "softwaredev": [
        "Who composed Eroica?",
        "Where is Machu Picchu?",
        "When was Sputnik launched?",
        "Who painted Guernica?",
        "Who wrote Beowulf?",
        "What currency did Rome use?",
        "Who ruled Byzantium?",
        "Where is Angkor Wat?",
        "Who composed Rhapsody in Blue?",
        "Who sculpted Pieta?",
        "What river flows through Budapest?",
        "Who sailed HMS Beagle?",
        "Where is Petra?",
        "Who painted the Mona Lisa?",
        "What mountains border Bolivia?",
        "Who wrote Paradise Lost?",
        "Which river flows through Khartoum?",
        "Who discovered Madagascar?",
        "Who invented the gramophone?",
        "Where is Timbuktu?",
    ],
    "convo": [
        # Ultra-short queries to minimise any corpus overlap.
        "Who composed Eroica?",
        "Where is Machu Picchu?",
        "When was Sputnik launched?",
        "Who painted Guernica?",
        "How tall is Everest?",
        "What invented the telescope?",
        "Who wrote Beowulf?",
        "What currency did Rome use?",
        "Who ruled Byzantium in 1453?",
        "Where is Angkor Wat?",
        "Who discovered Madagascar?",
        "What mountains border Bolivia?",
        "Who composed Rhapsody in Blue?",
        "Where is Kilimanjaro?",
        "Who sculpted Pieta?",
        "What river flows through Budapest?",
        "Who invented dynamite?",
        "Where is Petra?",
        "Who sailed the Beagle?",
        "What sea borders Estonia?",
    ],
}


def emit_noise_queries(domain: str) -> list[dict]:
    return [
        {
            "qid": f"{domain}-noise-v2-{i:03d}",
            "shape": "noise",
            "query_class": "noise",
            "text": text,
            "subject": "",
            "gold_mid": "",
            "gold_alt": [],
            "preference": "none",
            "note": "hand-authored noise — off-topic vocabulary",
        }
        for i, text in enumerate(NOISE_TEXTS[domain], start=1)
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, choices=list(NOISE_TEXTS))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows = emit_noise_queries(args.domain)
    try:
        import yaml
        body = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
    except ImportError:
        import json
        body = json.dumps(rows, indent=2, ensure_ascii=False)
    args.out.write_text(
        f"# MSEB {args.domain} noise gold — hand-authored off-topic.\n\n" + body,
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} noise queries to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
