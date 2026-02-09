"""Unit tests for GatingEngine"""

import pytest
from src.dr.scoring.gating import GatingEngine, GatingConfig, GateDecision, GatingDecision


class TestGatingEngine:
    """Tests for GO/MAYBE/NO-GO gating logic"""

    def test_engine_initialization(self):
        """Test gating engine initializes with default config"""
        engine = GatingEngine()
        assert engine is not None
        assert engine.config is not None
        assert isinstance(engine.config, GatingConfig)

    def test_engine_custom_config(self):
        """Test gating engine accepts custom configuration"""
        config = GatingConfig(
            min_benefit_papers=5,
            go_threshold=70.0,
            maybe_threshold=50.0
        )
        engine = GatingEngine(config)
        assert engine.config.min_benefit_papers == 5
        assert engine.config.go_threshold == 70.0
        assert engine.config.maybe_threshold == 50.0

    def test_go_decision_high_score(self):
        """Test GO decision for drug with high score and strong evidence"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D001",
            "canonical_name": "test_drug",
            "total_pmids": 100,
            "evidence_count": {
                "benefit": 15,
                "harm": 2,
                "neutral": 3,
                "unknown": 5
            }
        }

        scores = {
            "total_score_0_100": 85.0,
            "evidence_strength_0_30": 28.0,
            "safety_fit_0_20": 18.0
        }

        decision = engine.evaluate(dossier, scores)

        assert isinstance(decision, GatingDecision)
        assert decision.decision == GateDecision.GO
        assert len(decision.gate_reasons) == 0
        assert decision.scores == scores

    def test_maybe_decision_medium_score(self):
        """Test MAYBE decision for drug with medium score"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D002",
            "canonical_name": "test_drug",
            "total_pmids": 50,
            "evidence_count": {
                "benefit": 5,
                "harm": 1,
                "neutral": 2,
                "unknown": 10
            }
        }

        scores = {
            "total_score_0_100": 55.0,  # Between maybe (40) and go (60)
            "safety_fit_0_20": 18.0
        }

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.MAYBE
        assert len(decision.gate_reasons) > 0
        assert "score" in decision.gate_reasons[0].lower()

    def test_no_go_low_score(self):
        """Test NO-GO decision for drug with low score"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D003",
            "canonical_name": "test_drug",
            "total_pmids": 20,
            "evidence_count": {
                "benefit": 2,
                "harm": 0,
                "neutral": 1,
                "unknown": 15
            }
        }

        scores = {
            "total_score_0_100": 30.0,  # Below maybe threshold (40)
            "safety_fit_0_20": 18.0
        }

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO
        assert len(decision.gate_reasons) > 0

    def test_hard_gate_min_benefit(self):
        """Test hard gate: minimum benefit papers"""
        config = GatingConfig(min_benefit_papers=5)
        engine = GatingEngine(config)

        dossier = {
            "drug_id": "D004",
            "total_pmids": 100,
            "evidence_count": {
                "benefit": 2,  # Below threshold of 5
                "harm": 0,
                "neutral": 0,
                "unknown": 20
            }
        }

        scores = {"total_score_0_100": 80.0, "safety_fit_0_20": 18.0}

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO
        assert any("benefit" in reason.lower() for reason in decision.gate_reasons)

    def test_hard_gate_max_harm_ratio(self):
        """Test hard gate: maximum harm ratio"""
        config = GatingConfig(max_harm_ratio=0.3)  # 30% max harm
        engine = GatingEngine(config)

        dossier = {
            "drug_id": "D005",
            "total_pmids": 100,
            "evidence_count": {
                "benefit": 5,
                "harm": 10,  # 10/(5+10) = 67% harm > 30% threshold
                "neutral": 2,
                "unknown": 10
            }
        }

        scores = {"total_score_0_100": 70.0, "safety_fit_0_20": 18.0}

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO
        assert any("harm" in reason.lower() for reason in decision.gate_reasons)

    def test_hard_gate_min_pmids(self):
        """Test hard gate: minimum total PMIDs"""
        config = GatingConfig(min_total_pmids=10)
        engine = GatingEngine(config)

        dossier = {
            "drug_id": "D006",
            "total_pmids": 5,  # Below threshold of 10
            "evidence_count": {
                "benefit": 5,
                "harm": 0,
                "neutral": 0,
                "unknown": 0
            }
        }

        scores = {"total_score_0_100": 80.0, "safety_fit_0_20": 18.0}

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO
        assert any("pmid" in reason.lower() for reason in decision.gate_reasons)

    def test_hard_gate_safety_blacklist(self):
        """Test hard gate: safety blacklist"""
        config = GatingConfig(blacklist_is_hard_gate=True)
        engine = GatingEngine(config)

        dossier = {
            "drug_id": "D007",
            "canonical_name": "dexamethasone",  # On safety blacklist
            "total_pmids": 100,
            "evidence_count": {
                "benefit": 20,
                "harm": 2,
                "neutral": 3,
                "unknown": 5
            }
        }

        scores = {
            "total_score_0_100": 85.0,
            "safety_fit_0_20": 10.0  # Low due to blacklist
        }

        decision = engine.evaluate(dossier, scores, canonical_name="dexamethasone")

        assert decision.decision == GateDecision.NO_GO
        assert any("safety" in reason.lower() for reason in decision.gate_reasons)

    def test_metrics_collection(self):
        """Test that decision includes all relevant metrics"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D008",
            "total_pmids": 50,
            "evidence_count": {
                "benefit": 10,
                "harm": 3,
                "neutral": 2,
                "unknown": 5
            }
        }

        scores = {"total_score_0_100": 70.0, "safety_fit_0_20": 18.0}

        decision = engine.evaluate(dossier, scores)

        assert "benefit" in decision.metrics
        assert "harm" in decision.metrics
        assert "total_pmids" in decision.metrics
        assert "total_score" in decision.metrics
        assert decision.metrics["benefit"] == 10
        assert decision.metrics["harm"] == 3

    def test_decision_to_dict(self):
        """Test GatingDecision serializes to dictionary"""
        decision = GatingDecision(
            decision=GateDecision.GO,
            gate_reasons=[],
            scores={"total_score_0_100": 80.0},
            metrics={"benefit": 15, "harm": 2}
        )

        result = decision.to_dict()

        assert isinstance(result, dict)
        assert result["decision"] == "GO"
        assert result["gate_reasons"] == []
        assert "scores" in result
        assert "metrics" in result

    def test_edge_case_zero_evidence(self):
        """Test handling of drug with zero evidence"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D009",
            "total_pmids": 0,
            "evidence_count": {
                "benefit": 0,
                "harm": 0,
                "neutral": 0,
                "unknown": 0
            }
        }

        scores = {"total_score_0_100": 0.0, "safety_fit_0_20": 20.0}

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO
        assert len(decision.gate_reasons) > 0

    def test_edge_case_only_harm(self):
        """Test drug with only harm evidence"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D010",
            "total_pmids": 20,
            "evidence_count": {
                "benefit": 0,
                "harm": 10,
                "neutral": 0,
                "unknown": 5
            }
        }

        scores = {"total_score_0_100": 20.0, "safety_fit_0_20": 10.0}

        decision = engine.evaluate(dossier, scores)

        assert decision.decision == GateDecision.NO_GO

    def test_threshold_boundaries(self):
        """Test decisions at exact threshold boundaries"""
        engine = GatingEngine()

        dossier = {
            "drug_id": "D011",
            "total_pmids": 50,
            "evidence_count": {"benefit": 5, "harm": 1, "neutral": 2, "unknown": 10}
        }

        # Test at GO threshold (60.0)
        scores1 = {"total_score_0_100": 60.0, "safety_fit_0_20": 18.0}
        decision1 = engine.evaluate(dossier, scores1)
        assert decision1.decision == GateDecision.GO

        # Test at MAYBE threshold (40.0)
        scores2 = {"total_score_0_100": 40.0, "safety_fit_0_20": 18.0}
        decision2 = engine.evaluate(dossier, scores2)
        assert decision2.decision == GateDecision.MAYBE

        # Test just below MAYBE threshold
        scores3 = {"total_score_0_100": 39.9, "safety_fit_0_20": 18.0}
        decision3 = engine.evaluate(dossier, scores3)
        assert decision3.decision == GateDecision.NO_GO
