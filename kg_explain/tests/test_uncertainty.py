"""Test suite for bootstrap CI uncertainty quantification.

Tests cover:
- bootstrap_ci: known distributions, edge cases, determinism, aggregation
- assign_confidence_tier: boundary conditions
- add_uncertainty_to_ranking: DataFrame integration, column validation, missing data
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from kg_explain.rankers.uncertainty import (
    add_uncertainty_to_ranking,
    assign_confidence_tier,
    bootstrap_ci,
)


# ---------------------------------------------------------------------------
# TestBootstrapCI
# ---------------------------------------------------------------------------
class TestBootstrapCI:
    """Tests for bootstrap_ci function."""

    def test_known_distribution_mean_within_bounds(self):
        """Bootstrap CI of a known uniform distribution should contain the true mean."""
        scores = [0.0, 0.25, 0.5, 0.75, 1.0]
        result = bootstrap_ci(scores, n_bootstrap=2000, seed=42)
        true_mean = 0.5
        assert result["ci_lower"] <= true_mean <= result["ci_upper"]
        assert result["mean"] == pytest.approx(true_mean, abs=1e-6)
        assert result["n_paths"] == 5

    def test_known_distribution_ci_width_positive(self):
        """CI width should be positive when there is variance in scores."""
        scores = [0.1, 0.5, 0.9]
        result = bootstrap_ci(scores, n_bootstrap=1000, seed=42)
        assert result["ci_width"] > 0.0

    def test_single_path_returns_zero_width(self):
        """A single path score should produce zero CI width."""
        result = bootstrap_ci([0.75], n_bootstrap=1000, seed=42)
        assert result["mean"] == pytest.approx(0.75, abs=1e-6)
        assert result["ci_lower"] == pytest.approx(0.75, abs=1e-6)
        assert result["ci_upper"] == pytest.approx(0.75, abs=1e-6)
        assert result["ci_width"] == 0.0
        assert result["n_paths"] == 1

    def test_zero_paths_returns_zeros(self):
        """Empty path list should return all zeros."""
        result = bootstrap_ci([], n_bootstrap=1000, seed=42)
        assert result["mean"] == 0.0
        assert result["ci_lower"] == 0.0
        assert result["ci_upper"] == 0.0
        assert result["ci_width"] == 0.0
        assert result["n_paths"] == 0

    def test_deterministic_with_same_seed(self):
        """Same seed should produce identical results."""
        scores = [0.2, 0.4, 0.6, 0.8]
        r1 = bootstrap_ci(scores, n_bootstrap=500, seed=123)
        r2 = bootstrap_ci(scores, n_bootstrap=500, seed=123)
        assert r1 == r2

    def test_different_seeds_may_differ(self):
        """Different seeds can produce different CI bounds."""
        scores = [0.1, 0.3, 0.5, 0.7, 0.9]
        r1 = bootstrap_ci(scores, n_bootstrap=500, seed=1)
        r2 = bootstrap_ci(scores, n_bootstrap=500, seed=999)
        # Mean is deterministic regardless of seed
        assert r1["mean"] == r2["mean"]
        # CI bounds may differ (not guaranteed but highly likely with different seeds)

    def test_custom_ci_level_90(self):
        """90% CI should be narrower than 95% CI for the same data."""
        scores = [0.1, 0.3, 0.5, 0.7, 0.9]
        ci95 = bootstrap_ci(scores, n_bootstrap=2000, ci=0.95, seed=42)
        ci90 = bootstrap_ci(scores, n_bootstrap=2000, ci=0.90, seed=42)
        assert ci90["ci_width"] <= ci95["ci_width"]

    def test_median_aggregation(self):
        """Median aggregation should use median as the point estimate."""
        scores = [0.1, 0.2, 0.9]  # median=0.2, mean=0.4
        result_mean = bootstrap_ci(scores, n_bootstrap=1000, seed=42, agg_fn="mean")
        result_median = bootstrap_ci(scores, n_bootstrap=1000, seed=42, agg_fn="median")
        assert result_mean["mean"] == pytest.approx(0.4, abs=1e-6)
        assert result_median["mean"] == pytest.approx(0.2, abs=1e-6)

    def test_identical_scores_zero_width(self):
        """Identical scores should produce zero CI width."""
        scores = [0.5, 0.5, 0.5, 0.5]
        result = bootstrap_ci(scores, n_bootstrap=1000, seed=42)
        assert result["mean"] == pytest.approx(0.5, abs=1e-6)
        assert result["ci_width"] == pytest.approx(0.0, abs=1e-6)

    def test_ci_lower_leq_mean_leq_ci_upper(self):
        """CI lower bound should be <= mean <= CI upper bound."""
        scores = [0.1, 0.3, 0.5, 0.7, 0.9, 0.2, 0.8]
        result = bootstrap_ci(scores, n_bootstrap=2000, seed=42)
        assert result["ci_lower"] <= result["mean"] <= result["ci_upper"]

    def test_two_paths(self):
        """Two paths should still produce valid CI."""
        scores = [0.2, 0.8]
        result = bootstrap_ci(scores, n_bootstrap=1000, seed=42)
        assert result["n_paths"] == 2
        assert result["ci_width"] >= 0.0
        assert result["ci_lower"] <= result["ci_upper"]


# ---------------------------------------------------------------------------
# TestConfidenceTier
# ---------------------------------------------------------------------------
class TestConfidenceTier:
    """Tests for assign_confidence_tier function."""

    def test_high_confidence_with_enough_paths(self):
        """CI width < 0.10 with 3+ paths should be HIGH."""
        assert assign_confidence_tier(0.05, n_paths=5) == "HIGH"
        assert assign_confidence_tier(0.0, n_paths=3) == "HIGH"
        assert assign_confidence_tier(0.099, n_paths=10) == "HIGH"

    def test_medium_confidence_with_enough_paths(self):
        """CI width >= 0.10 and < 0.25 with 3+ paths should be MEDIUM."""
        assert assign_confidence_tier(0.10, n_paths=3) == "MEDIUM"
        assert assign_confidence_tier(0.15, n_paths=5) == "MEDIUM"
        assert assign_confidence_tier(0.249, n_paths=4) == "MEDIUM"

    def test_low_confidence_wide_ci(self):
        """CI width >= 0.25 should be LOW regardless of n_paths."""
        assert assign_confidence_tier(0.25, n_paths=10) == "LOW"
        assert assign_confidence_tier(0.5, n_paths=5) == "LOW"
        assert assign_confidence_tier(1.0, n_paths=3) == "LOW"

    def test_single_path_always_low(self):
        """Single path should always be LOW, even with zero CI width."""
        assert assign_confidence_tier(0.0, n_paths=1) == "LOW"
        assert assign_confidence_tier(0.0, n_paths=0) == "LOW"

    def test_two_paths_capped_at_medium(self):
        """Two paths should cap at MEDIUM even with narrow CI."""
        assert assign_confidence_tier(0.0, n_paths=2) == "MEDIUM"
        assert assign_confidence_tier(0.05, n_paths=2) == "MEDIUM"
        assert assign_confidence_tier(0.30, n_paths=2) == "LOW"

    def test_exact_boundary_high_medium(self):
        """Exact boundary 0.10 with enough paths: should be MEDIUM (not HIGH)."""
        assert assign_confidence_tier(0.10, n_paths=5) == "MEDIUM"

    def test_exact_boundary_medium_low(self):
        """Exact boundary 0.25 with enough paths: should be LOW (not MEDIUM)."""
        assert assign_confidence_tier(0.25, n_paths=5) == "LOW"

    def test_legacy_no_n_paths_defaults_to_low(self):
        """Calling without n_paths (default=0) should return LOW."""
        assert assign_confidence_tier(0.0) == "LOW"
        assert assign_confidence_tier(0.05) == "LOW"


# ---------------------------------------------------------------------------
# TestAddUncertaintyToRanking
# ---------------------------------------------------------------------------
class TestAddUncertaintyToRanking:
    """Tests for add_uncertainty_to_ranking DataFrame integration."""

    @pytest.fixture
    def sample_rank_df(self):
        return pd.DataFrame({
            "drug_normalized": ["aspirin", "metformin", "atorvastatin"],
            "diseaseId": ["EFO_0000378", "EFO_0000378", "EFO_0000378"],
            "final_score": [0.85, 0.72, 0.60],
        })

    @pytest.fixture
    def sample_evidence_paths(self):
        return [
            {"drug": "aspirin", "diseaseId": "EFO_0000378", "path_score": 0.9},
            {"drug": "aspirin", "diseaseId": "EFO_0000378", "path_score": 0.8},
            {"drug": "aspirin", "diseaseId": "EFO_0000378", "path_score": 0.7},
            {"drug": "aspirin", "diseaseId": "EFO_0000378", "path_score": 0.85},
            {"drug": "metformin", "diseaseId": "EFO_0000378", "path_score": 0.6},
            {"drug": "metformin", "diseaseId": "EFO_0000378", "path_score": 0.5},
            {"drug": "metformin", "diseaseId": "EFO_0000378", "path_score": 0.7},
        ]

    def test_output_columns_present(self, sample_rank_df, sample_evidence_paths):
        """Result DataFrame should have all expected CI columns."""
        result = add_uncertainty_to_ranking(sample_rank_df, sample_evidence_paths)
        expected_cols = {"ci_lower", "ci_upper", "ci_width", "confidence_tier", "n_evidence_paths"}
        assert expected_cols.issubset(set(result.columns))

    def test_row_count_preserved(self, sample_rank_df, sample_evidence_paths):
        """Number of rows should match the input ranking DataFrame."""
        result = add_uncertainty_to_ranking(sample_rank_df, sample_evidence_paths)
        assert len(result) == len(sample_rank_df)

    def test_n_evidence_paths_correct(self, sample_rank_df, sample_evidence_paths):
        """n_evidence_paths should match the number of paths per pair."""
        result = add_uncertainty_to_ranking(sample_rank_df, sample_evidence_paths)
        aspirin_row = result[result["drug_normalized"] == "aspirin"].iloc[0]
        metformin_row = result[result["drug_normalized"] == "metformin"].iloc[0]
        assert aspirin_row["n_evidence_paths"] == 4
        assert metformin_row["n_evidence_paths"] == 3

    def test_missing_pair_filled_with_defaults(self, sample_rank_df, sample_evidence_paths):
        """Drug with no evidence paths should get default LOW/0 values."""
        result = add_uncertainty_to_ranking(sample_rank_df, sample_evidence_paths)
        atorvastatin_row = result[result["drug_normalized"] == "atorvastatin"].iloc[0]
        assert atorvastatin_row["ci_lower"] == 0.0
        assert atorvastatin_row["ci_upper"] == 0.0
        assert atorvastatin_row["ci_width"] == 0.0
        assert atorvastatin_row["confidence_tier"] == "LOW"
        assert atorvastatin_row["n_evidence_paths"] == 0

    def test_empty_evidence_paths(self, sample_rank_df):
        """Empty evidence paths list should return defaults for all rows."""
        result = add_uncertainty_to_ranking(sample_rank_df, [])
        assert (result["ci_width"] == 0.0).all()
        assert (result["confidence_tier"] == "LOW").all()
        assert (result["n_evidence_paths"] == 0).all()

    def test_case_insensitive_drug_matching(self):
        """Drug names should be matched case-insensitively."""
        rank_df = pd.DataFrame({
            "drug_normalized": ["aspirin"],
            "diseaseId": ["EFO_001"],
            "final_score": [0.5],
        })
        paths = [
            {"drug": "ASPIRIN", "diseaseId": "EFO_001", "path_score": 0.6},
            {"drug": "Aspirin", "diseaseId": "EFO_001", "path_score": 0.7},
        ]
        result = add_uncertainty_to_ranking(rank_df, paths)
        assert result.iloc[0]["n_evidence_paths"] == 2

    def test_invalid_path_score_skipped(self):
        """Paths with non-numeric path_score should be silently skipped."""
        rank_df = pd.DataFrame({
            "drug_normalized": ["aspirin"],
            "diseaseId": ["EFO_001"],
            "final_score": [0.5],
        })
        paths = [
            {"drug": "aspirin", "diseaseId": "EFO_001", "path_score": 0.6},
            {"drug": "aspirin", "diseaseId": "EFO_001", "path_score": "not_a_number"},
            {"drug": "aspirin", "diseaseId": "EFO_001", "path_score": None},
        ]
        result = add_uncertainty_to_ranking(rank_df, paths)
        # Only the first path with valid score=0.6 should be counted
        assert result.iloc[0]["n_evidence_paths"] == 1
        # Single valid path → LOW confidence (not HIGH)
        assert result.iloc[0]["confidence_tier"] == "LOW"

    def test_single_path_pair_gets_low_confidence(self):
        """A drug-disease pair with only 1 evidence path should be LOW confidence."""
        rank_df = pd.DataFrame({
            "drug_normalized": ["aspirin"],
            "diseaseId": ["EFO_001"],
            "final_score": [0.9],
        })
        paths = [
            {"drug": "aspirin", "diseaseId": "EFO_001", "path_score": 0.9},
        ]
        result = add_uncertainty_to_ranking(rank_df, paths)
        assert result.iloc[0]["confidence_tier"] == "LOW"
        assert result.iloc[0]["ci_width"] == 0.0
        assert result.iloc[0]["n_evidence_paths"] == 1

    def test_original_columns_preserved(self, sample_rank_df, sample_evidence_paths):
        """Original columns from rank_df should still be present."""
        result = add_uncertainty_to_ranking(sample_rank_df, sample_evidence_paths)
        assert "final_score" in result.columns
        assert "drug_normalized" in result.columns
        assert "diseaseId" in result.columns
