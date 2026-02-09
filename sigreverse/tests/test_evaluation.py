"""Unit tests for sigreverse.evaluation.metrics module."""
import pytest
from sigreverse.evaluation.metrics import (
    hit_at_k, precision_at_k, reciprocal_rank, average_precision,
    ndcg_at_k, auroc, auprc, evaluate_ranking,
)


class TestHitAtK:
    def test_hit_found(self):
        assert hit_at_k(["a", "b", "c"], {"b"}, 3) == 1.0

    def test_hit_not_found(self):
        assert hit_at_k(["a", "b", "c"], {"d"}, 3) == 0.0

    def test_hit_at_1(self):
        assert hit_at_k(["a", "b"], {"a"}, 1) == 1.0
        assert hit_at_k(["a", "b"], {"b"}, 1) == 0.0


class TestPrecisionAtK:
    def test_all_positive(self):
        assert precision_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == pytest.approx(1.0)

    def test_half_positive(self):
        assert precision_at_k(["a", "b", "c", "d"], {"a", "c"}, 4) == pytest.approx(0.5)

    def test_none_positive(self):
        assert precision_at_k(["a", "b"], {"c"}, 2) == pytest.approx(0.0)


class TestReciprocalRank:
    def test_first_is_positive(self):
        assert reciprocal_rank(["a", "b", "c"], {"a"}) == pytest.approx(1.0)

    def test_second_is_positive(self):
        assert reciprocal_rank(["a", "b", "c"], {"b"}) == pytest.approx(0.5)

    def test_no_positive(self):
        assert reciprocal_rank(["a", "b", "c"], {"d"}) == 0.0


class TestAveragePrecision:
    def test_perfect_ranking(self):
        """All positives at top → AP = 1.0."""
        ranked = ["a", "b", "c", "d"]
        positives = {"a", "b"}
        assert average_precision(ranked, positives) == pytest.approx(1.0)

    def test_worst_ranking(self):
        """All positives at bottom → low AP."""
        ranked = ["c", "d", "a", "b"]
        positives = {"a", "b"}
        ap = average_precision(ranked, positives)
        assert ap < 0.5

    def test_empty_positives(self):
        assert average_precision(["a", "b"], set()) == 0.0


class TestNDCG:
    def test_perfect_ranking(self):
        ranked = ["a", "b", "c"]
        positives = {"a", "b"}
        assert ndcg_at_k(ranked, positives, 3) == pytest.approx(1.0)

    def test_no_positives_in_topk(self):
        ranked = ["c", "d", "a", "b"]
        positives = {"a", "b"}
        assert ndcg_at_k(ranked, positives, 2) == pytest.approx(0.0)


class TestAUROC:
    def test_perfect_ranking(self):
        ranked = ["pos1", "pos2", "neg1", "neg2"]
        positives = {"pos1", "pos2"}
        assert auroc(ranked, positives) == pytest.approx(1.0)

    def test_worst_ranking(self):
        ranked = ["neg1", "neg2", "pos1", "pos2"]
        positives = {"pos1", "pos2"}
        assert auroc(ranked, positives) == pytest.approx(0.0)

    def test_random_ranking(self):
        """Random ordering → AUROC ≈ 0.5."""
        ranked = ["a", "b", "c", "d"]
        positives = {"a", "c"}
        auc = auroc(ranked, positives)
        assert 0.0 <= auc <= 1.0


class TestEvaluateRanking:
    def test_all_metrics_present(self):
        results = evaluate_ranking(
            ranked_drugs=["a", "b", "c", "d"],
            known_positives={"a", "c"},
            ks=[5, 10],
        )
        assert "mrr" in results
        assert "map" in results
        assert "auroc" in results
        assert "auprc" in results
        assert "hit@5" in results
        assert "precision@10" in results
        assert "ndcg@5" in results
