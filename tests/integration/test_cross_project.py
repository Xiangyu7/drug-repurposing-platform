"""Cross-project integration tests.

Verifies that kg_explain and LLM+RAG证据工程 share compatible data schemas,
so changes in one sub-project don't silently break the other.
"""
import json
import pytest
import pandas as pd
from pathlib import Path


# ── kg_explain imports ──
from kg_explain.rankers.uncertainty import (
    bootstrap_ci,
    assign_confidence_tier,
    add_uncertainty_to_ranking,
)
from kg_explain.evaluation.leakage_audit import generate_leakage_report


# ── LLM+RAG imports ──
from src.dr.contracts import (
    STEP7_SCORES_REQUIRED_COLUMNS,
    STEP7_GATING_REQUIRED_COLUMNS,
    STEP8_SHORTLIST_REQUIRED_COLUMNS,
    STEP9_PLAN_REQUIRED_COLUMNS,
    validate_step7_cards,
    validate_step8_shortlist_columns,
)
from src.dr.contracts_enforcer import ContractEnforcer, ContractViolationError
from src.dr.scoring.release_gate import ReleaseGate, ReleaseGateConfig


# ════════════════════════════════════════
# Schema compatibility tests
# ════════════════════════════════════════


class TestKGRankCSVSchema:
    """Verify kg_explain ranking output is compatible with LLM+RAG expectations."""

    # These are the columns that kg_explain V5 always produces
    KG_RANK_REQUIRED = {
        "drug_normalized", "diseaseId", "diseaseName",
        "mechanism_score", "safety_penalty", "trial_penalty",
        "risk_multiplier", "phenotype_boost", "phenotype_multiplier",
        "final_score",
    }

    def test_rank_csv_has_all_required_columns(self):
        """Simulate a V5 rank output and verify all expected columns present."""
        df = pd.DataFrame([{
            "drug_normalized": "aspirin",
            "diseaseId": "EFO_0003914",
            "diseaseName": "atherosclerosis",
            "mechanism_score": 0.85,
            "safety_penalty": 0.1,
            "trial_penalty": 0.05,
            "risk_multiplier": 0.95,
            "phenotype_boost": 0.08,
            "phenotype_multiplier": 1.08,
            "final_score": 0.72,
        }])
        missing = self.KG_RANK_REQUIRED - set(df.columns)
        assert missing == set(), f"Missing columns in rank output: {missing}"

    def test_uncertainty_columns_after_g1(self):
        """After G1 improvement, rank CSV should have CI columns."""
        rank_df = pd.DataFrame([{
            "drug_normalized": "aspirin",
            "diseaseId": "EFO_0003914",
            "final_score": 0.72,
        }])
        paths = [{"drug": "aspirin", "diseaseId": "EFO_0003914", "path_score": 0.8}]
        result = add_uncertainty_to_ranking(rank_df, paths)

        expected_ci_cols = {"ci_lower", "ci_upper", "ci_width", "confidence_tier", "n_evidence_paths"}
        actual_cols = set(result.columns)
        missing = expected_ci_cols - actual_cols
        assert missing == set(), f"Missing CI columns: {missing}"


class TestStep7CardsRoundtrip:
    """Verify Step7 cards JSON can pass contract validation."""

    def test_valid_cards_pass_validation(self):
        cards = [
            {
                "drug_id": "drug_001",
                "canonical_name": "aspirin",
                "scores": {"total_score_0_100": 72.5},
                "gate_decision": "GO",
                "dossier_path": "/path/to/dossier.json",
            }
        ]
        issues = validate_step7_cards(cards)
        assert issues == [], f"Unexpected validation issues: {issues}"

    def test_invalid_cards_detected(self):
        cards = [{"drug_id": "drug_001"}]  # Missing canonical_name, scores, gate
        issues = validate_step7_cards(cards)
        assert len(issues) > 0


class TestStep8ShortlistRoundtrip:
    """Verify Step8 shortlist meets schema requirements."""

    def test_all_required_columns_defined(self):
        # Ensure the constant is non-empty
        assert len(STEP8_SHORTLIST_REQUIRED_COLUMNS) >= 10

    def test_valid_shortlist_passes(self):
        cols = list(STEP8_SHORTLIST_REQUIRED_COLUMNS)
        df = pd.DataFrame([{c: "test" for c in cols}])
        issues = validate_step8_shortlist_columns(df.columns)
        assert issues == []


class TestLeakageAuditIntegration:
    """Verify leakage_audit integrates with temporal validation results."""

    def test_leakage_report_has_required_keys(self):
        train = pd.DataFrame({
            "drug_normalized": ["aspirin", "metformin"],
            "diseaseId": ["EFO_0003914", "EFO_0000378"],
        })
        test = pd.DataFrame({
            "drug_normalized": ["aspirin", "rosuvastatin"],
            "diseaseId": ["EFO_0000378", "EFO_0003914"],
        })
        report = generate_leakage_report(train, test, "integration_test")

        required_keys = {"split_name", "passed", "drug_overlap", "disease_overlap",
                         "pair_overlap", "seen_drug_test_fraction", "recommendations"}
        assert required_keys.issubset(report.keys())

    def test_clean_split_passes(self):
        train = pd.DataFrame({
            "drug_normalized": ["aspirin"],
            "diseaseId": ["EFO_0003914"],
        })
        test = pd.DataFrame({
            "drug_normalized": ["metformin"],
            "diseaseId": ["EFO_0000378"],
        })
        report = generate_leakage_report(train, test)
        assert report["passed"] is True


class TestReleaseGateIntegration:
    """Verify release gate works with contracts enforcer."""

    def test_release_gate_catches_nogo_in_shortlist(self):
        shortlist_df = pd.DataFrame({
            "canonical_name": ["aspirin", "metformin"],
            "drug_id": ["d1", "d2"],
            "gate": ["GO", "NO-GO"],
            "total_score_0_100": [72, 35],
        })
        gate = ReleaseGate(ReleaseGateConfig(block_nogo=True))
        result = gate.check_shortlist_composition(shortlist_df)
        assert len(result.blockers) > 0  # Should have blockers

    def test_contract_enforcer_and_release_gate_coexist(self):
        """Both modules can be used in the same pipeline."""
        enforcer = ContractEnforcer(strict=False)
        gate = ReleaseGate(ReleaseGateConfig(strict=False))

        # Both should be importable and instantiable without conflict
        assert enforcer is not None
        assert gate is not None


class TestBootstrapCIBasic:
    """Verify bootstrap CI module is importable and functional from integration context."""

    def test_basic_ci_computation(self):
        result = bootstrap_ci([0.5, 0.6, 0.7, 0.8, 0.9])
        assert "mean" in result
        assert "ci_lower" in result
        assert "ci_upper" in result
        assert result["ci_lower"] <= result["mean"] <= result["ci_upper"]

    def test_confidence_tier_assignment(self):
        assert assign_confidence_tier(0.05) == "HIGH"
        assert assign_confidence_tier(0.15) == "MEDIUM"
        assert assign_confidence_tier(0.30) == "LOW"
