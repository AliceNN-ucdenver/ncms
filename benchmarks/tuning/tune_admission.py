"""Admission weight grid search — finds optimal routing thresholds and feature weights.

Creates labeled evaluation examples representing typical NCMS usage, then grid-searches
over admission parameters to maximize route accuracy.

Features (4 pure text heuristics):
    utility (0.30), persistence (0.25), state_change_signal (0.25), temporal_salience (0.20)

Results written to:
- benchmarks/tuning/admission_grid_results.json  (all configs + scores)
- benchmarks/tuning/admission_grid_report.md      (best config + analysis)

Usage:
    uv run python benchmarks/tuning/tune_admission.py
"""

from __future__ import annotations

import itertools
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ncms.domain.scoring import AdmissionFeatures, score_admission

TUNING_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Labeled evaluation examples
# ---------------------------------------------------------------------------

@dataclass
class LabeledExample:
    """Content with expected admission route."""
    content: str
    expected_route: str  # persist | ephemeral_cache | discard
    category: str  # human-readable category for reporting


EVALUATION_EXAMPLES: list[LabeledExample] = [
    # --- persist: architecture decisions, important facts ---
    LabeledExample(
        "We decided to migrate from REST to gRPC for all internal service communication",
        "persist", "architecture_decision",
    ),
    LabeledExample(
        "The authentication service uses OAuth 2.0 with PKCE flow for mobile clients",
        "persist", "architecture_fact",
    ),
    LabeledExample(
        "PostgreSQL 16 was chosen as the primary database for the user service",
        "persist", "technology_choice",
    ),
    LabeledExample(
        "The API rate limit is set to 1000 requests per minute per API key",
        "persist", "configuration",
    ),
    LabeledExample(
        "All microservices must implement health check endpoints at /healthz",
        "persist", "policy",
    ),
    LabeledExample(
        "The frontend uses React 18 with server-side rendering via Next.js",
        "persist", "architecture_fact",
    ),
    LabeledExample(
        "CI/CD pipeline runs on GitHub Actions with Docker-based build steps",
        "persist", "infrastructure",
    ),
    LabeledExample(
        "The search service uses Elasticsearch 8.x with custom analyzers"
        " for multi-language support",
        "persist", "architecture_fact",
    ),
    LabeledExample(
        "Data retention policy requires user data deletion within 30 days of account closure",
        "persist", "policy",
    ),
    LabeledExample(
        "The payment processing module integrates with Stripe API v2023-10"
        " for all transactions",
        "persist", "integration",
    ),
    LabeledExample(
        "Redis cluster with 6 nodes handles session management and caching"
        " across all services",
        "persist", "infrastructure",
    ),
    LabeledExample(
        "GraphQL federation is used to compose the unified API gateway from 12 subgraphs",
        "persist", "architecture_fact",
    ),

    # --- persist: state changes, version updates ---
    LabeledExample(
        "The auth service was updated from OAuth 1.0 to OAuth 2.0 as of January 2026",
        "persist", "state_change",
    ),
    LabeledExample(
        "Database version upgraded from PostgreSQL 14 to PostgreSQL 16",
        "persist", "version_change",
    ),
    LabeledExample(
        "The payment gateway switched from Stripe to Adyen in production",
        "persist", "provider_change",
    ),
    LabeledExample(
        "API endpoint /v2/users replaced the deprecated /v1/users endpoint",
        "persist", "api_change",
    ),
    LabeledExample(
        "Monitoring moved from Datadog to Grafana Cloud as of March 2026",
        "persist", "tool_change",
    ),
    LabeledExample(
        "The user service status changed from degraded to healthy"
        " after the fix was deployed",
        "persist", "status_change",
    ),
    LabeledExample(
        "Container runtime migrated from Docker to containerd"
        " across all Kubernetes clusters",
        "persist", "infrastructure_change",
    ),
    LabeledExample(
        "Node.js version bumped from 18 to 20 LTS for all backend services",
        "persist", "version_change",
    ),

    # --- ephemeral_cache: useful but transient ---
    LabeledExample(
        "I'm looking into the auth issue, will update once I have more info",
        "ephemeral_cache", "status_update",
    ),
    LabeledExample(
        "The build is currently failing on the staging branch, investigating now",
        "ephemeral_cache", "investigation",
    ),
    LabeledExample(
        "Can someone check if the API is returning 500 errors on the health endpoint?",
        "ephemeral_cache", "question",
    ),
    LabeledExample(
        "Working on the PR for the caching layer, should be ready for review tomorrow",
        "ephemeral_cache", "work_in_progress",
    ),
    LabeledExample(
        "Meeting notes: discussed roadmap priorities for Q2,"
        " need to follow up on auth redesign",
        "ephemeral_cache", "meeting_note",
    ),
    LabeledExample(
        "TODO: review the error handling in the payment module before merge",
        "ephemeral_cache", "todo",
    ),
    LabeledExample(
        "Testing the new deployment pipeline, so far looks good but need more coverage",
        "ephemeral_cache", "progress_note",
    ),
    LabeledExample(
        "Quick note: the staging environment is down for maintenance until 3pm",
        "ephemeral_cache", "temporary_notice",
    ),

    # --- discard: noise, social, trivial ---
    LabeledExample("ok", "discard", "noise"),
    LabeledExample("thanks", "discard", "noise"),
    LabeledExample("sounds good", "discard", "noise"),
    LabeledExample("lgtm", "discard", "noise"),
    LabeledExample("sure thing", "discard", "noise"),
    LabeledExample("hello team", "discard", "greeting"),
    LabeledExample("have a great weekend everyone", "discard", "social"),
    LabeledExample("brb", "discard", "noise"),
    LabeledExample("np", "discard", "noise"),
    LabeledExample("+1", "discard", "noise"),

    # --- persist: incidents, causal chains ---
    LabeledExample(
        "INCIDENT: Production API latency spiked to 5s at 14:00 UTC"
        " due to database connection pool exhaustion",
        "persist", "incident",
    ),
    LabeledExample(
        "Root cause identified: the connection pool leak was caused by"
        " unclosed transactions in the batch processor",
        "persist", "root_cause",
    ),
    LabeledExample(
        "Hotfix deployed: increased connection pool size from 20 to 50"
        " and added connection timeout of 30s",
        "persist", "resolution",
    ),
    LabeledExample(
        "Post-incident review: the monitoring alert for connection pool usage"
        " fired 10 minutes after the spike started",
        "persist", "post_mortem",
    ),
    LabeledExample(
        "Sprint retrospective: the auth refactor introduced a regression"
        " in token validation affecting 3 downstream services",
        "persist", "retrospective",
    ),
    LabeledExample(
        "Deployment of v2.3.1 to production started at 09:00,"
        " includes fix for JIRA-4521 payment timeout issue",
        "persist", "deployment",
    ),
]


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def extract_features_for_content(content: str, svc: object) -> AdmissionFeatures:
    """Extract admission features using AdmissionService's text heuristics."""
    text_lower = content.lower()

    return AdmissionFeatures(
        utility=svc._compute_utility(text_lower),  # type: ignore[attr-defined]
        temporal_salience=svc._compute_temporal_salience(  # type: ignore[attr-defined]
            text_lower, content,
        ),
        persistence=svc._compute_persistence(text_lower),  # type: ignore[attr-defined]
        state_change_signal=svc._compute_state_change_signal(  # type: ignore[attr-defined]
            text_lower,
        ),
    )


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

