"""Unit tests for DrugScorer"""

import pytest
from src.dr.scoring.scorer import DrugScorer, ScoringConfig


class TestDrugScorer:
    """Tests for multi-dimensional drug scoring"""

    def test_scorer_initialization(self):
        """Test scorer initializes with default config"""
        scorer = DrugScorer()
        assert scorer is not None
        assert scorer.config is not None
        assert isinstance(scorer.config, ScoringConfig)

    def test_scorer_custom_config(self):
        """Test scorer accepts custom configuration"""
        config = ScoringConfig(
            min_benefit_for_high_evidence=10,
            harm_penalty_per_paper=1.5
        )
        scorer = DrugScorer(config)
        assert scorer.config.min_benefit_for_high_evidence == 10
        assert scorer.config.harm_penalty_per_paper == 1.5

    def test_score_high_evidence_drug(self, sample_dossier):
        """Test scoring drug with strong evidence"""
        scorer = DrugScorer()

        # High benefit count
        dossier = sample_dossier.copy()
        dossier["evidence_count"] = {
            "benefit": 15,
            "harm": 2,
            "neutral": 3,
            "unclear": 5
        }

        score = scorer.score_drug(dossier)

        assert isinstance(score, dict)
        assert "evidence_strength_0_30" in score
        assert "total_score_0_100" in score
        assert score["evidence_strength_0_30"] >= 20.0  # High evidence score
        assert score["total_score_0_100"] > 50.0

    def test_score_low_evidence_drug(self, sample_dossier):
        """Test scoring drug with weak evidence"""
        scorer = DrugScorer()

        dossier = sample_dossier.copy()
        dossier["evidence_count"] = {
            "benefit": 2,
            "harm": 1,
            "neutral": 1,
            "unclear": 10
        }

        score = scorer.score_drug(dossier)

        assert score["evidence_strength_0_30"] < 15.0  # Low evidence score

    def test_score_within_bounds(self, sample_dossier):
        """Test all scores are within valid bounds"""
        scorer = DrugScorer()

        score = scorer.score_drug(sample_dossier)

        assert 0.0 <= score["evidence_strength_0_30"] <= 30.0
        assert 0.0 <= score["mechanism_plausibility_0_20"] <= 20.0
        assert 0.0 <= score["translatability_0_20"] <= 20.0
        assert 0.0 <= score["safety_fit_0_20"] <= 20.0
        assert 0.0 <= score["practicality_0_10"] <= 10.0
        assert 0.0 <= score["total_score_0_100"] <= 100.0

    def test_score_total_calculation(self, sample_dossier):
        """Test total score is sum of components"""
        scorer = DrugScorer()

        score = scorer.score_drug(sample_dossier)

        expected_total = (
            score["evidence_strength_0_30"] +
            score["mechanism_plausibility_0_20"] +
            score["translatability_0_20"] +
            score["safety_fit_0_20"] +
            score["practicality_0_10"]
        )

        assert abs(score["total_score_0_100"] - expected_total) < 0.01

    def test_score_deterministic(self, sample_dossier):
        """Test scoring is deterministic"""
        scorer = DrugScorer()

        score1 = scorer.score_drug(sample_dossier)
        score2 = scorer.score_drug(sample_dossier)

        assert score1["total_score_0_100"] == score2["total_score_0_100"]
        assert score1["evidence_strength_0_30"] == score2["evidence_strength_0_30"]

    def test_score_handles_missing_fields(self):
        """Test scoring handles incomplete dossiers gracefully"""
        scorer = DrugScorer()

        # Minimal dossier
        dossier = {
            "drug_id": "D001",
            "canonical_name": "test_drug",
            "total_pmids": 10
        }

        # Should not crash
        score = scorer.score_drug(dossier)
        assert isinstance(score, dict)
        assert "total_score_0_100" in score
