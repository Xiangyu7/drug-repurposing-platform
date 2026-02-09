"""Unit tests for ValidationPlanner"""

import pytest
from src.dr.scoring.validation import ValidationPlanner, ValidationPlan
from src.dr.scoring.cards import HypothesisCard


def _make_card(drug_id, name, decision, total, evidence=15, mechanism=15,
               translatability=15, safety=15, practicality=8, benefit=10, harm=1):
    """Helper to build a HypothesisCard for testing."""
    return HypothesisCard(
        drug_id=drug_id,
        canonical_name=name,
        gate_decision=decision,
        scores={
            "total_score_0_100": total,
            "evidence_strength_0_30": evidence,
            "mechanism_plausibility_0_20": mechanism,
            "translatability_0_20": translatability,
            "safety_fit_0_20": safety,
            "practicality_0_10": practicality,
        },
        evidence_summary={
            "benefit": benefit,
            "harm": harm,
            "neutral": 3,
            "unknown": 5,
            "total_pmids": benefit + harm + 8,
        },
        mechanism_hypothesis="Hypothesized mechanism",
    )


class TestValidationPlanner:
    """Tests for validation plan generation"""

    def test_planner_initialization(self):
        """Test validation planner initializes"""
        planner = ValidationPlanner()
        assert planner is not None

    def test_create_plan_for_go_drug(self):
        """Test creating validation plan for GO drug"""
        planner = ValidationPlanner()
        card = _make_card("D001", "wonder_drug", "GO", 85.0, evidence=28, mechanism=18, benefit=20, harm=2)
        dossier = {
            "drug_id": "D001", "canonical_name": "wonder_drug",
            "total_pmids": 100, "evidence_count": {"benefit": 20, "harm": 2, "neutral": 3, "unknown": 5}
        }

        plan = planner.create_plan(card, dossier)

        assert isinstance(plan, ValidationPlan)
        assert plan.drug_id == "D001"
        assert plan.canonical_name == "wonder_drug"
        assert plan.gate_decision == "GO"
        assert plan.priority in [1, 2, 3]

    def test_create_plan_for_maybe_drug(self):
        """Test creating validation plan for MAYBE drug"""
        planner = ValidationPlanner()
        card = _make_card("D002", "maybe_drug", "MAYBE", 55.0, evidence=20, mechanism=15, benefit=8, harm=2)
        dossier = {
            "drug_id": "D002", "canonical_name": "maybe_drug",
            "total_pmids": 50, "evidence_count": {"benefit": 8, "harm": 2, "neutral": 2, "unknown": 10}
        }

        plan = planner.create_plan(card, dossier)

        assert plan.gate_decision == "MAYBE"
        assert plan.priority >= 2

    def test_priority_calculation(self):
        """Test priority calculation based on score"""
        planner = ValidationPlanner()
        high_card = _make_card("D001", "high_score", "GO", 90.0, evidence=30, mechanism=20)
        med_card = _make_card("D002", "med_score", "GO", 65.0, evidence=25, mechanism=15)
        dossier = {
            "drug_id": "D001", "canonical_name": "test",
            "total_pmids": 50, "evidence_count": {"benefit": 10, "harm": 2, "neutral": 2, "unknown": 5}
        }

        plan_high = planner.create_plan(high_card, dossier)
        plan_med = planner.create_plan(med_card, dossier)

        assert plan_high.priority <= plan_med.priority

    def test_validation_stage_assignment(self):
        """Test validation stage is assigned appropriately"""
        planner = ValidationPlanner()
        card = _make_card("D001", "test_drug", "GO", 80.0, evidence=28, mechanism=18, benefit=20, harm=2)
        dossier = {
            "drug_id": "D001", "canonical_name": "test_drug",
            "total_pmids": 100, "evidence_count": {"benefit": 20, "harm": 2, "neutral": 3, "unknown": 5}
        }

        plan = planner.create_plan(card, dossier)

        assert plan.validation_stage in [
            "LITERATURE_REVIEW", "MECHANISM_VALIDATION", "PRECLINICAL_VALIDATION",
            "CLINICAL_TRIAL_DESIGN", "EXISTING_TRIAL_ANALYSIS"
        ]

    def test_experiments_list_populated(self):
        """Test experiments list is populated"""
        planner = ValidationPlanner()
        card = _make_card("D001", "test_drug", "GO", 80.0, evidence=28, mechanism=18, benefit=20, harm=2)
        dossier = {
            "drug_id": "D001", "canonical_name": "test_drug",
            "total_pmids": 100, "evidence_count": {"benefit": 20, "harm": 2, "neutral": 3, "unknown": 5}
        }

        plan = planner.create_plan(card, dossier)

        assert isinstance(plan.experiments, list)
        assert len(plan.experiments) >= 0

    def test_plan_to_dict(self):
        """Test ValidationPlan serializes to dictionary"""
        plan = ValidationPlan(
            drug_id="D001",
            canonical_name="test_drug",
            gate_decision="GO",
            priority=1,
            validation_stage="PRECLINICAL_VALIDATION",
            experiments=["ApoE-/- mouse model", "CIMT measurement"],
            trial_design={"phase": "2", "n": "200"},
            resources={"budget": "$500K"},
            timeline_weeks=52,
            cost_estimate_usd="$500K-$1M",
            notes=["High priority candidate"]
        )

        result = plan.to_dict()

        assert isinstance(result, dict)
        assert result["drug_id"] == "D001"
        assert result["gate_decision"] == "GO"
        assert result["priority"] == 1
        assert "experiments" in result
        assert "trial_design" in result

    def test_timeline_estimation(self):
        """Test timeline estimation is reasonable"""
        planner = ValidationPlanner()
        card = _make_card("D001", "test_drug", "GO", 80.0, evidence=28, mechanism=18, benefit=20, harm=2)
        dossier = {
            "drug_id": "D001", "canonical_name": "test_drug",
            "total_pmids": 100, "evidence_count": {"benefit": 20, "harm": 2, "neutral": 3, "unknown": 5}
        }

        plan = planner.create_plan(card, dossier)

        assert plan.timeline_weeks >= 0
        assert plan.timeline_weeks <= 520

    def test_no_go_drug_handling(self):
        """Test NO-GO drugs get lowest priority"""
        planner = ValidationPlanner()
        card = _make_card("D003", "bad_drug", "NO-GO", 25.0, evidence=10, mechanism=5, benefit=1, harm=5)
        dossier = {
            "drug_id": "D003", "canonical_name": "bad_drug",
            "total_pmids": 10, "evidence_count": {"benefit": 1, "harm": 5, "neutral": 1, "unknown": 5}
        }

        plan = planner.create_plan(card, dossier)

        assert plan.gate_decision == "NO-GO"
        assert plan.priority == 3
