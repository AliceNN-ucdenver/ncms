"""Pre-tuning smoke test — validates end-to-end pipeline before weight tuning.

Checks:
1. Spark connectivity (Nemotron Nano responds)
2. Full pipeline store (admission + entities + reconciliation + episodes)
3. Contradiction detection via LLM
4. Search with intent override
5. Routing distribution analysis

Results written to benchmarks/tuning/smoke_test_log.md
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TUNING_DIR = Path(__file__).parent


async def run_smoke_test() -> dict:
    """Run end-to-end smoke test and return results dict."""
    import httpx

    from ncms.application.admission_service import AdmissionService
    from ncms.application.episode_service import EpisodeService
    from ncms.application.memory_service import MemoryService
    from ncms.application.reconciliation_service import ReconciliationService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
    from ncms.infrastructure.storage.sqlite_store import SQLiteStore

    results: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tests": {},
    }

    # Get git SHA
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
        results["git_sha"] = sha
    except Exception:
        results["git_sha"] = "unknown"

    # --- Test 1: Spark connectivity ---
    print("=== Test 1: Spark connectivity ===")
    spark_url = "http://spark-ee7d.local:8000/v1/models"
    try:
        transport = httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",  # force IPv4
        )
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            resp = await client.get(spark_url)
            data = resp.json()
            model_id = data["data"][0]["id"]
            results["tests"]["spark_connectivity"] = {
                "status": "PASS",
                "model": model_id,
                "url": spark_url,
            }
            print(f"  PASS: {model_id}")
    except Exception as e:
        results["tests"]["spark_connectivity"] = {
            "status": "FAIL",
            "error": str(e),
        }
        print(f"  FAIL: {e}")
        # Try Ollama fallback
        print("  Checking Ollama fallback...")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://localhost:11434/api/tags")
                models = [m["name"] for m in resp.json().get("models", [])]
                results["tests"]["ollama_fallback"] = {
                    "status": "AVAILABLE",
                    "models": models,
                }
                print(f"  Ollama available: {models}")
        except Exception as e2:
            results["tests"]["ollama_fallback"] = {
                "status": "UNAVAILABLE", "error": str(e2),
            }

    # --- Setup services ---
    # Default to Spark; fallback to Ollama if connectivity test failed
    spark_ok = results["tests"].get("spark_connectivity", {}).get("status") == "PASS"
    if spark_ok:
        llm_model = "openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
        llm_api_base: str | None = "http://spark-ee7d.local:8000/v1"
        print("  Using Spark for LLM")
    else:
        llm_model = "ollama_chat/qwen3.5:35b-a3b"
        llm_api_base = None
        print("  Using Ollama fallback for LLM")

    config = NCMSConfig(
        db_path=":memory:",
        admission_enabled=True,
        reconciliation_enabled=True,
        episodes_enabled=True,
        intent_classification_enabled=True,
        contradiction_detection_enabled=True,
        llm_model=llm_model,
        llm_api_base=llm_api_base,
    )
    store = SQLiteStore(db_path=":memory:")
    await store.initialize()
    index = TantivyEngine()
    index.initialize()
    graph = NetworkXGraph()

    admission_svc = AdmissionService(
        store=store, index=index, graph=graph, config=config,
    )
    reconciliation_svc = ReconciliationService(store=store, config=config)
    episode_svc = EpisodeService(store=store, index=index, config=config)

    svc = MemoryService(
        store=store, index=index, graph=graph, config=config,
        admission=admission_svc,
        reconciliation=reconciliation_svc,
        episode=episode_svc,
    )

    # --- Test 2: Store with full pipeline ---
    print("\n=== Test 2: Store with full pipeline ===")
    test_contents = [
        ("The auth service uses OAuth 2.0 for all API endpoints", "fact", ["api"]),
        ("Database migration to PostgreSQL 16 completed on 2026-01-15", "fact", ["database"]),
        ("Decided to use Redis for session caching instead of Memcached", "architecture-decision", ["api"]),
    ]

    store_results = []
    for content, mtype, domains in test_contents:
        t0 = time.perf_counter()
        mem = await svc.store_memory(
            content=content, memory_type=mtype, domains=domains,
        )
        elapsed = time.perf_counter() - t0
        admission = (mem.structured or {}).get("admission", {})
        store_results.append({
            "content": content[:60],
            "admission_score": admission.get("score"),
            "route": admission.get("route"),
            "elapsed_ms": round(elapsed * 1000, 1),
        })
        print(f"  Score={admission.get('score', '?'):.3f} route={admission.get('route', '?')} "
              f"({elapsed*1000:.0f}ms) {content[:50]}...")

    results["tests"]["store_pipeline"] = {
        "status": "PASS",
        "items": store_results,
    }

    # --- Test 3: Contradiction detection ---
    print("\n=== Test 3: Contradiction detection (LLM) ===")
    t0 = time.perf_counter()
    m_contra = await svc.store_memory(
        content="The auth service uses API keys, not OAuth",
        memory_type="fact",
        domains=["api"],
    )
    elapsed = time.perf_counter() - t0
    admission = (m_contra.structured or {}).get("admission", {})
    contradictions = (m_contra.structured or {}).get("contradictions")
    results["tests"]["contradiction_detection"] = {
        "status": "PASS" if contradictions else "NO_CONTRADICTION_FOUND",
        "admission_score": admission.get("score"),
        "route": admission.get("route"),
        "contradictions": contradictions,
        "elapsed_ms": round(elapsed * 1000, 1),
    }
    print(f"  Score={admission.get('score', '?'):.3f} route={admission.get('route', '?')} "
          f"contradictions={contradictions} ({elapsed*1000:.0f}ms)")

    # --- Test 4: Search with intent override ---
    print("\n=== Test 4: Search with intent override ===")
    t0 = time.perf_counter()
    search_results = await svc.search(
        "auth protocol", intent_override="current_state_lookup",
    )
    elapsed = time.perf_counter() - t0
    results["tests"]["intent_search"] = {
        "status": "PASS" if len(search_results) > 0 else "NO_RESULTS",
        "num_results": len(search_results),
        "elapsed_ms": round(elapsed * 1000, 1),
        "top_results": [
            {
                "score": r.total_activation,
                "intent": r.intent,
                "content": r.memory.content[:60],
            }
            for r in search_results[:3]
        ],
    }
    print(f"  {len(search_results)} results ({elapsed*1000:.0f}ms)")
    for r in search_results[:3]:
        print(f"    score={r.total_activation:.3f} intent={r.intent} {r.memory.content[:50]}")

    # --- Test 5: Routing distribution ---
    print("\n=== Test 5: Routing distribution ===")
    routes = [sr["route"] for sr in store_results]
    routes.append(admission.get("route"))
    route_counts = {}
    for r in routes:
        route_counts[r] = route_counts.get(r, 0) + 1
    results["tests"]["routing_distribution"] = route_counts
    for route, count in sorted(route_counts.items()):
        print(f"  {route}: {count}")

    # --- Test 6: Graph state ---
    print("\n=== Test 6: Graph state ===")
    entity_count = graph.entity_count()
    rel_count = graph.relationship_count()
    results["tests"]["graph_state"] = {
        "entities": entity_count,
        "relationships": rel_count,
    }
    print(f"  Entities: {entity_count}, Relationships: {rel_count}")

    await store.close()

    # --- Write report ---
    _write_report(results)
    return results


def _write_report(results: dict) -> None:
    """Write smoke test results to markdown log."""
    lines = [
        f"# Smoke Test Log — {results['timestamp']}",
        f"",
        f"Git SHA: `{results['git_sha']}`",
        f"",
    ]

    for test_name, test_data in results["tests"].items():
        lines.append(f"## {test_name}")
        if isinstance(test_data, dict):
            status = test_data.get("status", "")
            lines.append(f"**Status**: {status}")
            for k, v in test_data.items():
                if k == "status":
                    continue
                if isinstance(v, list):
                    lines.append(f"- **{k}**:")
                    for item in v:
                        if isinstance(item, dict):
                            details = ", ".join(f"{ik}={iv}" for ik, iv in item.items())
                            lines.append(f"  - {details}")
                        else:
                            lines.append(f"  - {item}")
                else:
                    lines.append(f"- **{k}**: {v}")
        else:
            lines.append(str(test_data))
        lines.append("")

    report_path = TUNING_DIR / "smoke_test_log.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport written to {report_path}")


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()
    results = asyncio.run(run_smoke_test())
    # Exit with error if any critical test failed
    tests = results.get("tests", {})
    spark = tests.get("spark_connectivity", {})
    if spark.get("status") == "FAIL":
        print("\nWARNING: Spark not available — LLM features will not work")
        sys.exit(1)
    print("\n=== Smoke test complete ===")


if __name__ == "__main__":
    main()
