"""Unit tests for sigreverse.cmap_algorithms module.

Tests cover:
    - LDP3ESProvider enrichment extraction
    - Stage 2: WTCS computation (LDP3 same-sign convention)
    - Stage 3: NCS normalization (cell_line_null, global_null, none)
    - Stage 4: Tau computation and aggregation
    - Bootstrap and leave-one-out reference modes
    - Full CMapPipeline end-to-end
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.cmap_algorithms import (
    LDP3ESProvider, EnrichmentResult, WTCSResult, NCSResult,
    compute_wtcs, compute_ncs, compute_tau,
    CMapPipeline, _quantile_max,
    build_bootstrap_reference, build_leave_one_out_reference,
)


# ===== Test data helpers =====

def _make_df_detail():
    """Create a mock df_detail with LDP3 output columns."""
    return pd.DataFrame({
        "uuid": [f"sig_{i}" for i in range(10)],
        "z-up": [-3.0, -2.0, -4.0, 1.0, 2.0, -1.5, -3.5, 0.5, -2.5, 3.0],
        "z-down": [-2.0, -1.5, -3.0, 0.5, 1.5, -0.5, -2.5, -0.3, -1.0, 2.0],
        "meta.cell_line": ["HUVEC"]*3 + ["MCF7"]*3 + ["A549"]*2 + ["HUVEC"]*2,
        "meta.pert_name": ["drugA"]*3 + ["drugB"]*3 + ["drugA"]*2 + ["drugC"]*2,
        "meta.pert_dose": ["10"]*10,
        "meta.pert_time": ["24 h"]*10,
        "fdr-up": [0.01]*10,
        "fdr-down": [0.02]*10,
        "logp-fisher": [5.0]*10,
        "type": ["reversers"]*5 + ["mimickers"]*5,
    })


def _make_ncs_list():
    """Create a standard NCS result list for testing."""
    return [
        NCSResult(sig_id="s1", ncs=-2.0, cell_line="CL1", pert_name="drugA"),
        NCSResult(sig_id="s2", ncs=-1.5, cell_line="CL2", pert_name="drugA"),
        NCSResult(sig_id="s3", ncs=1.0, cell_line="CL1", pert_name="drugB"),
        NCSResult(sig_id="s4", ncs=0.5, cell_line="CL2", pert_name="drugB"),
        NCSResult(sig_id="s5", ncs=-0.5, cell_line="CL1", pert_name="drugC"),
        NCSResult(sig_id="s6", ncs=-0.8, cell_line="CL2", pert_name="drugC"),
        NCSResult(sig_id="s7", ncs=-3.0, cell_line="CL1", pert_name="drugD"),
        NCSResult(sig_id="s8", ncs=-2.8, cell_line="CL2", pert_name="drugD"),
        NCSResult(sig_id="s9", ncs=0.2, cell_line="CL1", pert_name="drugE"),
        NCSResult(sig_id="s10", ncs=-0.1, cell_line="CL2", pert_name="drugE"),
    ]


# ===== LDP3ESProvider =====

class TestLDP3ESProvider:
    def test_extraction(self):
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        results = provider.get_enrichment_scores()
        assert len(results) == 10
        assert results[0].sig_id == "sig_0"
        assert results[0].es_up == -3.0
        assert results[0].es_down == -2.0
        assert results[0].cell_line == "HUVEC"
        assert results[0].pert_name == "drugA"

    def test_source_name(self):
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        assert provider.source_name() == "LDP3_API_z-scores"


# ===== Stage 2: WTCS =====

class TestComputeWTCS:
    def test_reverser_both_negative(self):
        """Both negative -> same sign -> coherent -> WTCS = (es_up + es_down)/2"""
        enrichments = [EnrichmentResult(sig_id="s1", es_up=-4.0, es_down=-3.0)]
        results = compute_wtcs(enrichments)
        assert len(results) == 1
        assert results[0].is_coherent is True
        assert results[0].direction == "reverser"
        assert results[0].wtcs == pytest.approx(-3.5)

    def test_mimicker_both_positive(self):
        enrichments = [EnrichmentResult(sig_id="s1", es_up=4.0, es_down=3.0)]
        results = compute_wtcs(enrichments)
        assert results[0].is_coherent is True
        assert results[0].direction == "mimicker"
        assert results[0].wtcs == pytest.approx(3.5)

    def test_opposing_signs_incoherent(self):
        enrichments = [EnrichmentResult(sig_id="s1", es_up=-4.0, es_down=3.0)]
        results = compute_wtcs(enrichments)
        assert results[0].is_coherent is False
        assert results[0].direction == "partial"
        assert results[0].wtcs == 0.0

    def test_zeros_incoherent(self):
        enrichments = [EnrichmentResult(sig_id="s1", es_up=0.0, es_down=0.0)]
        results = compute_wtcs(enrichments)
        assert results[0].is_coherent is False
        assert results[0].direction == "orthogonal"


# ===== Stage 3: NCS =====

class TestComputeNCS:
    def _make_enrichments_and_wtcs(self):
        enrichments = [
            EnrichmentResult(sig_id="s1", es_up=-4, es_down=-3, cell_line="CL1", pert_name="drugA"),
            EnrichmentResult(sig_id="s2", es_up=-2, es_down=-1, cell_line="CL1", pert_name="drugA"),
            EnrichmentResult(sig_id="s3", es_up=-6, es_down=-5, cell_line="CL2", pert_name="drugA"),
            EnrichmentResult(sig_id="s4", es_up=3, es_down=2, cell_line="CL1", pert_name="drugB"),
        ]
        wtcs_results = compute_wtcs(enrichments)
        return enrichments, wtcs_results

    def test_none_normalization(self):
        enrichments, wtcs = self._make_enrichments_and_wtcs()
        ncs = compute_ncs(enrichments, wtcs, method="none")
        assert len(ncs) == 4
        # NCS = WTCS when no normalization
        assert ncs[0].ncs == wtcs[0].wtcs

    def test_global_null(self):
        enrichments, wtcs = self._make_enrichments_and_wtcs()
        ncs = compute_ncs(enrichments, wtcs, method="global_null")
        assert len(ncs) == 4
        # All normalized by same factor
        assert ncs[0].ncs != 0  # should have non-zero NCS

    def test_cell_line_null(self):
        enrichments, wtcs = self._make_enrichments_and_wtcs()
        ncs = compute_ncs(enrichments, wtcs, method="cell_line_null")
        assert len(ncs) == 4


# ===== Stage 4: Tau =====

class TestComputeTau:
    def test_tau_returns_all_drugs(self):
        ncs = _make_ncs_list()
        results = compute_tau(ncs)
        drug_names = [r.pert_name for r in results]
        assert "drugA" in drug_names
        assert "drugB" in drug_names
        assert "drugC" in drug_names

    def test_tau_sorted_ascending(self):
        ncs = _make_ncs_list()
        results = compute_tau(ncs)
        taus = [r.tau for r in results]
        assert all(taus[i] <= taus[i+1] for i in range(len(taus)-1))

    def test_reverser_has_negative_tau(self):
        ncs = _make_ncs_list()
        results = compute_tau(ncs)
        drug_a = [r for r in results if r.pert_name == "drugA"][0]
        assert drug_a.tau < 0  # drugA has negative NCS -> negative Tau

    def test_mimicker_has_positive_tau(self):
        ncs = _make_ncs_list()
        results = compute_tau(ncs)
        drug_b = [r for r in results if r.pert_name == "drugB"][0]
        assert drug_b.tau > 0

    def test_bootstrap_reference_mode(self):
        """Bootstrap mode should produce valid Tau results."""
        ncs = _make_ncs_list()
        results = compute_tau(ncs, reference_mode="bootstrap", bootstrap_n=1000)
        assert len(results) >= 4
        # Results should still be sorted
        taus = [r.tau for r in results]
        assert all(taus[i] <= taus[i+1] for i in range(len(taus)-1))

    def test_leave_one_out_mode(self):
        """LOO mode should produce valid Tau results."""
        ncs = _make_ncs_list()
        results = compute_tau(ncs, reference_mode="leave_one_out")
        assert len(results) >= 4
        taus = [r.tau for r in results]
        assert all(taus[i] <= taus[i+1] for i in range(len(taus)-1))

    def test_external_reference(self):
        """External reference should be used when provided."""
        ncs = _make_ncs_list()
        ref = np.array([-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5.0])
        results = compute_tau(ncs, reference_ncs=ref, reference_mode="external")
        assert len(results) >= 4


# ===== Bootstrap and LOO reference builders =====

class TestBootstrapReference:
    def test_bootstrap_size(self):
        ncs = _make_ncs_list()
        ref = build_bootstrap_reference(ncs, n_bootstrap=2000, seed=42)
        assert len(ref) == 2000

    def test_bootstrap_distribution_centered(self):
        """Bootstrap should roughly preserve the original distribution's center."""
        ncs = _make_ncs_list()
        original = np.array([nr.ncs for nr in ncs])
        ref = build_bootstrap_reference(ncs, n_bootstrap=5000, seed=42)
        assert abs(np.mean(ref) - np.mean(original)) < 0.5

    def test_bootstrap_too_few(self):
        """With <10 NCS values, should return raw values."""
        ncs = [NCSResult(sig_id=f"s{i}", ncs=float(i), pert_name="d") for i in range(5)]
        ref = build_bootstrap_reference(ncs, n_bootstrap=100)
        assert len(ref) == 5  # returns raw, not bootstrap