@dataclass
class RoutingConfig:
    """Routing threshold configuration to test."""
    discard_threshold: float
    ephemeral_upper: float
    state_change_threshold: float


def route_with_config(
    features: AdmissionFeatures,
    score: float,
    cfg: RoutingConfig,
) -> str:
    """Route memory using custom thresholds (3-way monotonic quality gate)."""
    # State change auto-promotes
    if features.state_change_signal >= cfg.state_change_threshold:
        return "persist"
    # Monotonic thresholds
    if score < cfg.discard_threshold:
        return "discard"
    if score < cfg.ephemeral_upper:
        return "ephemeral_cache"
    return "persist"


@dataclass
class WeightConfig:
    """Feature weight configuration to test (must sum to ~1.0)."""
    utility: float = 0.30
    persistence: float = 0.25
    state_change_signal: float = 0.25
    temporal_salience: float = 0.20

    def score(self, f: AdmissionFeatures) -> float:
        return (
            self.utility * f.utility
            + self.persistence * f.persistence
            + self.state_change_signal * f.state_change_signal
            + self.temporal_salience * f.temporal_salience
        )


def run_grid_search() -> dict:
    """Run grid search over routing thresholds and weight configs."""
    from ncms.application.admission_service import AdmissionService
    from ncms.config import NCMSConfig
    from ncms.infrastructure.graph.networkx_store import NetworkXGraph
    from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine

    # Create a lightweight service for feature extraction
    config = NCMSConfig(db_path=":memory:", admission_enabled=True)

    class _FakeStore:
        """Minimal store stub for admission service init."""
        async def initialize(self) -> None: pass
        async def close(self) -> None: pass

    admission_svc = AdmissionService(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=TantivyEngine(),
        graph=NetworkXGraph(),
        config=config,
    )

    print("Extracting features for all examples...")

    # Pre-extract features for all examples
    examples_with_features = []
    for ex in EVALUATION_EXAMPLES:
        features = extract_features_for_content(ex.content, admission_svc)
        examples_with_features.append((ex, features))
        print(
            f"  [{ex.expected_route:>20}] score={score_admission(features):.3f} "
            f"util={features.utility:.2f} persist={features.persistence:.2f} "
            f"state_chg={features.state_change_signal:.2f} "
            f"temporal={features.temporal_salience:.2f} "
            f"{ex.content[:50]}..."
        )

    # Grid: routing thresholds
    discard_thresholds = [0.05, 0.08, 0.10, 0.12, 0.15]
    ephemeral_uppers = [0.20, 0.25, 0.30, 0.35]
    state_change_thresholds = [0.30, 0.35, 0.40]

    # Grid: weight variations
    weight_configs = [
        WeightConfig(),  # default: 0.30, 0.25, 0.25, 0.20
        WeightConfig(utility=0.35, persistence=0.20),
        WeightConfig(utility=0.25, persistence=0.30),
        WeightConfig(utility=0.25, state_change_signal=0.30),
        WeightConfig(state_change_signal=0.30, temporal_salience=0.15),
        WeightConfig(utility=0.35, temporal_salience=0.15),
        WeightConfig(utility=0.20, persistence=0.30, state_change_signal=0.30,
                     temporal_salience=0.20),
    ]

    all_results = []
    best_accuracy = 0.0
    best_config = None

    routing_grid = list(itertools.product(
        discard_thresholds, ephemeral_uppers, state_change_thresholds,
    ))

    total_combos = len(routing_grid) * len(weight_configs)
    print(
        f"\nGrid search: {total_combos} configurations "
        f"({len(routing_grid)} routing × {len(weight_configs)} weight variants)"
    )

    for _wi, wc in enumerate(weight_configs):
        for dt, eu, sct in routing_grid:
            rc = RoutingConfig(
                discard_threshold=dt,
                ephemeral_upper=eu,
                state_change_threshold=sct,
            )

            correct = 0
            per_category: dict[str, dict[str, int]] = {}

            for ex, features in examples_with_features:
                admission_score = wc.score(features)
                predicted = route_with_config(features, admission_score, rc)

                cat = ex.expected_route
                if cat not in per_category:
                    per_category[cat] = {"correct": 0, "total": 0}
                per_category[cat]["total"] += 1

                if predicted == ex.expected_route:
                    correct += 1
                    per_category[cat]["correct"] += 1

            accuracy = correct / len(examples_with_features)

            result_entry = {
                "weights": {
                    "utility": wc.utility,
                    "persistence": wc.persistence,
                    "state_change_signal": wc.state_change_signal,
                    "temporal_salience": wc.temporal_salience,
                },
                "routing": {
                    "discard_threshold": dt,
                    "ephemeral_upper": eu,
                    "state_change_threshold": sct,
                },
                "accuracy": accuracy,
                "correct": correct,
                "total": len(examples_with_features),
                "per_category": {
                    k: v["correct"] / v["total"]
                    for k, v in per_category.items()
                },
            }
            all_results.append(result_entry)

            if accuracy > best_accuracy:
                best_accuracy = accuracy
                best_config = result_entry

    # Sort by accuracy descending
    all_results.sort(key=lambda r: r["accuracy"], reverse=True)

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": _get_git_sha(),
        "num_examples": len(EVALUATION_EXAMPLES),
        "num_configs_tested": total_combos,
        "best_accuracy": best_accuracy,
        "best_config": best_config,
        "top_10": all_results[:10],
        "all_results": all_results,
    }


