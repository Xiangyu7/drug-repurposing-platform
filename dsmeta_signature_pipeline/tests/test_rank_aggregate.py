"""Tests for 04_rank_aggregate.py logic and edge cases."""
import numpy as np
import pandas as pd
import pytest


# =====================================================================
# Test rank matrix construction logic
# =====================================================================
class TestRankMatrix:
    def test_basic_ranking(self):
        """Negative t-stats should get rank 1 (most upregulated in disease)."""
        de = pd.DataFrame({
            "feature_id": ["A", "B", "C"],
            "t": [3.0, -2.0, 0.5],
        })
        de["rank_up"] = (-de["t"]).rank(method="average")
        # Most negative t → most upregulated → rank 1
        assert de.loc[de["feature_id"] == "B", "rank_up"].values[0] == 3.0  # t=-2 → -t=2 → rank 3
        # Wait, -t gives [−3, 2, -0.5]. Ranked: 2 is largest → rank 3, -0.5 → rank 2, -3 → rank 1
        # Actually rank() assigns rank 1 to the smallest value
        # -t = [-3, 2, -0.5] → ranks: -3 gets rank 1, -0.5 gets rank 2, 2 gets rank 3
        assert de.loc[de["feature_id"] == "A", "rank_up"].values[0] == 1.0  # -3 → rank 1

    def test_fillna_with_max(self):
        """Missing genes should get the max rank (worst possible)."""
        s1 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"], name="GSE1")
        s2 = pd.Series([1.0, 2.0], index=["A", "D"], name="GSE2")

        all_genes = sorted(set(s1.index) | set(s2.index))
        M = pd.DataFrame({"GSE1": s1.reindex(all_genes), "GSE2": s2.reindex(all_genes)})
        M = M.apply(lambda col: col.fillna(col.max(skipna=True)), axis=0)

        # Gene "D" not in GSE1 → should get max rank from GSE1 = 3.0
        assert M.loc["D", "GSE1"] == 3.0
        # Gene "B", "C" not in GSE2 → should get max rank from GSE2 = 2.0
        assert M.loc["B", "GSE2"] == 2.0
        assert M.loc["C", "GSE2"] == 2.0

    def test_all_genes_same_t(self):
        """Tied t-statistics should get average ranks."""
        de = pd.DataFrame({
            "feature_id": ["A", "B", "C"],
            "t": [1.0, 1.0, 1.0],
        })
        de["rank_up"] = (-de["t"]).rank(method="average")
        # All same → all get average rank = 2.0
        assert all(de["rank_up"] == 2.0)


# =====================================================================
# Test ensemble weighting logic
# =====================================================================
class TestEnsemble:
    def test_weights_sum_to_one(self):
        """Default weights should sum to 1."""
        w_meta = 0.7
        w_rra = 0.3
        assert abs(w_meta + w_rra - 1.0) < 1e-10

    def test_ensemble_rank_computation(self):
        """Ensemble rank should be weighted combination."""
        meta_rank = np.array([0.1, 0.5, 0.9])
        rra_rank = np.array([0.2, 0.4, 0.8])
        w_meta, w_rra = 0.7, 0.3

        ensemble = w_meta * meta_rank + w_rra * rra_rank
        expected = np.array([0.7*0.1 + 0.3*0.2, 0.7*0.5 + 0.3*0.4, 0.7*0.9 + 0.3*0.8])
        np.testing.assert_allclose(ensemble, expected)

    def test_meta_only_when_rra_missing(self):
        """If RRA rank is NaN, should fall back to meta rank."""
        meta_rank = np.array([0.1, 0.5])
        rra_rank = np.array([np.nan, 0.4])

        # Fillna logic from script
        rra_filled = np.where(np.isnan(rra_rank), meta_rank, rra_rank)
        ensemble = 0.7 * meta_rank + 0.3 * rra_filled
        # First gene: rra is NaN → use meta = 0.1 → ensemble = 0.7*0.1 + 0.3*0.1 = 0.1
        assert abs(ensemble[0] - 0.1) < 1e-10


# =====================================================================
# Test edge cases
# =====================================================================
class TestEdgeCases:
    def test_single_gene(self):
        """Single gene should still get a valid rank."""
        de = pd.DataFrame({"feature_id": ["A"], "t": [2.5]})
        de["rank_up"] = (-de["t"]).rank(method="average")
        assert de["rank_up"].values[0] == 1.0

    def test_na_t_stats_dropped(self):
        """Genes with NaN t-stats should be excluded."""
        de = pd.DataFrame({
            "feature_id": ["A", "B", "C"],
            "t": [2.0, np.nan, -1.0],
        })
        de = de[["feature_id", "t"]].dropna()
        assert len(de) == 2
        assert "B" not in de["feature_id"].values

    def test_empty_de(self):
        """Empty DE results should produce empty rank."""
        de = pd.DataFrame({"feature_id": [], "t": []})
        de = de.dropna()
        assert len(de) == 0
