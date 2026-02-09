"""Unit tests for HypothesisCardBuilder"""

import pytest
from src.dr.scoring.cards import HypothesisCardBuilder, HypothesisCard


class TestHypothesisCardBuilder:
    """Tests for hypothesis card generation"""

    def test_builder_initialization(self):
        """Test card builder initializes"""
        builder = HypothesisCardBuilder()
        assert builder is not None

    def test_build_card_basic(self, sample_dossier):
        """Test building basic hypothesis card"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)
        
        card = builder.build_card(sample_dossier, scores, decision)
        
        assert isinstance(card, HypothesisCard)
        assert card.drug_id == sample_dossier["drug_id"]
        assert card.canonical_name == sample_dossier["canonical_name"]

    def test_card_includes_scores(self, sample_dossier):
        """Test card includes all score components"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine

        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()

        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)

        card = builder.build_card(sample_dossier, scores, decision)

        # Scores are stored in card.scores dict
        assert card.scores["total_score_0_100"] == scores["total_score_0_100"]
        assert "evidence_strength_0_30" in card.scores
        assert "mechanism_plausibility_0_20" in card.scores
        assert "translatability_0_20" in card.scores
        assert "safety_fit_0_20" in card.scores
        assert "practicality_0_10" in card.scores

    def test_card_includes_gate_decision(self, sample_dossier):
        """Test card includes gating decision"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)
        
        card = builder.build_card(sample_dossier, scores, decision)
        
        assert card.gate_decision in ["GO", "MAYBE", "NO-GO"]
        assert card.gate_decision == decision.decision.value

    def test_card_has_summary(self, sample_dossier):
        """Test card includes evidence summary"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine

        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()

        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)

        card = builder.build_card(sample_dossier, scores, decision)

        # Card has evidence_summary dict
        assert isinstance(card.evidence_summary, dict)
        assert len(card.evidence_summary) > 0

    def test_card_has_next_steps(self, sample_dossier):
        """Test card includes next steps"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)
        
        card = builder.build_card(sample_dossier, scores, decision)
        
        assert isinstance(card.next_steps, list)
        assert len(card.next_steps) > 0

    def test_card_to_dict(self, sample_dossier):
        """Test card serializes to dictionary"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()
        
        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)
        
        card = builder.build_card(sample_dossier, scores, decision)
        result = card.to_dict()
        
        assert isinstance(result, dict)
        assert "drug_id" in result
        assert "canonical_name" in result
        assert "scores" in result
        assert "gate_decision" in result
        assert "evidence_summary" in result
        assert "next_steps" in result

    def test_card_to_markdown(self, sample_dossier):
        """Test card exports to markdown"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine

        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()

        scores = scorer.score_drug(sample_dossier)
        decision = gating.evaluate(sample_dossier, scores)

        card = builder.build_card(sample_dossier, scores, decision)
        markdown = card.to_markdown()

        assert isinstance(markdown, str)
        assert len(markdown) > 0
        assert card.canonical_name.lower() in markdown.lower()
        # Check that score appears in markdown
        total_score = card.scores.get("total_score_0_100", 0)
        assert str(int(total_score)) in markdown or f"{total_score:.1f}" in markdown

    def test_go_card_has_positive_next_steps(self):
        """Test GO card has positive next steps"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine
        
        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()
        
        # High quality dossier → GO
        dossier = {
            "drug_id": "D001",
            "canonical_name": "wonder_drug",
            "total_pmids": 100,
            "evidence_count": {"benefit": 20, "harm": 1, "neutral": 3, "unknown": 5},
            "mechanism_keywords": ["antioxidant", "anti-inflammatory"],
            "safety_concerns": []
        }
        
        scores = scorer.score_drug(dossier)
        decision = gating.evaluate(dossier, scores)
        card = builder.build_card(dossier, scores, decision)
        
        assert card.gate_decision == "GO"
        # Next steps should mention validation or trials
        next_steps_text = " ".join(card.next_steps).lower()
        assert any(word in next_steps_text for word in ["validation", "trial", "preclinical", "plan"])

    def test_no_go_card_explains_rejection(self):
        """Test NO-GO card explains rejection reasons"""
        from src.dr.scoring.scorer import DrugScorer
        from src.dr.scoring.gating import GatingEngine

        builder = HypothesisCardBuilder()
        scorer = DrugScorer()
        gating = GatingEngine()

        # Poor quality dossier → NO-GO
        dossier = {
            "drug_id": "D002",
            "canonical_name": "bad_drug",
            "total_pmids": 5,
            "evidence_count": {"benefit": 1, "harm": 5, "neutral": 0, "unknown": 10},
            "mechanism_keywords": [],
            "safety_concerns": ["hepatotoxicity"]
        }

        scores = scorer.score_drug(dossier)
        decision = gating.evaluate(dossier, scores)
        card = builder.build_card(dossier, scores, decision)

        assert card.gate_decision == "NO-GO"
        # Gate reasons should explain rejection
        assert len(card.gate_reasons) > 0
        reasons_text = " ".join(card.gate_reasons).lower()
        assert len(reasons_text) > 0  # Has some explanation