class TestLeaveOneOutReference:
    def test_loo_excludes_drug(self):
        ncs = _make_ncs_list()
        loo = build_leave_one_out_reference(ncs)
        # drugA has 2 entries -> LOO ref should have 8
        assert len(loo["drugA"]) == 8
        # drugD has 2 entries -> LOO ref should have 8
        assert len(loo["drugD"]) == 8

    def test_loo_all_drugs_present(self):
        ncs = _make_ncs_list()
        loo = build_leave_one_out_reference(ncs)
        assert "drugA" in loo
        assert "drugB" in loo
        assert "drugC" in loo
        assert "drugD" in loo
        assert "drugE" in loo


# ===== Full pipeline =====

class TestCMapPipeline:
    def test_end_to_end(self):
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        pipeline = CMapPipeline(provider, ncs_method="global_null")
        results = pipeline.run()
        assert len(results) > 0

    def test_to_dataframe(self):
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        pipeline = CMapPipeline(provider, ncs_method="none")
        pipeline.run()
        df_tau = pipeline.to_dataframe()
        assert "drug" in df_tau.columns
        assert "tau" in df_tau.columns

    def test_signature_details(self):
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        pipeline = CMapPipeline(provider, ncs_method="none")
        pipeline.run()
        df_sig = pipeline.get_signature_details()
        assert "wtcs" in df_sig.columns
        assert "ncs" in df_sig.columns
        assert len(df_sig) == 10

    def test_pipeline_with_bootstrap_ref(self):
        """Pipeline should work with bootstrap tau reference mode."""
        df = _make_df_detail()
        provider = LDP3ESProvider(df)
        pipeline = CMapPipeline(provider, ncs_method="none", tau_reference_mode="bootstrap")
        results = pipeline.run()
        assert len(results) > 0


# ===== Quantile max helper =====

class TestQuantileMax:
    def test_basic(self):
        vals = np.array([-10, -5, 0, 5, 10])
        result = _quantile_max(vals)
        assert result != 0.0

    def test_single(self):
        assert _quantile_max(np.array([-5.0])) == -5.0

    def test_empty(self):
        assert _quantile_max(np.array([])) == 0.0
