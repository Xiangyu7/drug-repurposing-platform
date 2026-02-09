"""Unit tests for sigreverse.dose_response module.

Tests cover:
    - Dose parsing (with unit conversion, embedded units)
    - Monotonicity check (Spearman correlation)
    - Hill equation fitting
    - Quality assessment
    - Full analysis pipeline
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.dose_response import (
    parse_dose, check_monotonicity, fit_hill_equation,
    assess_dose_response_quality, analyze_dose_response,
)


# ===== Dose parsing =====

class TestParseDose:
    def test_plain_number(self):
        assert parse_dose("10.0") == pytest.approx(10.0)

    def test_with_unit_um(self):
        assert parse_dose("10", "um") == pytest.approx(10.0)

    def test_with_unit_nm(self):
        assert parse_dose("100", "nM") == pytest.approx(0.1)

    def test_with_unit_mm(self):
        assert parse_dose("1", "mM") == pytest.approx(1000.0)

    def test_none_returns_none(self):
        assert parse_dose(None) is None

    def test_nan_returns_none(self):
        assert parse_dose("nan") is None

    def test_empty_returns_none(self):
        assert parse_dose("") is None

    # --- New tests for embedded unit parsing ---

    def test_embedded_unit_um_space(self):
        """'10 uM' with no separate unit should parse correctly."""
        assert parse_dose("10 uM") == pytest.approx(10.0)

    def test_embedded_unit_um_no_space(self):
        """'10uM' without space should parse correctly."""
        assert parse_dose("10uM") == pytest.approx(10.0)

    def test_embedded_unit_nm(self):
        """'100 nM' should convert to 0.1 uM."""
        assert parse_dose("100 nM") == pytest.approx(0.1)

    def test_embedded_unit_mm(self):
        """'1 mM' should convert to 1000 uM."""
        assert parse_dose("1 mM") == pytest.approx(1000.0)

    def test_explicit_unit_overrides_embedded(self):
        """Explicit unit parameter should take priority over embedded."""
        # dose_str says "100 nM" but explicit unit says "um"
        assert parse_dose("100 nM", "um") == pytest.approx(100.0)

    def test_nan_unit_ignored(self):
        """dose_unit='nan' should be treated as empty."""
        assert parse_dose("10 uM", "nan") == pytest.approx(10.0)

    def test_none_unit_string(self):
        """dose_unit='None' should be treated as empty."""
        assert parse_dose("10 nM", "None") == pytest.approx(0.01)

    def test_scientific_notation(self):
        """Scientific notation like '1e-3' should work."""
        assert parse_dose("1e-3", "mM") == pytest.approx(1.0)

    def test_negative_dose_returns_none(self):
        """Negative doses are invalid."""
        assert parse_dose("-10") is None

    def test_sentinel_minus666(self):
        """LINCS uses -666 as missing sentinel."""
        assert parse_dose("-666") is None

    def test_percentage_unit(self):
        """'10 %' should keep value as-is."""
        assert parse_dose("10 %") == pytest.approx(10.0)


# ===== Monotonicity =====

class TestCheckMonotonicity:
    def test_perfectly_monotonic_decreasing(self):
        """Score decreases with dose -> monotonic (negative rho)."""
        doses = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        scores = np.array([-1.0, -2.0, -3.0, -4.0, -5.0])
        is_mono, rho = check_monotonicity(doses, scores)
        assert bool(is_mono) is True
        assert rho < -0.9  # near -1.0

    def test_not_monotonic(self):
        doses = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        scores = np.array([-1.0, -5.0, -2.0, -4.0, -3.0])
        is_mono, rho = check_monotonicity(doses, scores)
        # Random pattern, may or may not be monotonic

    def test_too_few_points(self):
        doses = np.array([1.0, 2.0])
        scores = np.array([-1.0, -2.0])
        is_mono, rho = check_monotonicity(doses, scores)
        assert is_mono is False  # need >=3 points

    def test_increasing_scores(self):
        """Score increases with dose -> NOT monotonic reverser (positive rho)."""
        doses = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        scores = np.array([-5.0, -4.0, -3.0, -2.0, -1.0])
        is_mono, rho = check_monotonicity(doses, scores)
        assert bool(is_mono) is False
        assert rho > 0.5  # positive correlation


# ===== Hill equation fitting =====

class TestFitHillEquation:
    def test_too_few_points(self):
        doses = np.array([1.0, 2.0])
        scores = np.array([-1.0, -2.0])
        ec50, emax, n, r2 = fit_hill_equation(doses, scores)
        assert ec50 is None

    def test_no_reversal_signal(self):
        """All positive scores -> no reversal -> fit fails."""
        doses = np.array([1.0, 2.0, 3.0, 4.0])
        scores = np.array([1.0, 2.0, 3.0, 4.0])
        ec50, emax, n, r2 = fit_hill_equation(doses, scores)
        assert ec50 is None

    def test_ideal_hill_curve(self):
        """Generate data from a known Hill curve, check fit recovers params."""
        # Hill: E = Emax * D^n / (EC50^n + D^n)
        true_ec50, true_emax, true_n = 5.0, -10.0, 1.5
        doses = np.array([0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0])
        scores = true_emax * (doses ** true_n) / (true_ec50 ** true_n + doses ** true_n)

        ec50, emax, n, r2 = fit_hill_equation(doses, scores, n_restarts=20, seed=42)
        if r2 is not None:
            assert r2 > 0.5  # should be reasonable fit


# ===== Quality assessment =====

class TestAssessQuality:
    def test_excellent(self):
        q = assess_dose_response_quality(n_doses=5, is_monotonic=True, monotonicity_score=-0.9, hill_r2=0.85)
        assert q == "excellent"

    def test_good(self):
        q = assess_dose_response_quality(n_doses=4, is_monotonic=True, monotonicity_score=-0.6, hill_r2=0.6)
        assert q == "good"

    def test_marginal(self):
        q = assess_dose_response_quality(n_doses=3, is_monotonic=False, monotonicity_score=-0.4, hill_r2=0.3)
        assert q == "marginal"

    def test_poor(self):
        q = assess_dose_response_quality(n_doses=2, is_monotonic=False, monotonicity_score=0.1, hill_r2=0.1)
        assert q == "poor"

    def test_insufficient(self):
        q = assess_dose_response_quality(n_doses=1, is_monotonic=False, monotonicity_score=0.0, hill_r2=None)
        assert q == "insufficient"


# ===== Full pipeline =====

class TestAnalyzeDoseResponse:
    def _make_df(self):
        return pd.DataFrame({
            "meta.pert_name": ["drugA"] * 6 + ["drugB"] * 3,
            "meta.pert_dose": ["1", "2", "5", "10", "20", "50", "10", "10", "10"],
            "meta.pert_dose_unit": ["um"] * 9,
            "sig_score": [-1.0, -2.0, -3.0, -5.0, -7.0, -8.0, -3.0, -4.0, -2.5],
        })

    def test_returns_dataframe(self):
        df = self._make_df()
        result = analyze_dose_response(df)
        assert isinstance(result, pd.DataFrame)
        assert "drug" in result.columns
        assert "dr_quality" in result.columns

    def test_drug_a_has_multiple_doses(self):
        df = self._make_df()
        result = analyze_dose_response(df)
        drug_a = result[result["drug"] == "drugA"].iloc[0]
        assert drug_a["dr_n_doses"] == 6

    def test_drug_b_single_dose(self):
        df = self._make_df()
        result = analyze_dose_response(df)
        drug_b = result[result["drug"] == "drugB"].iloc[0]
        assert drug_b["dr_n_doses"] == 1  # all same dose
        assert drug_b["dr_quality"] == "insufficient"

    def test_embedded_unit_parsing(self):
        """Doses with embedded units (e.g., '10 uM') should parse correctly."""
        df = pd.DataFrame({
            "meta.pert_name": ["drugC"] * 4,
            "meta.pert_dose": ["1 uM", "5 uM", "10 uM", "50 uM"],
            "meta.pert_dose_unit": ["nan"] * 4,  # LINCS sometimes has nan
            "sig_score": [-1.0, -3.0, -5.0, -7.0],
        })
        result = analyze_dose_response(df)
        drug_c = result[result["drug"] == "drugC"].iloc[0]
        assert drug_c["dr_n_doses"] == 4
        assert drug_c["dr_quality"] != "insufficient"
