"""Tests for 07_pathway_meta.py core functions."""
import sys
from pathlib import Path
import numpy as np
import pytest

# Add scripts to path so we can import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from importlib import import_module

# Import the module (filename starts with number, can't import directly)
import importlib.util
spec = importlib.util.spec_from_file_location(
    "pathway_meta",
    Path(__file__).resolve().parent.parent / "scripts" / "07_pathway_meta.py"
)
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

stouffer_signed = pm.stouffer_signed
bh_fdr = pm.bh_fdr


# =====================================================================
# stouffer_signed tests
# =====================================================================
class TestStoufferSigned:
    def test_basic_positive(self):
        """Two studies both positive → positive combined z."""
        pvals = np.array([0.01, 0.01])
        signs = np.array([1.0, 1.0])
        z, p = stouffer_signed(pvals, signs)
        assert z > 0
        assert 0 < p < 0.05

    def test_basic_negative(self):
        """Two studies both negative → negative combined z."""
        pvals = np.array([0.01, 0.01])
        signs = np.array([-1.0, -1.0])
        z, p = stouffer_signed(pvals, signs)
        assert z < 0
        assert 0 < p < 0.05

    def test_conflicting_signs(self):
        """Conflicting directions should cancel out → larger p."""
        pvals = np.array([0.01, 0.01])
        signs = np.array([1.0, -1.0])
        z, p = stouffer_signed(pvals, signs)
        assert abs(z) < 1  # should be near 0
        assert p > 0.3  # not significant

    def test_very_small_pvals(self):
        """Extremely small p-values should not produce NaN."""
        pvals = np.array([1e-320, 1e-320])  # smaller than 1e-300
        signs = np.array([1.0, 1.0])
        z, p = stouffer_signed(pvals, signs)
        assert np.isfinite(z)
        assert np.isfinite(p)

    def test_pval_one(self):
        """p=1.0 should give z near 0."""
        pvals = np.array([1.0, 1.0])
        signs = np.array([1.0, 1.0])
        z, p = stouffer_signed(pvals, signs)
        assert abs(z) < 0.1

    def test_single_study(self):
        """Single study should return NaN (need >= 2)."""
        pvals = np.array([0.01])
        signs = np.array([1.0])
        z, p = stouffer_signed(pvals, signs)
        assert np.isnan(z)
        assert np.isnan(p)

    def test_empty(self):
        """Empty arrays should return NaN."""
        pvals = np.array([])
        signs = np.array([])
        z, p = stouffer_signed(pvals, signs)
        assert np.isnan(z)

    def test_with_weights(self):
        """Weighted Stouffer should give higher z to higher-weighted study."""
        pvals = np.array([0.001, 0.5])
        signs = np.array([1.0, 1.0])
        # Heavy weight on first (very significant) study
        z_heavy, _ = stouffer_signed(pvals, signs, weights=np.array([10.0, 1.0]))
        # Equal weights
        z_equal, _ = stouffer_signed(pvals, signs, weights=np.array([1.0, 1.0]))
        assert z_heavy > z_equal

    def test_nan_pvals(self):
        """NaN p-values should be handled gracefully."""
        pvals = np.array([np.nan, 0.01, 0.01])
        signs = np.array([1.0, 1.0, 1.0])
        z, p = stouffer_signed(pvals, signs)
        # Should still work with 2 valid values
        assert np.isfinite(z)


