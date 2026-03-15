"""Episode formation tuning — validates episode grouping precision.

Creates synthetic episode scenarios (groups of related fragments + unrelated
distractors) and measures how accurately the episode linker groups them.

Grid over:
  - match_threshold: [0.20, 0.25, 0.30, 0.35, 0.40]
  - min_entities: [1, 2, 3]
  - Signal weights (entity_overlap dominance vs balanced)

Metric: Precision = correct groupings / total groupings. Target ≥ 80%.

Results written to benchmarks/tuning/episode_tuning_results.json
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TUNING_DIR = Path(__file__).parent


# ── Synthetic Episode Scenarios ──────────────────────────────────────────

# Each scenario: a list of fragments that SHOULD group together,
# plus distractors that SHOULD NOT join.
SCENARIOS: list[dict] = [
    {
        "name": "auth_migration",
        "description": "OAuth to API key migration incident",
        "fragments": [
            "Auth service migration from OAuth 2.0 to API keys started today for JIRA-456",
            "Redis session cache cleared as part of auth migration JIRA-456",
            "Auth migration JIRA-456 hit rate limiting issue, rolling back temporarily",
            "JIRA-456 auth migration rollback complete, investigating rate limit root cause",
        ],
        "distractors": [
            "Database backup completed successfully on schedule",
            "Frontend team updated the landing page CSS",
        ],
    },
    {
        "name": "database_outage",
        "description": "PostgreSQL outage and recovery",
        "fragments": [
            "PostgreSQL primary database went down at 14:30 UTC due to disk full",
            "Database failover to read replica initiated, partial service restored",
            "Root cause identified: uncompressed audit logs filled /data partition",
            "Database primary restored after disk cleanup, all services nominal",
        ],
        "distractors": [
            "New team member Alice started in the API team today",
            "Sprint planning meeting scheduled for Thursday",
        ],
    },
    {
        "name": "deploy_v2",
        "description": "Version 2.0 deployment pipeline",
        "fragments": [
            "Release v2.0.0 build started, deploying to staging environment",
            "Staging smoke tests passed for v2.0.0, promoting to production",
            "Production deployment of v2.0.0 complete, monitoring error rates",
        ],
        "distractors": [
            "Code review policy updated: require 2 approvals for main branch",
            "AWS cost report shows 15% increase this month",
            "Team lunch moved to Friday",
        ],
    },
    {
        "name": "performance_investigation",
        "description": "API latency investigation",
        "fragments": [
            "API response times degraded, p99 latency increased from 200ms to 800ms",
            "Investigation shows N+1 query pattern in user profile endpoint",
            "Fix deployed: batch loading for user profile queries, p99 back to 180ms",
        ],
        "distractors": [
            "Documentation updated for the new onboarding flow",
            "Security scan completed with no critical findings",
        ],
    },
    {
        "name": "feature_flag_rollout",
        "description": "Gradual feature flag rollout",
        "fragments": [
            "Feature flag 'new-checkout-flow' enabled for 10% of users PR-789",
            "Checkout conversion rate up 5% with new flow, expanding to 50% PR-789",
            "PR-789 new checkout flow at 100% rollout, old flow deprecated",
        ],
        "distractors": [
            "Kubernetes cluster upgraded to version 1.28",
        ],
    },
]


def _count_grid_configs() -> int:
    """Count total grid configurations."""
    thresholds = [0.20, 0.25, 0.30, 0.35, 0.40]
    min_entities_list = [1, 2, 3]
    weight_variants = [
        {"label": "balanced", "entity": 0.25, "bm25": 0.20, "domain": 0.15},
        {"label": "entity_heavy", "entity": 0.35, "bm25": 0.15, "domain": 0.10},
        {"label": "bm25_heavy", "entity": 0.15, "bm25": 0.30, "domain": 0.15},
    ]
    return len(thresholds) * len(min_entities_list) * len(weight_variants)


async def evaluate_episodes() -> dict:
    """Run episode formation tuning grid search."""
    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.memory_service import MemoryService
    from ncms.application.reconciliation_service import ReconciliationService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        sha = "unknown"

    thresholds = [0.20, 0.25, 0.30, 0.35, 0.40]
    min_entities_list = [1, 2, 3]
    weight_variants = [
        {"label": "balanced", "entity": 0.25, "bm25": 0.20, "domain": 0.15},
        {"label": "entity_heavy", "entity": 0.35, "bm25": 0.15, "domain": 0.10},
        {"label": "bm25_heavy", "entity": 0.15, "bm25": 0.30, "domain": 0.15},
    ]

    results = {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "scenarios": len(SCENARIOS),
        "total_fragments": sum(len(s["fragments"]) for s in SCENARIOS),
        "total_distractors": sum(len(s["distractors"]) for s in SCENARIOS),
        "configs": [],
        "best": None,
    }

    total_configs = len(thresholds) * len(min_entities_list) * len(weight_variants)
    print(f"Episode tuning: {total_configs} configs, {len(SCENARIOS)} scenarios", flush=True)

    best_precision = -1.0
    best_cfg = None
    config_idx = 0

    for threshold in thresholds:
        for min_ents in min_entities_list:
            for wv in weight_variants:
                # Fresh backends for each config
                store = SQLiteStore(db_path=":memory:")
                await store.initialize()
                index = TantivyEngine()
                index.initialize()
                graph = NetworkXGraph()

                config = NCMSConfig(
                    db_path=":memory:",
                    admission_enabled=True,
                    episodes_enabled=True,
                    episode_match_threshold=threshold,
                    episode_create_min_entities=min_ents,
                    episode_weight_bm25=wv["bm25"],
                    episode_weight_entity_overlap=wv["entity"],
                    episode_weight_domain=wv["domain"],
                    # Other weights stay default
                    contradiction_detection_enabled=False,
                    reconciliation_enabled=False,
                )

                admission_svc = AdmissionService(
                    store=store, index=index, graph=graph, config=config,
                )
                episode_svc = EpisodeService(
                    store=store, index=index, config=config,
                )
                svc = MemoryService(
                    store=store, index=index, graph=graph, config=config,
                    admission=admission_svc, episode=episode_svc,
                )

                # Run all scenarios
                correct = 0
                total = 0

                for scenario in SCENARIOS:
                    # Store fragments (should form episode)
                    fragment_mems = []
                    for frag in scenario["fragments"]:
                        mem = await svc.store_memory(
                            content=frag, memory_type="fact", domains=["ops"],
                        )
                        fragment_mems.append(mem)

                    # Store distractors (should NOT join the episode)
                    distractor_mems = []
                    for dist in scenario["distractors"]:
                        mem = await svc.store_memory(
                            content=dist, memory_type="fact", domains=["general"],
                        )
                        distractor_mems.append(mem)

                    # Check: how many fragments share the same episode?
                    frag_episodes = set()
                    for mem in fragment_mems:
                        admission_info = (mem.structured or {}).get("admission", {})
                        ep = (mem.structured or {}).get("episode")
                        if ep:
                            frag_episodes.add(ep.get("episode_id", ""))

                    # Precision: fragments grouped together
                    if len(frag_episodes) == 1 and "" not in frag_episodes:
                        correct += 1  # All fragments in same episode
                    total += 1

                    # Check distractors didn't join
                    for mem in distractor_mems:
                        ep = (mem.structured or {}).get("episode")
                        if ep:
                            ep_id = ep.get("episode_id", "")
                            if ep_id in frag_episodes:
                                pass  # Distractor wrongly joined
                            else:
                                correct += 1  # Distractor in different episode (ok)
                        else:
                            correct += 1  # Distractor not in any episode (ok)
                        total += 1

                precision = correct / total if total > 0 else 0.0

                entry = {
                    "index": config_idx,
                    "threshold": threshold,
                    "min_entities": min_ents,
                    "weights": wv["label"],
                    "precision": round(precision, 4),
                    "correct": correct,
                    "total": total,
                }
                results["configs"].append(entry)

                if precision > best_precision:
                    best_precision = precision
                    best_cfg = entry

                config_idx += 1
                await store.close()

                if config_idx % 15 == 0 or config_idx == total_configs:
                    print(
                        f"  [{config_idx}/{total_configs}] "
                        f"best precision={best_precision:.3f}",
                        flush=True,
                    )

    results["best"] = best_cfg

    # Write results
    path = TUNING_DIR / "episode_tuning_results.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults written to {path}", flush=True)
    print(f"Best: threshold={best_cfg['threshold']} "
          f"min_entities={best_cfg['min_entities']} "
          f"weights={best_cfg['weights']} "
          f"precision={best_cfg['precision']:.3f}", flush=True)

    return results


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()
    asyncio.run(evaluate_episodes())


if __name__ == "__main__":
    main()
