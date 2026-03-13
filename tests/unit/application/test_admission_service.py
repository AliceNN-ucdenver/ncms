"""Tests for AdmissionService — feature extraction, scoring, and routing."""

import pytest

from ncms.application.admission_service import AdmissionService
from ncms.config import NCMSConfig
from ncms.domain.models import Memory
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


class TestNovelty:
    async def test_empty_store_full_novelty(self, admission: AdmissionService):
        """Empty index → novelty = 1.0."""
        features = await admission.compute_features("brand new information")
        assert features.novelty == 1.0

    async def test_duplicate_low_novelty(
        self, admission: AdmissionService, store: SQLiteStore, index: TantivyEngine
    ):
        """Content already indexed → low novelty."""
        existing = Memory(content="The API endpoint returns JSON data", domains=["api"])
        await store.save_memory(existing)
        index.index_memory(existing)

        features = await admission.compute_features("The API endpoint returns JSON data")
        assert features.novelty < 0.5

    async def test_different_content_high_novelty(
        self, admission: AdmissionService, store: SQLiteStore, index: TantivyEngine
    ):
        """Very different content from what's indexed → high novelty."""
        existing = Memory(content="React component uses useState hook", domains=["frontend"])
        await store.save_memory(existing)
        index.index_memory(existing)

        features = await admission.compute_features(
            "PostgreSQL database schema migration strategy"
        )
        assert features.novelty > 0.5


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


class TestReliability:
    async def test_system_source_high(self, admission: AdmissionService):
        """System source → high reliability."""
        features = await admission.compute_features(
            "System report generated",
            source_type="system",
        )
        assert features.reliability >= 0.85

    async def test_speculative_source_low(self, admission: AdmissionService):
        """Speculative source → low reliability."""
        features = await admission.compute_features(
            "Some data point",
            source_type="speculative",
        )
        assert features.reliability <= 0.35

    async def test_hedging_penalizes(self, admission: AdmissionService):
        """Hedging language reduces reliability."""
        confident = await admission.compute_features("The database uses PostgreSQL")
        hedging = await admission.compute_features(
            "I think maybe the database possibly uses PostgreSQL, not sure"
        )
        assert hedging.reliability < confident.reliability

    async def test_default_observed(self, admission: AdmissionService):
        """Default (no source type) → observed level."""
        features = await admission.compute_features("Normal content here")
        assert 0.40 <= features.reliability <= 0.70


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


class TestRedundancy:
    async def test_empty_store_no_redundancy(self, admission: AdmissionService):
        """Empty index → zero redundancy."""
        features = await admission.compute_features("new info")
        assert features.redundancy == 0.0

    async def test_duplicate_high_redundancy(
        self, admission: AdmissionService, store: SQLiteStore, index: TantivyEngine
    ):
        """Same content already stored → high redundancy."""
        existing = Memory(content="The API endpoint returns JSON data", domains=["api"])
        await store.save_memory(existing)
        index.index_memory(existing)

        features = await admission.compute_features("The API endpoint returns JSON data")
        assert features.redundancy > 0.3


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


class TestEpisodeAffinity:
    async def test_always_zero_in_phase1(self, admission: AdmissionService):
        """Phase 1 stub — always returns 0.0."""
        features = await admission.compute_features("any content at all")
        assert features.episode_affinity == 0.0


class TestFullPipeline:
    async def test_evaluate_returns_tuple(self, admission: AdmissionService):
        """evaluate() returns (features, score, route) tuple."""
        features, score, route = await admission.evaluate(
            "Some content", domains=["test"]
        )
        assert isinstance(score, float)
        assert route in {
            "discard", "ephemeral_cache", "atomic_memory",
            "entity_state_update", "episode_fragment",
        }
        assert features.novelty >= 0.0

    async def test_high_quality_content_routes_to_atomic(
        self, admission: AdmissionService
    ):
        """High-quality content should route to atomic_memory."""
        _, score, route = await admission.evaluate(
            "Architectural decision: we chose PostgreSQL for the primary database "
            "due to its JSON support and reliability. This was decided on 2026-03-01 "
            "after evaluating multiple options.",
            source_type="authoritative",
        )
        assert score > 0.45
        assert route == "atomic_memory"

    async def test_generic_content_routes_to_ephemeral_or_discard(
        self, admission: AdmissionService
    ):
        """Generic low-value content should be ephemeral or discarded."""
        _, score, route = await admission.evaluate("hello world")
        assert route in {"discard", "ephemeral_cache"}

    async def test_all_features_populated(self, admission: AdmissionService):
        """All 8 features should be populated (non-negative)."""
        features = await admission.compute_features(
            "Content for testing all features"
        )
        assert features.novelty >= 0.0
        assert features.utility >= 0.0
        assert features.reliability >= 0.0
        assert features.temporal_salience >= 0.0
        assert features.persistence >= 0.0
        assert features.redundancy >= 0.0
        assert features.episode_affinity >= 0.0
        assert features.state_change_signal >= 0.0