# =====================================================================
# bh_fdr tests
# =====================================================================
class TestBHFDR:
    def test_basic(self):
        """Basic FDR correction should inflate p-values."""
        p = np.array([0.01, 0.02, 0.03, 0.5])
        q = bh_fdr(p)
        assert len(q) == 4
        # FDR values should be >= original p-values
        assert np.all(q >= p - 1e-10)
        # Should be in [0, 1]
        assert np.all(q >= 0) and np.all(q <= 1)

    def test_monotonicity(self):
        """FDR should be monotonically non-decreasing when input is sorted."""
        p = np.sort(np.array([0.001, 0.01, 0.05, 0.1, 0.5]))
        q = bh_fdr(p)
        # When input is sorted, output should also be non-decreasing
        assert np.all(np.diff(q) >= -1e-10)

    def test_all_significant(self):
        """All very small p-values should remain significant after FDR."""
        p = np.array([0.001, 0.002, 0.003])
        q = bh_fdr(p)
        assert np.all(q < 0.05)

    def test_all_nonsignificant(self):
        """Large p-values should remain large after FDR."""
        p = np.array([0.8, 0.9, 0.95])
        q = bh_fdr(p)
        assert np.all(q >= 0.8)

    def test_empty(self):
        """Empty input should return empty output."""
        q = bh_fdr(np.array([]))
        assert len(q) == 0

    def test_single(self):
        """Single p-value: FDR should equal the p-value itself."""
        p = np.array([0.05])
        q = bh_fdr(p)
        assert np.isclose(q[0], 0.05)

    def test_unsorted_input(self):
        """Should work correctly with unsorted input."""
        p = np.array([0.5, 0.01, 0.1, 0.001])
        q = bh_fdr(p)
        # The smallest p-value should have the smallest FDR
        assert q[3] <= q[1] <= q[2] <= q[0]

    def test_clipped_to_one(self):
        """FDR values should never exceed 1.0."""
        p = np.array([0.9, 0.95, 0.99])
        q = bh_fdr(p)
        assert np.all(q <= 1.0)

    def test_consistency_with_r(self):
        """Known result: matches R's p.adjust(method='BH')."""
        # R: p.adjust(c(0.01, 0.04, 0.03, 0.005), method="BH")
        # Returns: 0.02 0.04 0.04 0.02
        p = np.array([0.01, 0.04, 0.03, 0.005])
        q = bh_fdr(p)
        expected = np.array([0.02, 0.04, 0.04, 0.02])
        np.testing.assert_allclose(q, expected, atol=1e-10)


# =====================================================================
# Config validation tests (via run.py)
# =====================================================================
class TestConfigValidation:
    """Test config validation from run.py"""

    @pytest.fixture
    def validate_config(self):
        spec_run = importlib.util.spec_from_file_location(
            "run",
            Path(__file__).resolve().parent.parent / "run.py"
        )
        run_mod = importlib.util.module_from_spec(spec_run)
        spec_run.loader.exec_module(run_mod)
        return run_mod.validate_config

    def test_valid_config(self, validate_config):
        cfg = {
            "project": {"name": "test", "outdir": "out", "workdir": "work", "seed": 42},
            "geo": {"gse_list": ["GSE12345"]},
            "labeling": {"mode": "regex", "regex_rules": {"GSE12345": {"case": {"any": ["x"]}, "control": {"any": ["y"]}}}},
            "de": {"method": "limma"},
            "meta": {"top_n": 300, "min_sign_concordance": 0.7},
        }
        # Should not raise
        validate_config(cfg)

    def test_missing_project(self, validate_config):
        cfg = {"geo": {"gse_list": ["GSE12345"]}, "labeling": {"mode": "regex"},
               "de": {}, "meta": {}}
        with pytest.raises(SystemExit):
            validate_config(cfg)

    def test_invalid_gse(self, validate_config):
        cfg = {
            "project": {"name": "t", "outdir": "o", "workdir": "w", "seed": 1},
            "geo": {"gse_list": ["NOTGSE"]},
            "labeling": {"mode": "regex", "regex_rules": {"NOTGSE": {}}},
            "de": {}, "meta": {},
        }
        with pytest.raises(SystemExit):
            validate_config(cfg)

    def test_negative_seed(self, validate_config):
        cfg = {
            "project": {"name": "t", "outdir": "o", "workdir": "w", "seed": -1},
            "geo": {"gse_list": ["GSE00001"]},
            "labeling": {"mode": "regex", "regex_rules": {"GSE00001": {}}},
            "de": {}, "meta": {},
        }
        with pytest.raises(SystemExit):
            validate_config(cfg)

    def test_invalid_min_sign(self, validate_config):
        cfg = {
            "project": {"name": "t", "outdir": "o", "workdir": "w", "seed": 1},
            "geo": {"gse_list": ["GSE00001"]},
            "labeling": {"mode": "regex", "regex_rules": {"GSE00001": {}}},
            "de": {}, "meta": {"min_sign_concordance": 1.5},
        }
        with pytest.raises(SystemExit):
            validate_config(cfg)

    def test_low_nperm(self, validate_config):
        cfg = {
            "project": {"name": "t", "outdir": "o", "workdir": "w", "seed": 1},
            "geo": {"gse_list": ["GSE00001"]},
            "labeling": {"mode": "regex", "regex_rules": {"GSE00001": {}}},
            "de": {}, "meta": {},
            "gsea": {"nperm": 10},
        }
        with pytest.raises(SystemExit):
            validate_config(cfg)
