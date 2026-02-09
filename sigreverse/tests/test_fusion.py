"""Unit tests for sigreverse.fusion module.

Tests cover:
    - Evidence sources (SignatureEvidence, KGExplainEvidence)
    - Normalization utilities
    - FusionRanker
    - Confidence levels
"""
import pytest
import numpy as np
import pandas as pd

from sigreverse.fusion import (
    SignatureEvidence, KGExplainEvidence, SafetyEvidence,
    FusionRanker, min_max_normalize, rank_normalize,
    FusionScore,
)


# ===== Normalization =====

class TestMinMaxNormalize:
    def test_basic(self):
        scores = {"a": -10.0, "b": 0.0, "c": 10.0}
        normed = min_max_normalize(scores, lower_is_better=True)
        assert normed["a"] == pytest.approx(0.0)   # best (lowest)
        assert normed["c"] == pytest.approx(1.0)   # worst (highest)

    def test_all_same(self):
        scores = {"a": 5.0, "b": 5.0}
        normed = min_max_normalize(scores)
        assert normed["a"] == pytest.approx(0.5)

    def test_empty(self):
        assert min_max_normalize({}) == {}


class TestRankNormalize:
    def test_basic(self):
        scores = {"a": -10.0, "b": 0.0, "c": 10.0}
        normed = rank_normalize(scores, lower_is_better=True)
        assert normed["a"] == pytest.approx(0.0)  # rank 1 = best
        assert normed["c"] == pytest.approx(1.0)  # rank 3 = worst

    def test_empty(self):
        assert rank_normalize({}) == {}


# ===== Evidence sources =====

class TestSignatureEvidence:
    def test_extraction(self):
        df = pd.DataFrame({
            "drug": ["aspirin", "ibuprofen", "metformin"],
            "final_reversal_score": [-5.0, -3.0, -1.0],
        })
        ev = SignatureEvidence(df)
        scores = ev.get_scores()
        assert len(scores) == 3
        assert scores["aspirin"] == -5.0

    def test_source_name(self):
        ev = SignatureEvidence(pd.DataFrame({"drug": [], "final_reversal_score": []}))
        assert ev.source_name() == "SigReverse"


class TestKGExplainEvidence:
    def test_from_dataframe(self):
        df = pd.DataFrame({
            "drug": ["aspirin", "ibuprofen"],
            "kg_score": [-2.0, -1.0],
        })
        ev = KGExplainEvidence(df_kg=df)
        scores = ev.get_scores()
        assert len(scores) == 2

    def test_empty(self):
        ev = KGExplainEvidence()
        assert len(ev.get_scores()) == 0


# ===== FusionRanker =====

class TestFusionRanker:
    def _make_drug_df(self):
        return pd.DataFrame({
            "drug": ["aspirin", "ibuprofen", "metformin", "simvastatin"],
            "final_reversal_score": [-8.0, -5.0, -2.0, -6.0],
        })

    def _make_kg_df(self):
        return pd.DataFrame({
            "drug": ["aspirin", "ibuprofen", "simvastatin", "atorvastatin"],
            "kg_score": [-3.0, -1.0, -5.0, -4.0],
        })

    def test_basic_fusion(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        ranker.add_evidence(KGExplainEvidence(df_kg=self._make_kg_df()))
        results = ranker.fuse()
        assert len(results) > 0

    def test_all_drugs_included(self):
        """Union of drugs from all sources should be present."""
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        ranker.add_evidence(KGExplainEvidence(df_kg=self._make_kg_df()))
        results = ranker.fuse()
        drug_names = {r.drug for r in results}
        assert "aspirin" in drug_names
        assert "atorvastatin" in drug_names  # only in KG

    def test_sorted_by_fusion_score(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        results = ranker.fuse()
        scores = [r.fusion_score for r in results]
        assert all(scores[i] <= scores[i+1] for i in range(len(scores)-1))

    def test_ranks_assigned(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        results = ranker.fuse()
        ranks = [r.rank for r in results]
        assert ranks == list(range(1, len(results) + 1))

    def test_confidence_levels(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        ranker.add_evidence(KGExplainEvidence(df_kg=self._make_kg_df()))
        results = ranker.fuse()
        # Drugs in both sources should have medium confidence
        aspirin = [r for r in results if r.drug == "aspirin"][0]
        assert aspirin.confidence in ("medium", "high")
        assert aspirin.evidence_sources >= 2

    def test_to_dataframe(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        ranker.fuse()
        df = ranker.to_dataframe()
        assert "rank" in df.columns
        assert "fusion_score" in df.columns
        assert "confidence" in df.columns

    def test_custom_weights(self):
        ranker = FusionRanker(weights={"signature": 1.0, "kg": 0.0})
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        ranker.add_evidence(KGExplainEvidence(df_kg=self._make_kg_df()))
        results = ranker.fuse()
        assert len(results) > 0

    def test_dose_response_bonus(self):
        ranker = FusionRanker()
        ranker.add_evidence(SignatureEvidence(self._make_drug_df()))
        dr_df = pd.DataFrame({
            "drug": ["aspirin", "ibuprofen"],
            "dr_quality": ["excellent", "poor"],
        })
        ranker.set_dose_response(dr_df)
        results = ranker.fuse()
        aspirin = [r for r in results if r.drug == "aspirin"][0]
        ibuprofen = [r for r in results if r.drug == "ibuprofen"][0]
        assert aspirin.dr_bonus < ibuprofen.dr_bonus  # excellent gets bigger bonus (more negative)
