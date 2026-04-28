"""Tests for personalized PageRank graph scoring."""

from __future__ import annotations

import pytest

from ncms.domain.scoring import ppr_graph_score


class TestPPRGraphScore:
    def test_empty_memory_entities(self) -> None:
        result = ppr_graph_score([], {"e1": 0.5})
        assert result == 0.0

    def test_empty_ppr_scores(self) -> None:
        result = ppr_graph_score(["e1", "e2"], {})
        assert result == 0.0

    def test_both_empty(self) -> None:
        result = ppr_graph_score([], {})
        assert result == 0.0

    def test_single_entity_match(self) -> None:
        result = ppr_graph_score(["e1"], {"e1": 0.5, "e2": 0.3})
        assert result == pytest.approx(0.5)

    def test_single_entity_no_match(self) -> None:
        result = ppr_graph_score(["e3"], {"e1": 0.5, "e2": 0.3})
        assert result == 0.0

    def test_multi_entity_mean(self) -> None:
        # Mean-pooled: (0.3 + 0.2) / 2 = 0.25
        result = ppr_graph_score(["e1", "e2"], {"e1": 0.3, "e2": 0.2, "e3": 0.1})
        assert result == pytest.approx(0.25)

    def test_idf_weighting(self) -> None:
        # With IDF: e1 (rare, IDF=5) should contribute more than e2 (common, IDF=1)
        result_with_idf = ppr_graph_score(
            ["e1", "e2"],
            {"e1": 0.1, "e2": 0.1},
            entity_idf={"e1": 5.0, "e2": 1.0},
        )
        # Mean-pooled: (0.1*5.0 + 0.1*1.0) / 2 = 0.3
        assert result_with_idf == pytest.approx(0.3)

    def test_idf_default_weight(self) -> None:
        # Entities not in IDF dict get default weight 1.0
        result = ppr_graph_score(
            ["e1", "e2"],
            {"e1": 0.3, "e2": 0.2},
            entity_idf={"e1": 2.0},  # e2 not in IDF → default 1.0
        )
        # Mean-pooled: (0.3*2.0 + 0.2*1.0) / 2 = 0.4
        assert result == pytest.approx(0.4)

    def test_no_idf_dict(self) -> None:
        # When entity_idf is None, all weights are 1.0
        # Mean-pooled: (0.3 + 0.2) / 2 = 0.25
        result = ppr_graph_score(["e1", "e2"], {"e1": 0.3, "e2": 0.2}, entity_idf=None)
        assert result == pytest.approx(0.25)

    def test_zero_ppr_score_ignored(self) -> None:
        # Only e2 has PPR > 0, so mean is over count=1: 0.3/1 = 0.3
        result = ppr_graph_score(["e1", "e2"], {"e1": 0.0, "e2": 0.3})
        assert result == pytest.approx(0.3)