def _get_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        return "unknown"


def _write_results(results: dict) -> None:
    """Write grid search results to JSON and markdown report."""
    # JSON with summary (no full all_results)
    json_path = TUNING_DIR / "admission_grid_results.json"
    summary = {k: v for k, v in results.items() if k != "all_results"}
    summary["total_configs"] = results["num_configs_tested"]
    json_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Results JSON: {json_path}")

    # Markdown report
    best = results["best_config"]
    report_lines = [
        "# Admission Weight Tuning Report",
        "",
        f"**Date**: {results['timestamp']}",
        f"**Git SHA**: `{results['git_sha']}`",
        f"**Examples**: {results['num_examples']}",
        f"**Configs tested**: {results['num_configs_tested']}",
        "",
        "## Best Configuration",
        "",
        f"**Accuracy**: {best['accuracy']:.1%} ({best['correct']}/{best['total']})",
        "",
        "### Routing Thresholds",
        "",
        "| Parameter | Default | Tuned |",
        "|-----------|---------|-------|",
        f"| Discard threshold | 0.10 | {best['routing']['discard_threshold']} |",
        f"| Ephemeral upper | 0.25 | {best['routing']['ephemeral_upper']} |",
        f"| State change threshold | 0.35 | {best['routing']['state_change_threshold']} |",
        "",
        "### Feature Weights",
        "",
        "| Feature | Default | Tuned |",
        "|---------|---------|-------|",
    ]

    default_weights = {
        "utility": 0.30,
        "persistence": 0.25,
        "state_change_signal": 0.25,
        "temporal_salience": 0.20,
    }

    for feat, default_val in default_weights.items():
        tuned_val = best["weights"][feat]
        marker = " *" if abs(tuned_val - default_val) > 0.001 else ""
        report_lines.append(
            f"| {feat} | {default_val} | {tuned_val}{marker} |"
        )

    report_lines.extend([
        "",
        "### Per-Category Accuracy",
        "",
        "| Route | Accuracy |",
        "|-------|----------|",
    ])
    for cat, acc in sorted(best["per_category"].items()):
        report_lines.append(f"| {cat} | {acc:.1%} |")

    report_lines.extend([
        "",
        "## Top 10 Configurations",
        "",
        "| # | Accuracy | Discard | Ephemeral | StateChg |",
        "|---|----------|---------|-----------|----------|",
    ])
    for i, cfg in enumerate(results["top_10"], 1):
        r = cfg["routing"]
        report_lines.append(
            f"| {i} | {cfg['accuracy']:.1%} | {r['discard_threshold']} | "
            f"{r['ephemeral_upper']} | {r['state_change_threshold']} |"
        )

    report_path = TUNING_DIR / "admission_grid_report.md"
    report_path.write_text("\n".join(report_lines) + "\n")
    print(f"Report: {report_path}")


def main() -> None:
    from benchmarks.env import load_dotenv
    load_dotenv()
    t0 = time.perf_counter()
    results = run_grid_search()
    elapsed = time.perf_counter() - t0

    print(f"\n=== Grid search complete in {elapsed:.1f}s ===")
    print(f"Best accuracy: {results['best_accuracy']:.1%}")
    best = results["best_config"]
    print(
        f"Best routing: discard<{best['routing']['discard_threshold']} "
        f"ephemeral<{best['routing']['ephemeral_upper']} "
        f"state_chg>={best['routing']['state_change_threshold']}"
    )

    _write_results(results)


if __name__ == "__main__":
    main()
