"""Tests for MSEB CTLG shadow diagnostic plumbing."""

from __future__ import annotations

from benchmarks.mseb.backends.base import BackendRanking
from benchmarks.mseb.harness import FeatureSet, RunConfig, _run_queries
from benchmarks.mseb.schema import GoldQuery


class _FakeBackend:
    def __init__(self) -> None:
        self.shadow_gold: set[str] | None = None
        self.shadow_subject: str | None = None

    async def search_with_stages(
        self,
        *,
        query: str,
        limit: int,
        capture_stages: bool,
    ) -> tuple[list[BackendRanking], dict[str, list[str]]]:
        assert query == "What is current?"
        assert limit == 10
        assert capture_stages is True
        return [BackendRanking(mid="m2", score=1.0)], {"returned": ["m2"]}

    def classify_query(self, query: str) -> dict[str, object]:
        assert query == "What is current?"
        return {"intent": "fact_lookup", "intent_conf": 0.8}

    async def ctlg_shadow_query(
        self,
        query: str,
        *,
        gold_mids: set[str],
        gold_subject: str | None = None,
    ) -> dict[str, object]:
        assert query == "What is current?"
        self.shadow_gold = set(gold_mids)
        self.shadow_subject = gold_subject
        return {
            "mode": "ctlg_shadow",
            "rank_before": 2,
            "rank_after": 1,
            "would_compose": True,
        }


async def test_run_queries_embeds_ctlg_shadow_diagnostics_without_changing_rankings() -> None:
    backend = _FakeBackend()
    queries = [
        GoldQuery(
            qid="q1",
            shape="current_state",
            text="What is current?",
            subject="auth",
            gold_mid="m1",
            gold_alt=["m2"],
        )
    ]

    preds = await _run_queries(backend, queries)

    assert backend.shadow_gold == {"m1", "m2"}
    assert backend.shadow_subject == "auth"
    assert len(preds) == 1
    assert preds[0].ranked_mids == ["m2"]
    assert preds[0].intent_confidence == 0.8
    assert preds[0].head_outputs["ctlg_shadow"] == {
        "mode": "ctlg_shadow",
        "rank_before": 2,
        "rank_after": 1,
        "would_compose": True,
    }


def test_run_config_carries_ctlg_adapter_selection(tmp_path) -> None:
    cfg = RunConfig(
        domain="mseb_softwaredev",
        build_dir=tmp_path,
        backend="ncms",
        adapter_domain="software_dev",
        feature_set=FeatureSet(),
        out_dir=tmp_path,
        run_id="run",
        ctlg_adapter_domain="software_dev",
        ctlg_adapter_version="ctlg-v1",
    )

    assert cfg.ctlg_adapter_domain == "software_dev"
    assert cfg.ctlg_adapter_version == "ctlg-v1"
