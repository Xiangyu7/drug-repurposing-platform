"""Integration tests for Step7 pipeline"""

import pytest
from unittest.mock import Mock


@pytest.mark.integration
class TestStep7Pipeline:
    """End-to-end tests for Step7 scoring and gating pipeline"""

    def test_step7_scoring_pipeline(self, sample_dossier):
        """Test Step7 scoring pipeline processes dossier correctly"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        scorer = DrugScorer()
        gating = GatingEngine()
        
        # Score drug
        scores = scorer.score_drug(sample_dossier)
        
        assert "total_score_0_100" in scores
        assert 0 <= scores["total_score_0_100"] <= 100
        
        # Apply gating
        decision = gating.evaluate(sample_dossier, scores)
        
        assert decision.decision in ["GO", "MAYBE", "NO-GO"]
        assert "total_score" in decision.metrics

    def test_high_quality_drug_gets_go(self):
        """Test that high-quality drug gets GO decision"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        # Create high-quality dossier
        dossier = {
            "drug_id": "D001",
            "canonical_name": "wonder_drug",
            "total_pmids": 100,
            "evidence_count": {
                "benefit": 20,
                "harm": 1,
                "neutral": 3,
                "unknown": 5
            },
            "mechanism_keywords": ["antioxidant", "anti-inflammatory", "nrf2"],
            "safety_concerns": []
        }
        
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(dossier)
        decision = gating.evaluate(dossier, scores)
        
        # High quality drug should get GO
        assert scores["total_score_0_100"] >= 60
        assert decision.decision == "GO"

    def test_poor_quality_drug_gets_no_go(self):
        """Test that poor-quality drug gets NO-GO decision"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        # Create poor-quality dossier
        dossier = {
            "drug_id": "D002",
            "canonical_name": "bad_drug",
            "total_pmids": 5,  # Too few
            "evidence_count": {
                "benefit": 1,  # Too low
                "harm": 5,     # High harm
                "neutral": 0,
                "unknown": 10
            },
            "mechanism_keywords": [],
            "safety_concerns": ["hepatotoxicity"]
        }
        
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(dossier)
        decision = gating.evaluate(dossier, scores)
        
        # Poor quality drug should get NO-GO
        assert decision.decision == "NO-GO"
        assert len(decision.gate_reasons) > 0

    def test_medium_quality_drug_gets_maybe(self):
        """Test that medium-quality drug gets MAYBE decision"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine, GatingConfig
        
        # Create medium-quality dossier
        dossier = {
            "drug_id": "D003",
            "canonical_name": "okay_drug",
            "total_pmids": 20,
            "evidence_count": {
                "benefit": 3,
                "harm": 2,
                "neutral": 4,
                "unknown": 10
            },
            "mechanism_keywords": ["antioxidant"],
            "safety_concerns": []
        }
        
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(dossier)
        decision = gating.evaluate(dossier, scores)
        
        # Medium quality drug should get MAYBE
        assert 40 <= scores["total_score_0_100"] < 60
        assert decision.decision == "MAYBE"

    def test_scoring_components_sum_to_total(self):
        """Test that scoring components sum to total score"""
        from src.dr.scoring.scorer import DrugScorer
        
        scorer = DrugScorer()
        
        dossier = {
            "drug_id": "D004",
            "canonical_name": "test_drug",
            "total_pmids": 50,
            "evidence_count": {"benefit": 10, "harm": 2, "neutral": 3, "unknown": 5},
            "mechanism_keywords": ["test"],
            "safety_concerns": []
        }
        
        scores = scorer.score_drug(dossier)
        
        # Verify components sum to total
        components_sum = (
            scores["evidence_strength_0_30"] +
            scores["mechanism_plausibility_0_20"] +
            scores["translatability_0_20"] +
            scores["safety_fit_0_20"] +
            scores["practicality_0_10"]
        )
        
        assert abs(scores["total_score_0_100"] - components_sum) < 0.01

    def test_batch_scoring_consistency(self):
        """Test batch scoring produces consistent results"""
        from src.dr.scoring.scorer import DrugScorer
        
        scorer = DrugScorer()
        
        dossiers = [
            {
                "drug_id": f"D{i:03d}",
                "canonical_name": f"drug_{i}",
                "total_pmids": 50,
                "evidence_count": {"benefit": 10, "harm": 2, "neutral": 3, "unknown": 5},
                "mechanism_keywords": ["test"],
                "safety_concerns": []
            }
            for i in range(5)
        ]
        
        # Score individually
        individual_scores = [scorer.score_drug(d) for d in dossiers]
        
        # All should have same total score (identical dossiers)
        total_scores = [s["total_score_0_100"] for s in individual_scores]
        assert len(set(total_scores)) == 1  # All identical

    def test_gating_respects_custom_config(self):
        """Test gating respects custom configuration"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine, GatingConfig
        
        dossier = {
            "drug_id": "D005",
            "canonical_name": "test_drug",
            "total_pmids": 50,
            "evidence_count": {"benefit": 4, "harm": 1, "neutral": 2, "unknown": 5},
            "mechanism_keywords": ["test"],
            "safety_concerns": []
        }
        
        scorer = DrugScorer()
        scores = scorer.score_drug(dossier)
        
        # Default config (min_benefit=2)
        gating_default = GatingEngine()
        decision_default = gating_default.evaluate(dossier, scores)
        
        # Strict config (min_benefit=10)
        config_strict = GatingConfig(min_benefit_papers=10)
        gating_strict = GatingEngine(config_strict)
        decision_strict = gating_strict.evaluate(dossier, scores)
        
        # Same dossier, different configs, different decisions
        assert decision_strict.decision == "NO-GO"  # Fails strict gate

    def test_end_to_end_data_flow(self, sample_dossier):
        """Test complete data flow from dossier to decision"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        scorer = DrugScorer()
        gating = GatingEngine()
        
        # Complete pipeline
        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)
        decision_dict = decision.to_dict()
        
        # Verify data structure integrity
        assert isinstance(scores, dict)
        assert isinstance(decision_dict, dict)
        
        assert "total_score_0_100" in scores
        assert "decision" in decision_dict
        assert "gate_reasons" in decision_dict
        assert "metrics" in decision_dict
        
        # Verify metrics match original dossier
        assert decision_dict["metrics"]["benefit"] == sample_dossier["evidence_count"]["benefit"]
