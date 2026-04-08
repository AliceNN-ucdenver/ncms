"""Reconciliation penalty tuning — measures retrieval ranking impact.

Creates labeled state transition pairs (entity states that supersede or conflict
with each other), then measures whether superseded/conflicted states are properly
demoted in retrieval results.

Grid over:
  - supersession_penalty: [0.2, 0.3, 0.4, 0.5]
  - conflict_penalty: [0.1, 0.15, 0.2, 0.3]

Metric: % of superseded states ranked below current state. Target ≥ 85%.

Results written to benchmarks/tuning/reconciliation_tuning.json
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

TUNING_DIR = Path(__file__).parent


# ── Labeled State Transitions ────────────────────────────────────────────

# Each pair: (old_state, new_state, search_query, expected_relation)
# The new_state should supersede the old_state, and the search should
# rank new_state higher.
STATE_TRANSITIONS: list[dict] = [
    {
        "entity": "PostgreSQL",
        "old": "Database running PostgreSQL 14 on the primary cluster",
        "new": "Database migrated to PostgreSQL 16 on the primary cluster",
        "query": "what PostgreSQL version is the database running",
        "relation": "supersedes",
    },
    {
        "entity": "API Gateway",
        "old": "API Gateway uses nginx 1.24 as reverse proxy",
        "new": "API Gateway upgraded to nginx 1.26 as reverse proxy",
        "query": "what nginx version does the API gateway use",
        "relation": "supersedes",
    },
    {
        "entity": "Auth Service",
        "old": "Auth service uses OAuth 2.0 for all API endpoints",
        "new": "Auth service switched from OAuth 2.0 to API key authentication",
        "query": "what authentication method does the auth service use",
        "relation": "supersedes",
    },
    {
        "entity": "Cache Layer",
        "old": "Session caching uses Memcached with 30-minute TTL",
        "new": "Session caching migrated to Redis with 1-hour TTL",
        "query": "what caching system is used for sessions",
        "relation": "supersedes",
    },
    {
        "entity": "Kubernetes",
        "old": "Kubernetes cluster running version 1.27",
        "new": "Kubernetes cluster upgraded to version 1.28",
        "query": "what Kubernetes version is the cluster running",
        "relation": "supersedes",
    },
    {
        "entity": "Python Runtime",
        "old": "Application uses Python 3.11 runtime",
        "new": "Application upgraded to Python 3.12 runtime",
        "query": "what Python version does the application use",
        "relation": "supersedes",
    },
    {
        "entity": "CI Pipeline",
        "old": "CI pipeline runs on Jenkins with 45 minute build time",
        "new": "CI pipeline migrated to GitHub Actions with 12 minute build time",
        "query": "what CI system does the project use",
        "relation": "supersedes",
    },
    {
        "entity": "Monitoring",
        "old": "Application monitoring uses Nagios for alerting",
        "new": "Application monitoring switched to Prometheus and Grafana",
        "query": "what monitoring system is used for alerting",
        "relation": "supersedes",
    },
    {
        "entity": "Message Queue",
        "old": "Message queue uses RabbitMQ for async job processing",
        "new": "Message queue migrated from RabbitMQ to Apache Kafka",
        "query": "what message queue system is used",
        "relation": "supersedes",
    },
    {
        "entity": "Load Balancer",
        "old": "Load balancer uses HAProxy with round-robin algorithm",
        "new": "Load balancer replaced HAProxy with AWS ALB with least-connections",
        "query": "what load balancer is used for traffic distribution",
        "relation": "supersedes",
    },
    # Conflict pairs (both states may be valid, but one contradicts the other)
    {
        "entity": "Deploy Policy",
        "old": "Deploy policy: blue-green deployments with manual approval",
        "new": "Deploy policy changed to canary deployments with automatic rollback",
        "query": "what deployment strategy does the team use",
        "relation": "supersedes",
    },
    {
        "entity": "API Protocol",
        "old": "API uses REST with JSON payloads for all endpoints",
        "new": "API migrated critical endpoints to gRPC, REST kept for public API",
        "query": "what API protocol does the service use",
        "relation": "supersedes",
    },
    {
        "entity": "Storage Backend",
        "old": "File storage uses local filesystem with NFS mounts",
        "new": "File storage migrated to Amazon S3 for all environments",
        "query": "where are files stored",
        "relation": "supersedes",
    },
    {
        "entity": "Test Framework",
        "old": "Unit tests written with unittest framework",
        "new": "Testing framework migrated from unittest to pytest",
        "query": "what testing framework is used for unit tests",
        "relation": "supersedes",
    },
    {
        "entity": "Log Aggregation",
        "old": "Logs aggregated using ELK stack on self-hosted servers",
        "new": "Log aggregation migrated to Datadog for all services",
        "query": "what log aggregation system is used",
        "relation": "supersedes",
    },
    {
        "entity": "DNS Provider",
        "old": "DNS managed by self-hosted BIND9 servers",
        "new": "DNS migrated to Cloudflare with proxy enabled",
        "query": "what DNS provider is used",
        "relation": "supersedes",
    },
    {
        "entity": "Secret Management",
        "old": "Secrets stored in encrypted environment variables",
        "new": "Secret management migrated to HashiCorp Vault",
        "query": "how are secrets managed",
        "relation": "supersedes",
    },
    {
        "entity": "Container Registry",
        "old": "Docker images stored in self-hosted Harbor registry",
        "new": "Container registry migrated to GitHub Container Registry",
        "query": "where are container images stored",
        "relation": "supersedes",
    },
    {
        "entity": "Feature Flags",
        "old": "Feature flags managed with custom JSON config files",
        "new": "Feature flag system replaced with LaunchDarkly",
        "query": "how are feature flags managed",
        "relation": "supersedes",
    },
    {
        "entity": "Backup System",
        "old": "Database backups use pg_dump with daily cron job to local disk",
        "new": "Database backups migrated to WAL-G with continuous archiving to S3",
        "query": "how are database backups handled",
        "relation": "supersedes",
    },
]


async def evaluate_reconciliation() -> dict:
    """Run reconciliation penalty grid search."""
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

    supersession_penalties = [0.2, 0.3, 0.4, 0.5]
    conflict_penalties = [0.1, 0.15, 0.2, 0.3]

    results = {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "transitions": len(STATE_TRANSITIONS),
        "grid": {
            "supersession_penalty": supersession_penalties,
            "conflict_penalty": conflict_penalties,
        },
        "configs": [],
        "best": None,
    }

    total_configs = len(supersession_penalties) * len(conflict_penalties)
    print(f"Reconciliation tuning: {total_configs} configs, "
          f"{len(STATE_TRANSITIONS)} transitions", flush=True)

    best_rate = -1.0
    best_cfg = None
    config_idx = 0

    for sup_pen in supersession_penalties:
        for con_pen in conflict_penalties:
            # Fresh backends
            store = SQLiteStore(db_path=":memory:")
            await store.initialize()
            index = TantivyEngine()
            index.initialize()
            graph = NetworkXGraph()

            config = NCMSConfig(
                db_path=":memory:",
                admission_enabled=True,
                reconciliation_enabled=True,
                reconciliation_supersession_penalty=sup_pen,
                reconciliation_conflict_penalty=con_pen,
                episodes_enabled=False,
                contradiction_detection_enabled=False,
            )

            admission_svc = AdmissionService(
                store=store, index=index, graph=graph, config=config,
            )
            reconciliation_svc = ReconciliationService(store=store, config=config)
            episode_svc = EpisodeService(store=store, index=index, config=config)
            svc = MemoryService(
                store=store, index=index, graph=graph, config=config,
                admission=admission_svc, reconciliation=reconciliation_svc,
                episode=episode_svc,
            )

            correct = 0
            total = 0

            for transition in STATE_TRANSITIONS:
                # Store old state first
                old_mem = await svc.store_memory(
                    content=transition["old"],
                    memory_type="fact",
                    domains=["ops"],
                )

                # Store new state (should supersede)
                new_mem = await svc.store_memory(
                    content=transition["new"],
                    memory_type="fact",
                    domains=["ops"],
                )

                # Search — new state should rank above old state
                search_results = await svc.search(
                    query=transition["query"],
                    domain="ops",
                    limit=20,
                )

                # Check ranking
                old_rank = None
                new_rank = None
                for rank, scored in enumerate(search_results):
                    if scored.memory.id == old_mem.id:
                        old_rank = rank
                    if scored.memory.id == new_mem.id:
                        new_rank = rank

                total += 1
                if new_rank is not None and (old_rank is None or new_rank < old_rank):
                    correct += 1

            demotion_rate = correct / total if total > 0 else 0.0

            entry = {
                "index": config_idx,
                "supersession_penalty": sup_pen,
                "conflict_penalty": con_pen,
                "demotion_rate": round(demotion_rate, 4),
                "correct": correct,
                "total": total,
            }
            results["configs"].append(entry)

            if demotion_rate > best_rate:
                best_rate = demotion_rate
                best_cfg = entry

            config_idx += 1
            await store.close()

            print(
                f"  [{config_idx}/{total_configs}] "
                f"sup={sup_pen} con={con_pen} "
                f"demotion={demotion_rate:.3f}",
                flush=True,
            )

    results["best"] = best_cfg

    # Write results
    path = TUNING_DIR / "reconciliation_tuning.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nResults written to {path}", flush=True)
    print(f"Best: sup_penalty={best_cfg['supersession_penalty']} "
          f"con_penalty={best_cfg['conflict_penalty']} "
          f"demotion_rate={best_cfg['demotion_rate']:.3f}", flush=True)

    return results


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()
    asyncio.run(evaluate_reconciliation())


if __name__ == "__main__":
    main()
