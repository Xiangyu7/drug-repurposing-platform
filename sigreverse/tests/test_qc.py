"""Unit tests for sigreverse.qc module.

Tests cover:
    - Missing gene ratio calculation
    - Signature size checking
    - Signature-level QC summary
    - Toxicity flag heuristic
    - NaN/edge case handling
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.qc import (
    missing_gene_ratio,
    check_signature_size,
    signature_qc_summary,
    toxicity_flag_heuristic,
    apply_toxicity_flags,
)


# ===== Missing gene ratio =====

class TestMissingGeneRatio:
    def test_no_missing(self):
        assert missing_gene_ratio([], [], ["A", "B"], ["C", "D"]) == 0.0

    def test_all_missing(self):
        assert missing_gene_ratio(["A", "B"], ["C", "D"], ["A", "B"], ["C", "D"]) == 1.0

    def test_half_missing(self):
        assert missing_gene_ratio(["A"], ["C"], ["A", "B"], ["C", "D"]) == pytest.approx(0.5)

    def test_empty_lists(self):
        assert missing_gene_ratio([], [], [], []) == 0.0

    def test_clamped_to_one(self):
        """Even if calculation exceeds 1, should be clamped."""
        ratio = missing_gene_ratio(["A", "B", "C"], [], ["A"], [])
        assert ratio <= 1.0


# ===== Signature size check =====

class TestCheckSignatureSize:
    def test_optimal(self):
        result = check_signature_size(200, 200)
        assert result["status"] == "optimal"
        assert len(result["warnings"]) == 0

    def test_suboptimal(self):
        result = check_signature_size(100, 100)
        assert result["status"] == "suboptimal"
        assert len(result["warnings"]) == 1

    def test_below_minimum(self):
        result = check_signature_size(20, 20)
        assert result["status"] == "below_minimum"

    def test_empty(self):
        result = check_signature_size(0, 100)
        assert result["status"] == "empty"

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            check_signature_size(-1, 10)

    def test_custom_thresholds(self):
        result = check_signature_size(30, 30, min_recommended=20, optimal_min=50)
        assert result["status"] == "suboptimal"


# ===== Signature QC summary =====

class TestSignatureQCSummary:
    def test_empty_dataframe(self):
        df = pd.DataFrame()
        result = signature_qc_summary(df)
        assert result["n_signatures"] == 0
        assert result["status"] == "empty"

    def test_basic_summary(self):
        df = pd.DataFrame({
            "sig_score": [-5.0, -3.0, 0.0, 2.0],
            "fdr_pass": [True, True, False, True],
            "direction_category": ["reverser", "reverser", "orthogonal", "mimicker"],
        })
        result = signature_qc_summary(df)
        assert result["n_signatures"] == 4
        assert result["fdr_pass_rate"] == pytest.approx(0.75)
        assert result["n_fdr_pass"] == 3
        assert result["direction_distribution"]["reverser"] == 2

    def test_zero_score_fraction(self):
        df = pd.DataFrame({
            "sig_score": [0.0, 0.0, -3.0, 2.0],
        })
        result = signature_qc_summary(df)
        assert result["zero_score_fraction"] == pytest.approx(0.5)

    def test_nan_scores(self):
        df = pd.DataFrame({
            "sig_score": [np.nan, -3.0, np.nan, 2.0],
        })
        result = signature_qc_summary(df)
        assert result["n_signatures"] == 4
        assert "score_stats" in result
        assert result["score_stats"]["n_nan"] == 2

    def test_ldp3_agreement(self):
        df = pd.DataFrame({
            "ldp3_type_agree": [True, True, False, None],
        })
        result = signature_qc_summary(df)
        assert result["ldp3_type_agreement_rate"] == pytest.approx(2/3, abs=0.01)
        assert result["n_ldp3_disagree"] == 1


# ===== Toxicity flag heuristic =====

class TestToxicityFlag:
    def test_flagged(self):
        """Drug with many sigs, high reverser rate, strong effect → flagged."""
        assert toxicity_flag_heuristic(
            n_signatures=15,
            p_reverser=0.9,
            median_strength=30.0,
            cfg={"enabled": True, "min_signatures": 10, "min_p_reverser": 0.8, "min_median_strength": 25.0},
        ) is True

    def test_not_flagged_low_sigs(self):
        assert toxicity_flag_heuristic(
            n_signatures=5, p_reverser=0.9, median_strength=30.0,
            cfg={"enabled": True, "min_signatures": 10},
        ) is False

    def test_not_flagged_low_reverser(self):
        assert toxicity_flag_heuristic(
            n_signatures=15, p_reverser=0.5, median_strength=30.0,
            cfg={"enabled": True, "min_p_reverser": 0.8},
        ) is False

    def test_not_flagged_low_strength(self):
        assert toxicity_flag_heuristic(
            n_signatures=15, p_reverser=0.9, median_strength=10.0,
            cfg={"enabled": True, "min_median_strength": 25.0},
        ) is False

    def test_disabled(self):
        assert toxicity_flag_heuristic(
            n_signatures=100, p_reverser=1.0, median_strength=100.0,
            cfg={"enabled": False},
        ) is False

    def test_nan_strength(self):
        """NaN strength should not flag."""
        assert toxicity_flag_heuristic(
            n_signatures=15, p_reverser=0.9, median_strength=float("nan"),
            cfg={"enabled": True},
        ) is False

    def test_negative_sigs(self):
        """Negative n_signatures should not flag."""
        assert toxicity_flag_heuristic(
            n_signatures=-1, p_reverser=0.9, median_strength=30.0,
            cfg={"enabled": True},
        ) is False


# ===== Apply toxicity flags =====

class TestApplyToxicityFlags:
    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["drug", "p_reverser", "median_score"])
        result = apply_toxicity_flags(df, {"enabled": True})
        assert "possible_toxicity_confounder" in result.columns

    def test_adds_column(self):
        df = pd.DataFrame({
            "drug": ["A", "B"],
            "p_reverser": [0.9, 0.3],
            "median_score": [30.0, 5.0],
            "n_signatures_fdr_pass": [15, 15],
        })
        result = apply_toxicity_flags(df, {
            "enabled": True,
            "min_signatures": 10,
            "min_p_reverser": 0.8,
            "min_median_strength": 25.0,
        })
        assert bool(result.iloc[0]["possible_toxicity_confounder"]) is True
        assert bool(result.iloc[1]["possible_toxicity_confounder"]) is False
