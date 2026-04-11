"""Tests for AdmissionService — feature extraction, scoring, and routing.

4 pure text heuristic features: utility, temporal_salience, persistence, state_change_signal.
No index or LLM dependency.
"""

import pytest

from ncms.application.admission_service import AdmissionService
from ncms.config import NCMSConfig
from ncms.infrastructure.graph.networkx_store import NetworkXGraph
from ncms.infrastructure.indexing.tantivy_engine import TantivyEngine
from ncms.infrastructure.storage.sqlite_store import SQLiteStore


@pytest.fixture
def config() -> NCMSConfig:
    return NCMSConfig(db_path=":memory:", actr_noise=0.0, admission_enabled=True)


@pytest.fixture
async def store():
    s = SQLiteStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def index() -> TantivyEngine:
    engine = TantivyEngine()
    engine.initialize()
    return engine


@pytest.fixture
def graph() -> NetworkXGraph:
    return NetworkXGraph()


@pytest.fixture
def admission(store, index, graph, config) -> AdmissionService:
    return AdmissionService(store=store, index=index, graph=graph, config=config)


class TestUtility:
    async def test_decision_content_high_utility(self, admission: AdmissionService):
        """Content with decision markers → high utility."""
        features = await admission.compute_features(
            "We decided to use PostgreSQL instead of MySQL for the main database"
        )
        assert features.utility > 0.3

    async def test_generic_low_utility(self, admission: AdmissionService):
        """Generic content with no markers → low utility."""
        features = await admission.compute_features("hello world")
        assert features.utility < 0.15

    async def test_incident_high_utility(self, admission: AdmissionService):
        """Incident content → high utility."""
        features = await admission.compute_features(
            "Production error: API crashed due to memory leak, fixed with hotfix"
        )
        assert features.utility > 0.3

    async def test_architecture_high_utility(self, admission: AdmissionService):
        """Architecture content → high utility."""
        features = await admission.compute_features(
            "Architectural decision: use event-driven pattern for microservices"
        )
        assert features.utility > 0.3


class TestTemporalSalience:
    async def test_iso_date_high(self, admission: AdmissionService):
        """Content with ISO date → high temporal salience."""
        features = await admission.compute_features(
            "Deployed as of 2026-03-01 to production servers"
        )
        assert features.temporal_salience > 0.3

    async def test_informal_date_high(self, admission: AdmissionService):
        """Informal date → high temporal salience."""
        features = await admission.compute_features(
            "API v2 release scheduled for March 2026"
        )
        assert features.temporal_salience > 0.3

    async def test_temporal_markers(self, admission: AdmissionService):
        """Temporal markers → elevated temporal salience."""
        features = await admission.compute_features(
            "Currently the service uses gRPC since the migration"
        )
        assert features.temporal_salience > 0.1

    async def test_no_temporal_low(self, admission: AdmissionService):
        """No temporal information → low temporal salience."""
        features = await admission.compute_features("Generic information about code")
        assert features.temporal_salience < 0.15


class TestPersistence:
    async def test_architectural_high(self, admission: AdmissionService):
        """Architectural decision → high persistence."""
        features = await admission.compute_features(
            "Architectural decision: always use dependency injection for services"
        )
        assert features.persistence > 0.60

    async def test_ephemeral_low(self, admission: AdmissionService):
        """WIP/temporary content → low persistence."""
        features = await admission.compute_features(
            "TODO: this is a temporary workaround, draft implementation"
        )
        assert features.persistence < 0.30

    async def test_default_medium(self, admission: AdmissionService):
        """Normal content → medium persistence."""
        features = await admission.compute_features(
            "The user service handles authentication"
        )
        assert 0.30 <= features.persistence <= 0.60


class TestStateChangeSignal:
    async def test_status_change_high(self, admission: AdmissionService):
        """Status change content → high state_change_signal."""
        features = await admission.compute_features(
            "Service status changed from healthy to degraded, is now critical"
        )
        assert features.state_change_signal > 0.3

    async def test_version_update(self, admission: AdmissionService):
        """Version reference → elevated state_change_signal."""
        features = await admission.compute_features(
            "Updated library from v2.1.0 to v3.0.0"
        )
        assert features.state_change_signal > 0.2

    async def test_no_state_change_low(self, admission: AdmissionService):
        """No state change indicators → low signal."""
        features = await admission.compute_features(
            "This is a general description of the system"
        )
        assert features.state_change_signal < 0.15


# ---------------------------------------------------------------------------
# Full Pipeline — evaluate() and routing
# ---------------------------------------------------------------------------


class TestFullPipeline:
    async def test_evaluate_returns_tuple(self, admission: AdmissionService):
        """evaluate() returns (features, score, route) tuple."""
        features, score, route = await admission.evaluate(
            "Some content", domains=["test"]
        )
        assert isinstance(score, float)
        assert route in {"discard", "ephemeral_cache", "persist"}
        assert features.utility >= 0.0
        assert features.temporal_salience >= 0.0
        assert features.persistence >= 0.0
        assert features.state_change_signal >= 0.0

    async def test_high_quality_content_routes_to_persist(
        self, admission: AdmissionService
    ):
        """High-quality content should pass quality gate (persist).

        Scoring uses 4 active features: utility (0.30), persistence (0.25),
        state_change_signal (0.25), temporal_salience (0.20).
        """
        _, score, route = await admission.evaluate(
            "Architectural decision: we chose PostgreSQL for the primary database "
            "due to its JSON support and reliability. This was decided on 2026-03-01 "
            "after evaluating multiple options.",
        )
        assert score > 0.35
        assert route == "persist"

    async def test_generic_content_routes_to_ephemeral_or_discard(
        self, admission: AdmissionService
    ):
        """Generic low-value content should be ephemeral or discarded."""
        _, score, route = await admission.evaluate("hello world")
        assert route in {"discard", "ephemeral_cache"}

    async def test_state_change_promotes_to_persist(
        self, admission: AdmissionService
    ):
        """State change signal >= 0.35 promotes to persist regardless of score."""
        _, score, route = await admission.evaluate(
            "Service status changed from healthy to degraded, is now critical"
        )
        assert route == "persist"

    async def test_all_features_populated(self, admission: AdmissionService):
        """All 4 features should be non-negative."""
        features = await admission.compute_features(
            "Content for testing all features"
        )
        assert features.utility >= 0.0
        assert features.temporal_salience >= 0.0
        assert features.persistence >= 0.0
        assert features.state_change_signal >= 0.0
