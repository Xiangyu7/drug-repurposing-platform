"""Tests for ContractEnforcer - schema contract enforcement."""

import pytest
import pandas as pd

from src.dr.contracts_enforcer import (
    ContractEnforcer,
    ContractViolationError,
    default_enforcer,
)
from src.dr.contracts import (
    STEP7_SCORES_REQUIRED_COLUMNS,
    STEP7_GATING_REQUIRED_COLUMNS,
    STEP8_SHORTLIST_REQUIRED_COLUMNS,
    STEP9_PLAN_REQUIRED_COLUMNS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_dossier():
    """Return a minimal valid Step6 dossier dict."""
    return {
        "drug_id": "D001",
        "canonical_name": "aspirin",
        "target_disease": "atherosclerosis",
        "endpoint_type": "CV_EVENTS",
        "pubmed_rag": {
            "top_abstracts": [{"pmid": "111"}],
            "top_sentences": [{"text": "x"}],
        },
        "llm_structured": {
            "supporting_evidence": [{"pmid": "111"}],
            "harm_or_neutral_evidence": [],
            "counts": {
                "supporting_evidence_count": 1,
                "supporting_sentence_count": 1,
                "unique_supporting_pmids_count": 1,
                "harm_or_neutral_count": 0,
            },
        },
    }


def _make_incomplete_dossier():
    """Return a dossier missing required keys."""
    return {"drug_id": "D001"}


def _make_df(columns):
    """Build a single-row DataFrame with the given columns."""
    return pd.DataFrame({col: ["x"] for col in columns})


def _make_valid_cards():
    """Return a minimal valid Step7 cards list."""
    return [
        {
            "drug_id": "D001",
            "canonical_name": "aspirin",
            "scores": {"total_score_0_100": 75},
            "gate_decision": "GO",
            "dossier_path": "/tmp/d001.json",
        }
    ]


# ---------------------------------------------------------------------------
# TestContractViolationError
# ---------------------------------------------------------------------------

class TestContractViolationError:
    def test_message_format(self):
        err = ContractViolationError("step7_scores", ["missing column: drug_id"])
        assert "step7_scores" in str(err)
        assert "1 issues" in str(err)
        assert "missing column: drug_id" in str(err)

    def test_step_and_violations_accessible(self):
        violations = ["issue A", "issue B"]
        err = ContractViolationError("step8_shortlist", violations)
        assert err.step == "step8_shortlist"
        assert err.violations == violations
        assert "2 issues" in str(err)


# ---------------------------------------------------------------------------
# TestCheckStep6Dossier
# ---------------------------------------------------------------------------

class TestCheckStep6Dossier:
    def test_valid_dossier_passes_strict(self):
        enforcer = ContractEnforcer(strict=True)
        dossier = _make_valid_dossier()
        result = enforcer.check_step6_dossier(dossier)
        assert result is dossier
        assert len(enforcer.violations) == 0

    def test_incomplete_dossier_raises_strict(self):
        enforcer = ContractEnforcer(strict=True)
        with pytest.raises(ContractViolationError) as exc_info:
            enforcer.check_step6_dossier(_make_incomplete_dossier())
        assert exc_info.value.step == "step6_dossier"
        assert len(exc_info.value.violations) > 0

    def test_incomplete_dossier_warns_soft(self):
        enforcer = ContractEnforcer(strict=False)
        result = enforcer.check_step6_dossier(_make_incomplete_dossier())
        # Should not raise, but should record violations
        assert len(enforcer.violations) == 1
        assert enforcer.violations[0]["step"] == "step6_dossier"
        assert result == _make_incomplete_dossier()

    def test_stamps_contract_when_valid(self):
        enforcer = ContractEnforcer(strict=True)
        dossier = _make_valid_dossier()
        enforcer.check_step6_dossier(dossier, producer="test_producer")
        assert "_contract" in dossier
        assert dossier["_contract"]["producer"] == "test_producer"

    def test_does_not_stamp_on_violation(self):
        enforcer = ContractEnforcer(strict=False)
        dossier = _make_incomplete_dossier()
        enforcer.check_step6_dossier(dossier, producer="test_producer")
        assert "_contract" not in dossier


# ---------------------------------------------------------------------------
# TestCheckStep7Scores
# ---------------------------------------------------------------------------

class TestCheckStep7Scores:
    def test_valid_df_passes(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(STEP7_SCORES_REQUIRED_COLUMNS)
        enforcer.check_step7_scores(df)  # Should not raise
        assert len(enforcer.violations) == 0

    def test_missing_columns_raises(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(["drug_id"])  # Missing most required columns
        with pytest.raises(ContractViolationError) as exc_info:
            enforcer.check_step7_scores(df)
        assert exc_info.value.step == "step7_scores"
        assert len(exc_info.value.violations) > 0

    def test_all_required_columns_tested(self):
        """Removing any single required column should cause a violation."""
        for col in STEP7_SCORES_REQUIRED_COLUMNS:
            remaining = STEP7_SCORES_REQUIRED_COLUMNS - {col}
            df = _make_df(remaining)
            enforcer = ContractEnforcer(strict=False)
            enforcer.check_step7_scores(df)
            assert len(enforcer.violations) == 1, f"No violation for missing {col}"


# ---------------------------------------------------------------------------
# TestCheckStep7Gating
# ---------------------------------------------------------------------------

class TestCheckStep7Gating:
    def test_valid_df_passes(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(STEP7_GATING_REQUIRED_COLUMNS)
        enforcer.check_step7_gating(df)
        assert len(enforcer.violations) == 0

    def test_missing_columns_raises(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(["drug_id"])
        with pytest.raises(ContractViolationError):
            enforcer.check_step7_gating(df)

    def test_all_required_columns_tested(self):
        for col in STEP7_GATING_REQUIRED_COLUMNS:
            remaining = STEP7_GATING_REQUIRED_COLUMNS - {col}
            df = _make_df(remaining)
            enforcer = ContractEnforcer(strict=False)
            enforcer.check_step7_gating(df)
            assert len(enforcer.violations) == 1, f"No violation for missing {col}"


# ---------------------------------------------------------------------------
# TestCheckStep7Cards
# ---------------------------------------------------------------------------

class TestCheckStep7Cards:
    def test_valid_cards_pass(self):
        enforcer = ContractEnforcer(strict=True)
        enforcer.check_step7_cards(_make_valid_cards())
        assert len(enforcer.violations) == 0

    def test_invalid_structure_raises(self):
        enforcer = ContractEnforcer(strict=True)
        # cards must be a list
        with pytest.raises(ContractViolationError):
            enforcer.check_step7_cards("not a list")

    def test_card_missing_keys_raises(self):
        enforcer = ContractEnforcer(strict=True)
        cards = [{"drug_id": "D001"}]  # Missing canonical_name, scores, gate
        with pytest.raises(ContractViolationError) as exc_info:
            enforcer.check_step7_cards(cards)
        assert len(exc_info.value.violations) > 0


# ---------------------------------------------------------------------------
# TestCheckStep8Shortlist
# ---------------------------------------------------------------------------

class TestCheckStep8Shortlist:
    def test_valid_df_passes(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(STEP8_SHORTLIST_REQUIRED_COLUMNS)
        enforcer.check_step8_shortlist(df)
        assert len(enforcer.violations) == 0

    def test_missing_columns_raises(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(["drug_id", "canonical_name"])
        with pytest.raises(ContractViolationError):
            enforcer.check_step8_shortlist(df)


# ---------------------------------------------------------------------------
# TestCheckStep9Plan
# ---------------------------------------------------------------------------

class TestCheckStep9Plan:
    def test_valid_df_passes(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(STEP9_PLAN_REQUIRED_COLUMNS)
        enforcer.check_step9_plan(df)
        assert len(enforcer.violations) == 0

    def test_missing_columns_raises(self):
        enforcer = ContractEnforcer(strict=True)
        df = _make_df(["rank"])
        with pytest.raises(ContractViolationError):
            enforcer.check_step9_plan(df)


# ---------------------------------------------------------------------------
# TestSoftMode
# ---------------------------------------------------------------------------

class TestSoftMode:
    def test_violations_logged_not_raised(self):
        enforcer = ContractEnforcer(strict=False)
        df = _make_df(["drug_id"])
        enforcer.check_step7_scores(df)  # Should NOT raise
        assert len(enforcer.violations) == 1

    def test_violation_log_populated(self):
        enforcer = ContractEnforcer(strict=False)
        # Trigger two different violations
        enforcer.check_step7_scores(_make_df(["drug_id"]))
        enforcer.check_step7_gating(_make_df(["drug_id"]))
        assert len(enforcer.violations) == 2
        assert enforcer.violations[0]["step"] == "step7_scores"
        assert enforcer.violations[1]["step"] == "step7_gating"

    def test_clear_log(self):
        enforcer = ContractEnforcer(strict=False)
        enforcer.check_step7_scores(_make_df(["drug_id"]))
        assert len(enforcer.violations) == 1
        enforcer.clear_log()
        assert len(enforcer.violations) == 0


# ---------------------------------------------------------------------------
# TestDefaultEnforcer
# ---------------------------------------------------------------------------

class TestDefaultEnforcer:
    def test_returns_same_instance(self):
        e1 = default_enforcer(strict=True)
        e2 = default_enforcer(strict=True)
        assert e1 is e2

    def test_strict_parameter_respected(self):
        e_strict = default_enforcer(strict=True)
        assert e_strict.strict is True
        e_soft = default_enforcer(strict=False)
        assert e_soft.strict is False
        # Switching strictness creates a new instance
        assert e_strict is not e_soft
