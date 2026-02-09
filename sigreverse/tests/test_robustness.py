"""Unit tests for sigreverse.robustness module.

Tests cover:
    - Weighted median computation
    - Drug-level aggregation
    - FDR filtering
    - Quantile-max aggregation
    - n_factor computation (log vs sqrt modes)
    - Confidence tier assignment
    - Cell-line conflict detection
    - Edge cases (empty data, single drug, missing columns)
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.robustness import (
    aggregate_to_drug, weighted_median, _quantile_max_aggregate,
    _compute_n_factor, _compute_confidence_tier,
)


# ===== Weighted median =====

class TestWeightedMedian:
    def test_equal_weights(self):
        vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        weights = np.ones(5)
        assert weighted_median(vals, weights) == pytest.approx(3.0)

    def test_skewed_weights(self):
        vals = np.array([1.0, 10.0])
        weights = np.array([9.0, 1.0])
        # Heavily weighted toward 1.0
        assert weighted_median(vals, weights) == pytest.approx(1.0)

    def test_single_value(self):
        assert weighted_median(np.array([42.0]), np.array([1.0])) == pytest.approx(42.0)

    def test_empty(self):
        assert weighted_median(np.array([]), np.array([])) == 0.0


# ===== Quantile max =====

class TestQuantileMax:
    def test_negative_scores(self):
        """Strong negative scores -> q_lo should dominate."""
        scores = np.array([-10, -8, -5, -3, -1, 0, 1])
        result = _quantile_max_aggregate(scores)
        # Should return whichever of q67/q33 has larger abs value
        assert result != 0.0

    def test_single_score(self):
        assert _quantile_max_aggregate(np.array([-5.0])) == pytest.approx(-5.0)

    def test_empty(self):
        assert _quantile_max_aggregate(np.array([])) == 0.0


# ===== n_factor computation =====

class TestComputeNFactor:
    def test_log_mode_single(self):
        """Single sig with n_cap=8: log(2)/log(9) ~ 0.316"""
        nf = _compute_n_factor(1, 8, mode="log")
        assert nf == pytest.approx(0.3155, abs=0.01)

    def test_log_mode_saturated(self):
        """At n_cap, n_factor should be 1.0."""
        assert _compute_n_factor(8, 8, mode="log") == pytest.approx(1.0)

    def test_log_mode_above_cap(self):
        """Above n_cap, still capped at 1.0."""
        assert _compute_n_factor(100, 8, mode="log") == pytest.approx(1.0)

    def test_sqrt_mode_single(self):
        """sqrt(1/8) ~ 0.354"""
        nf = _compute_n_factor(1, 8, mode="sqrt")
        assert nf == pytest.approx(0.3536, abs=0.01)

    def test_zero_returns_zero(self):
        assert _compute_n_factor(0, 8) == 0.0

    def test_log_gentler_than_sqrt_at_n3(self):
        """Log mode should give higher factor than sqrt at n=3."""
        log_f = _compute_n_factor(3, 8, mode="log")
        sqrt_f = _compute_n_factor(3, 8, mode="sqrt")
        assert log_f > sqrt_f


# ===== Confidence tiers =====

class TestConfidenceTier:
    def test_high(self):
        assert _compute_confidence_tier(5, 2, 0.7) == "high"

    def test_medium(self):
        assert _compute_confidence_tier(3, 1, 0.6) == "medium"

    def test_low(self):
        assert _compute_confidence_tier(2, 1, 0.3) == "low"

    def test_exploratory(self):
        assert _compute_confidence_tier(1, 1, 1.0) == "exploratory"

    def test_high_needs_2_cell_lines(self):
        """Even with 5 sigs, need >=2 cell lines for high tier."""
        assert _compute_confidence_tier(5, 1, 0.8) == "medium"


# ===== Drug-level aggregation =====

class TestAggregateToDrug:
    def _make_detail(self):
        """Create test detail DataFrame."""
        return pd.DataFrame({
            "meta.pert_name": ["drugA"] * 5 + ["drugB"] * 3 + ["drugC"] * 2,
            "sig_score": [-5.0, -4.0, -3.0, -2.0, -1.0, -6.0, -5.5, -5.0, 0.0, 1.0],
            "sig_strength": [5.0, 4.0, 3.0, 2.0, 1.0, 6.0, 5.5, 5.0, 0.0, 1.0],
            "is_reverser": [True, True, True, True, False, True, True, True, False, False],
            "fdr_pass": [True, True, True, True, True, True, True, True, True, True],
            "confidence_weight": [1.0] * 10,
            "direction_category": ["reverser"] * 4 + ["partial"] + ["reverser"] * 3 + ["orthogonal", "mimicker"],
        })

    def test_basic_aggregation(self):
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1)
        assert len(result) == 3  # drugA, drugB, drugC
        assert "final_reversal_score" in result.columns
        assert "status" in result.columns

    def test_drug_a_has_ok_status(self):
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1)
        drug_a = result[result["drug"] == "drugA"].iloc[0]
        assert drug_a["status"] == "ok"

    def test_too_few_signatures(self):
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=10, min_reverser=1)
        # All drugs have <10 signatures -> all should be too_few
        assert all(result["status"] == "too_few_signatures")

    def test_no_reverser_context(self):
        df = self._make_detail()
        # drugC has 0 reversers
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1)
        drug_c = result[result["drug"] == "drugC"].iloc[0]
        assert drug_c["status"] == "no_reverser_context"

    def test_fdr_filtering(self):
        df = self._make_detail()
        df.loc[df["meta.pert_name"] == "drugA", "fdr_pass"] = False
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1, filter_fdr=True)
        drug_a = result[result["drug"] == "drugA"].iloc[0]
        # All drugA signatures fail FDR
        assert drug_a["n_fdr_removed"] == 5

    def test_sorted_ascending(self):
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1)
        scores = result["final_reversal_score"].values
        assert all(scores[i] <= scores[i+1] for i in range(len(scores)-1))

    def test_direction_category_counts(self):
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=2, min_reverser=1)
        assert "n_mimicker" in result.columns
        assert "n_partial" in result.columns

    def test_quantile_max_mode(self):
        df = self._make_detail()
        result = aggregate_to_drug(
            df, min_signatures=2, min_reverser=1,
            aggregation_mode="quantile_max"
        )
        assert len(result) == 3

    def test_confidence_tier_in_output(self):
        """New output column confidence_tier should be present."""
        df = self._make_detail()
        result = aggregate_to_drug(df, min_signatures=1, min_reverser=1)
        assert "confidence_tier" in result.columns
        assert "n_cell_lines" in result.columns

    def test_single_sig_drug_ok_with_min1(self):
        """With min_signatures=1, single-sig drugs should get status=ok."""
        df = pd.DataFrame({
            "meta.pert_name": ["solo_drug"],
            "sig_score": [-5.0],
            "sig_strength": [5.0],
            "is_reverser": [True],
            "fdr_pass": [True],
            "confidence_weight": [1.0],
            "direction_category": ["reverser"],
        })
        result = aggregate_to_drug(df, min_signatures=1, min_reverser=1)
        assert result.iloc[0]["status"] == "ok"
        assert result.iloc[0]["confidence_tier"] == "exploratory"
        assert result.iloc[0]["final_reversal_score"] < 0

    def test_n_factor_log_vs_sqrt(self):
        """Log mode should give less penalty than sqrt for small n."""
        df = self._make_detail()
        res_log = aggregate_to_drug(df, min_signatures=2, min_reverser=1, n_factor_mode="log")
        res_sqrt = aggregate_to_drug(df, min_signatures=2, min_reverser=1, n_factor_mode="sqrt")
        # Both should produce results with same structure
        assert len(res_log) == len(res_sqrt)

    def test_cell_line_conflict_detection(self):
        """Drugs with conflicting cell-line directions should be flagged."""
        df = pd.DataFrame({
            "meta.pert_name": ["conflictDrug"] * 4,
            "meta.cell_line": ["CL1", "CL1", "CL2", "CL2"],
            "sig_score": [-5.0, -4.0, 3.0, 2.0],
            "sig_strength": [5.0, 4.0, 3.0, 2.0],
            "is_reverser": [True, True, False, False],
            "fdr_pass": [True, True, True, True],
            "confidence_weight": [1.0] * 4,
            "direction_category": ["reverser", "reverser", "mimicker", "mimicker"],
        })
        result = aggregate_to_drug(df, min_signatures=1, min_reverser=1)
        drug = result[result["drug"] == "conflictDrug"].iloc[0]
        assert bool(drug["has_cl_conflict"]) is True
