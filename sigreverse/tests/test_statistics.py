"""Unit tests for sigreverse.statistics module.

Tests cover:
    - Permutation null distribution
    - Empirical p-value
    - Benjamini-Hochberg FDR
    - Bootstrap confidence interval
    - Effect size normalization
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.statistics import (
    permutation_null_distribution,
    compute_empirical_pvalue,
    benjamini_hochberg,
    bootstrap_confidence_interval,
    normalize_effect_size,
)


# ===== Empirical p-value =====

class TestEmpiricalPValue:
    def test_extreme_observed(self):
        """Observed far below null → very small p-value."""
        null = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        pval = compute_empirical_pvalue(-10.0, null)
        assert pval == pytest.approx(1.0 / 6.0)  # (0+1)/(5+1)

    def test_observed_in_null(self):
        """Observed equals median of null → p ≈ 0.5."""
        null = np.linspace(-5, 5, 1000)
        pval = compute_empirical_pvalue(0.0, null)
        assert 0.4 < pval < 0.6

    def test_observed_above_null(self):
        """Observed above all null → p ≈ 1.0."""
        null = np.array([-5.0, -4.0, -3.0, -2.0, -1.0])
        pval = compute_empirical_pvalue(10.0, null)
        assert pval == pytest.approx(6.0 / 6.0)  # all null <= 10 → (5+1)/(5+1)

    def test_pseudocount_prevents_zero(self):
        """P-value should never be exactly 0."""
        null = np.array([1.0, 2.0, 3.0])
        pval = compute_empirical_pvalue(-100.0, null)
        assert pval > 0


# ===== BH-FDR =====

class TestBenjaminiHochberg:
    def test_single_pvalue(self):
        result = benjamini_hochberg(np.array([0.05]))
        assert result[0] == pytest.approx(0.05)

    def test_all_significant(self):
        pvals = np.array([0.001, 0.002, 0.003])
        adjusted = benjamini_hochberg(pvals)
        # All should still be < 0.05
        assert all(q < 0.05 for q in adjusted)

    def test_monotonicity(self):
        """Adjusted p-values should respect original ordering."""
        pvals = np.array([0.01, 0.04, 0.03, 0.8])
        adjusted = benjamini_hochberg(pvals)
        # Smallest raw p should have smallest adjusted p
        assert adjusted[0] <= adjusted[1]

    def test_clipped_to_one(self):
        pvals = np.array([0.99, 0.999])
        adjusted = benjamini_hochberg(pvals)
        assert all(q <= 1.0 for q in adjusted)

    def test_empty(self):
        result = benjamini_hochberg(np.array([]))
        assert len(result) == 0


# ===== Bootstrap CI =====

class TestBootstrapCI:
    def test_tight_data(self):
        """All same values → CI should be a single point."""
        values = np.array([5.0, 5.0, 5.0, 5.0])
        lo, hi = bootstrap_confidence_interval(values, n_bootstrap=500, seed=42)
        assert lo == pytest.approx(5.0)
        assert hi == pytest.approx(5.0)

    def test_wide_data(self):
        """Wide spread → CI should be wide."""
        rng = np.random.default_rng(42)
        values = rng.normal(0, 10, 100)
        lo, hi = bootstrap_confidence_interval(values, n_bootstrap=1000, seed=42)
        assert lo < 0
        assert hi > lo

    def test_negative_data(self):
        """All negative → CI should be negative."""
        values = np.array([-10.0, -8.0, -12.0, -9.0, -11.0])
        lo, hi = bootstrap_confidence_interval(values, n_bootstrap=1000, seed=42)
        assert hi < 0  # entire CI below zero

    def test_single_value(self):
        values = np.array([3.14])
        lo, hi = bootstrap_confidence_interval(values, seed=42)
        assert lo == pytest.approx(3.14)
        assert hi == pytest.approx(3.14)


# ===== Effect size normalization =====

class TestNormalizeEffectSize:
    def test_zero_when_equal_to_mean(self):
        null = np.array([0.0, 0.0, 0.0])
        z = normalize_effect_size(0.0, null)
        assert z == 0.0  # std is 0, returns 0

    def test_negative_z_for_reverser(self):
        """Observed << mean_null → large negative z."""
        null = np.random.default_rng(42).normal(0, 1, 1000)
        z = normalize_effect_size(-5.0, null)
        assert z < -3.0  # well below null

    def test_positive_z_for_mimicker(self):
        null = np.random.default_rng(42).normal(0, 1, 1000)
        z = normalize_effect_size(5.0, null)
        assert z > 3.0


# ===== Permutation null distribution =====

class TestPermutationNullDistribution:
    def _make_df(self):
        """Create a simple test DataFrame."""
        return pd.DataFrame({
            "meta.pert_name": ["drugA"] * 5 + ["drugB"] * 5,
            "sig_score": [-3, -2, -4, -1, -5, 1, 2, 0, 3, -1],
        })

    def test_returns_correct_drugs(self):
        df = self._make_df()
        null = permutation_null_distribution(df, n_permutations=100, seed=42)
        assert "drugA" in null
        assert "drugB" in null

    def test_correct_number_of_permutations(self):
        df = self._make_df()
        null = permutation_null_distribution(df, n_permutations=200, seed=42)
        assert len(null["drugA"]) == 200

    def test_null_centered_near_overall_median(self):
        """Null distribution should be centered near the overall median score."""
        df = self._make_df()
        null = permutation_null_distribution(df, n_permutations=1000, seed=42)
        overall_median = df["sig_score"].median()
        # Each drug's null mean should be near overall median
        for drug in ["drugA", "drugB"]:
            null_mean = np.mean(null[drug])
            assert abs(null_mean - overall_median) < 2.0
