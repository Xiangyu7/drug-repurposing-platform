"""Unit tests for Step9 validation plan script."""

import sys
from pathlib import Path

import pandas as pd

from scripts import step9_validation_plan as step9
from src.dr.contracts import STEP8_SHORTLIST_VERSION


class TestStep9ValidationPlan:
    def test_pick_shortlist_prefers_latest(self, tmp_path):
        s1 = tmp_path / "step8_shortlist_top3.csv"
        s2 = tmp_path / "step8_shortlist_top5.csv"
        s1.write_text("a\n1\n", encoding="utf-8")
        s2.write_text("a\n2\n", encoding="utf-8")

        picked = step9._pick_shortlist(tmp_path)
        assert picked == s2

    def test_main_generates_outputs(self, tmp_path, monkeypatch):
        step8_dir = tmp_path / "step8"
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step9"
        step8_dir.mkdir()
        step7_dir.mkdir()

        shortlist = pd.DataFrame(
            [
                {
                    "drug_id": "D001",
                    "canonical_name": "resveratrol",
                    "gate": "GO",
                    "endpoint_type": "PLAQUE_IMAGING",
                    "total_score_0_100": 82.5,
                    "safety_blacklist_hit": False,
                    "supporting_sentence_count": 8,
                    "rank_key": 123.0,
                    "unique_supporting_pmids_count": 6,
                    "harm_or_neutral_sentence_count": 1,
                    "topic_match_ratio": 0.9,
                    "neg_trials_n": 0,
                    "dossier_json": "/tmp/d001.json",
                    "dossier_md": "/tmp/d001.md",
                    "docking_primary_target_chembl_id": "CHEMBL2147",
                    "docking_primary_target_name": "Sirtuin 1",
                    "docking_primary_uniprot": "Q96EB6",
                    "docking_primary_structure_source": "PDB+AlphaFold",
                    "docking_primary_structure_provider": "PDB",
                    "docking_primary_structure_id": "4I5I",
                    "alphafold_structure_id": "AF-Q96EB6-F1",
                    "docking_backup_targets_json": "[]",
                    "docking_feasibility_tier": "READY_PDB",
                    "docking_target_selection_score": 0.95,
                    "docking_risk_flags": "",
                    "docking_policy_version": "v1",
                    "mechanism_score": 4.2,
                    "reversal_score": -7.5,
                    "contract_version": STEP8_SHORTLIST_VERSION,
                },
                {
                    "drug_id": "D002",
                    "canonical_name": "drug_b",
                    "gate": "MAYBE",
                    "endpoint_type": "BIOMARKER",
                    "total_score_0_100": 55.0,
                    "safety_blacklist_hit": False,
                    "supporting_sentence_count": 3,
                    "rank_key": 100.0,
                    "unique_supporting_pmids_count": 2,
                    "harm_or_neutral_sentence_count": 3,
                    "topic_match_ratio": 0.6,
                    "neg_trials_n": 1,
                    "dossier_json": "/tmp/d002.json",
                    "dossier_md": "/tmp/d002.md",
                    "docking_primary_target_chembl_id": "",
                    "docking_primary_target_name": "",
                    "docking_primary_uniprot": "",
                    "docking_primary_structure_source": "none",
                    "docking_primary_structure_provider": "none",
                    "docking_primary_structure_id": "",
                    "alphafold_structure_id": "",
                    "docking_backup_targets_json": "[]",
                    "docking_feasibility_tier": "NO_STRUCTURE",
                    "docking_target_selection_score": 0.0,
                    "docking_risk_flags": "NO_TARGET_DETAILS",
                    "docking_policy_version": "v1",
                    "mechanism_score": 0.0,
                    "reversal_score": 0.0,
                    "contract_version": STEP8_SHORTLIST_VERSION,
                },
            ]
        )
        shortlist_path = step8_dir / "step8_shortlist_top2.csv"
        shortlist.to_csv(shortlist_path, index=False)

        step7_plan = pd.DataFrame(
            [
                {
                    "drug_id": "D001",
                    "canonical_name": "resveratrol",
                    "validation_stage": "in_vivo",
                    "timeline_weeks": 5,
                    "priority": 1,
                }
            ]
        )
        step7_plan.to_csv(step7_dir / "step7_validation_plan.csv", index=False)

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "step9_validation_plan.py",
                "--step8_dir",
                str(step8_dir),
                "--step7_dir",
                str(step7_dir),
                "--outdir",
                str(outdir),
                "--target_disease",
                "atherosclerosis",
            ],
        )
        step9.main()

        plan_csv = outdir / "step9_validation_plan.csv"
        plan_md = outdir / "step9_validation_plan.md"
        manifest = outdir / "step9_manifest.json"
        assert plan_csv.exists()
        assert plan_md.exists()
        assert manifest.exists()

        out_df = pd.read_csv(plan_csv)
        assert len(out_df) == 2
        assert "priority_tier" in out_df.columns
        assert "contract_version" in out_df.columns
        assert out_df.loc[out_df["drug_id"] == "D001", "recommended_stage"].iloc[0] == "in_vivo"
