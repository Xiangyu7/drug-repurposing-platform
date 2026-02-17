"""Unit tests for pipeline data contracts."""

from src.dr.contracts import (
    STEP6_DOSSIER_SCHEMA,
    STEP6_DOSSIER_VERSION,
    STEP8_SHORTLIST_VERSION,
    stamp_step6_dossier_contract,
    validate_contract_version_values,
    validate_step6_dossier,
    validate_step7_cards,
    validate_step7_scores_columns,
    validate_step7_gating_columns,
    validate_step8_shortlist_columns,
    validate_step9_plan_columns,
)


def _valid_step6_dossier():
    return {
        "drug_id": "D001",
        "canonical_name": "resveratrol",
        "target_disease": "atherosclerosis",
        "endpoint_type": "PLAQUE_IMAGING",
        "pubmed_rag": {
            "top_abstracts": [],
            "top_sentences": [],
        },
        "llm_structured": {
            "supporting_evidence": [],
            "harm_or_neutral_evidence": [],
            "counts": {
                "supporting_evidence_count": 0,
                "supporting_sentence_count": 0,
                "unique_supporting_pmids_count": 0,
                "harm_or_neutral_count": 0,
            },
        },
    }


class TestContracts:
    def test_stamp_and_validate_step6_dossier(self):
        dossier = _valid_step6_dossier()
        stamp_step6_dossier_contract(dossier, producer="unit-test")
        issues = validate_step6_dossier(dossier)
        assert issues == []
        assert dossier["_contract"]["schema"] == STEP6_DOSSIER_SCHEMA
        assert dossier["_contract"]["version"] == STEP6_DOSSIER_VERSION

    def test_validate_step6_reports_missing_keys(self):
        dossier = {"drug_id": "D001"}
        issues = validate_step6_dossier(dossier)
        assert any("missing top-level key: canonical_name" in x for x in issues)
        assert any("pubmed_rag must be a dict" in x for x in issues)

    def test_validate_step6_requires_contract_when_enabled(self):
        dossier = _valid_step6_dossier()
        issues = validate_step6_dossier(dossier, require_contract=True)
        assert any("missing _contract metadata" in x for x in issues)

    def test_validate_step7_scores_columns(self):
        columns = {
            "drug_id",
            "canonical_name",
            "evidence_strength_0_30",
            "mechanism_plausibility_0_20",
            "translatability_0_20",
            "safety_fit_0_20",
            "practicality_0_10",
            "total_score_0_100",
        }
        assert validate_step7_scores_columns(columns) == []
        assert validate_step7_scores_columns({"drug_id"}) != []

    def test_validate_step7_gating_columns(self):
        columns = {
            "drug_id",
            "canonical_name",
            "gate_decision",
            "gate_reasons",
            "total_score",
            "benefit",
            "harm",
            "neutral",
            "total_pmids",
        }
        assert validate_step7_gating_columns(columns) == []
        assert validate_step7_gating_columns({"drug_id"}) != []

    def test_validate_step7_cards(self):
        cards = [
            {
                "drug_id": "D001",
                "canonical_name": "resveratrol",
                "gate_decision": "GO",
                "scores": {"total_score_0_100": 80.0},
                "dossier_path": "output/step6/dossiers/D001.json",
            }
        ]
        assert validate_step7_cards(cards) == []
        assert validate_step7_cards({"not": "a-list"}) != []

    def test_validate_step8_shortlist_columns(self):
        columns = {
            "canonical_name",
            "drug_id",
            "gate",
            "endpoint_type",
            "total_score_0_100",
            "safety_blacklist_hit",
            "supporting_sentence_count",
            "unique_supporting_pmids_count",
            "harm_or_neutral_sentence_count",
            "topic_match_ratio",
            "neg_trials_n",
            "dossier_json",
            "dossier_md",
            "rank_key",
            "docking_primary_target_chembl_id",
            "docking_primary_target_name",
            "docking_primary_uniprot",
            "docking_primary_structure_source",
            "docking_primary_structure_provider",
            "docking_primary_structure_id",
            "docking_backup_targets_json",
            "docking_feasibility_tier",
            "docking_target_selection_score",
            "docking_risk_flags",
            "docking_policy_version",
            "contract_version",
        }
        assert (
            validate_step8_shortlist_columns(
                columns,
                require_contract_version=True,
            )
            == []
        )
        assert validate_step8_shortlist_columns({"drug_id"}) != []

    def test_validate_step9_plan_columns(self):
        columns = {
            "rank",
            "drug_id",
            "canonical_name",
            "gate",
            "priority_tier",
            "recommended_stage",
            "timeline_weeks",
            "target_disease",
            "endpoint_type",
            "total_score_0_100",
            "primary_readouts",
            "stop_go_criteria",
            "evidence_gap",
            "owner",
            "shortlist_source",
            "contract_version",
        }
        assert validate_step9_plan_columns(columns) == []
        assert validate_step9_plan_columns({"drug_id"}) != []

    def test_validate_contract_version_values(self):
        ok = validate_contract_version_values(
            [STEP8_SHORTLIST_VERSION, STEP8_SHORTLIST_VERSION],
            expected_version=STEP8_SHORTLIST_VERSION,
            label="step8.contract_version",
        )
        bad = validate_contract_version_values(
            ["0.9.0"],
            expected_version=STEP8_SHORTLIST_VERSION,
            label="step8.contract_version",
        )
        assert ok == []
        assert bad != []
