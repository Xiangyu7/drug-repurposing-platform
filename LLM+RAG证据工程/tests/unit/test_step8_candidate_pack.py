"""Unit tests for Step8 candidate pack script."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts import step8_candidate_pack as step8
from src.dr.contracts import STEP8_SHORTLIST_VERSION


class TestStep8CandidatePack:
    def test_main_generates_outputs(self, tmp_path, monkeypatch):
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        data_dir = tmp_path / "data"
        bridge_dir = tmp_path / "kg"
        dossiers_dir = tmp_path / "dossiers"
        step7_dir.mkdir()
        data_dir.mkdir()
        bridge_dir.mkdir()
        dossiers_dir.mkdir()

        dossier_path = dossiers_dir / "D001_resveratrol.json"
        dossier_payload = {
            "drug_id": "D001",
            "canonical_name": "resveratrol",
            "llm_structured": {
                "supporting_evidence": [{"pmid": "12345678", "claim": "benefit signal"}],
                "harm_or_neutral_evidence": [{"pmid": "87654321", "claim": "neutral"}],
                "proposed_mechanisms": ["SIRT1 activation"],
                "key_risks": ["bleeding risk"],
                "qc_summary": {"topic_match_ratio": 0.8},
            },
        }
        dossier_path.write_text(json.dumps(dossier_payload), encoding="utf-8")

        cards = [
            {
                "drug_id": "D001",
                "canonical_name": "resveratrol",
                "gate_decision": "GO",
                "scores": {"total_score_0_100": 81.2},
                "dossier_path": str(dossier_path),
            }
        ]
        (step7_dir / "step7_cards.json").write_text(json.dumps(cards), encoding="utf-8")

        neg_df = pd.DataFrame(
            [
                {
                    "drug_raw": "resveratrol",
                    "nctId": "NCT00000001",
                    "primary_outcome_pvalues": "p=0.7",
                }
            ]
        )
        neg_path = data_dir / "poolA_negative_drug_level.csv"
        neg_df.to_csv(neg_path, index=False)

        bridge_payload = pd.DataFrame(
            [
                {
                    "drug_id": "D001",
                    "targets": "Sirtuin 1 (CHEMBL2147) [UniProt:Q96EB6] [PDB+AlphaFold] — activator",
                    "target_details": json.dumps(
                        [
                            {
                                "target_chembl_id": "CHEMBL2147",
                                "target_name": "Sirtuin 1",
                                "mechanism_of_action": "Sirtuin 1 activator",
                                "uniprot": "Q96EB6",
                                "pdb_ids": ["4I5I", "5BTR"],
                                "pdb_count": 2,
                                "has_alphafold": True,
                                "structure_source": "PDB+AlphaFold",
                            },
                            {
                                "target_chembl_id": "CHEMBL0000",
                                "target_name": "Backup target",
                                "mechanism_of_action": "Backup mechanism",
                                "uniprot": "P12345",
                                "pdb_ids": [],
                                "pdb_count": 0,
                                "has_alphafold": True,
                                "structure_source": "AlphaFold_only",
                            },
                        ]
                    ),
                }
            ]
        )
        bridge_path = bridge_dir / "bridge_origin_reassess.csv"
        bridge_payload.to_csv(bridge_path, index=False)

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "step8_candidate_pack.py",
                "--step7_dir",
                str(step7_dir),
                "--neg",
                str(neg_path),
                "--bridge",
                str(bridge_path),
                "--outdir",
                str(outdir),
                "--topk",
                "1",
            ],
        )
        step8.main()

        shortlist_csv = outdir / "step8_shortlist_top1.csv"
        candidate_xlsx = outdir / "step8_candidate_pack_from_step7.xlsx"
        one_pager_md = outdir / "step8_one_pagers_top1.md"
        manifest = outdir / "step8_manifest.json"

        assert shortlist_csv.exists()
        assert candidate_xlsx.exists()
        assert one_pager_md.exists()
        assert manifest.exists()

        shortlist_df = pd.read_csv(shortlist_csv)
        assert "contract_version" in shortlist_df.columns
        assert shortlist_df["contract_version"].iloc[0] == STEP8_SHORTLIST_VERSION
        assert shortlist_df["docking_primary_target_chembl_id"].iloc[0] == "CHEMBL2147"
        assert shortlist_df["docking_primary_structure_provider"].iloc[0] == "PDB"
        assert shortlist_df["docking_feasibility_tier"].iloc[0] == "READY_PDB"
        assert "docking_backup_targets_json" in shortlist_df.columns
        backup = json.loads(shortlist_df["docking_backup_targets_json"].iloc[0])
        assert len(backup) >= 1

    def test_strict_contract_rejects_invalid_cards(self, tmp_path, monkeypatch):
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        step7_dir.mkdir()

        invalid_cards = [{"drug_id": "D001", "canonical_name": "resveratrol"}]
        (step7_dir / "step7_cards.json").write_text(json.dumps(invalid_cards), encoding="utf-8")

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "step8_candidate_pack.py",
                "--step7_dir",
                str(step7_dir),
                "--outdir",
                str(outdir),
                "--strict_contract",
                "1",
            ],
        )
        with pytest.raises(ValueError):
            step8.main()

    def test_malformed_target_details_downgrades_non_blocking(self, tmp_path, monkeypatch):
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        bridge_dir = tmp_path / "kg"
        dossiers_dir = tmp_path / "dossiers"
        step7_dir.mkdir()
        bridge_dir.mkdir()
        dossiers_dir.mkdir()

        dossier_path = dossiers_dir / "D001_resveratrol.json"
        dossier_path.write_text(
            json.dumps(
                {
                    "drug_id": "D001",
                    "canonical_name": "resveratrol",
                    "llm_structured": {
                        "supporting_evidence": [],
                        "harm_or_neutral_evidence": [],
                        "proposed_mechanisms": ["SIRT1 activation"],
                        "key_risks": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        cards = [
            {
                "drug_id": "D001",
                "canonical_name": "resveratrol",
                "gate_decision": "GO",
                "scores": {"total_score_0_100": 81.2},
                "dossier_path": str(dossier_path),
            }
        ]
        (step7_dir / "step7_cards.json").write_text(json.dumps(cards), encoding="utf-8")

        bridge_path = bridge_dir / "bridge_origin_reassess.csv"
        pd.DataFrame(
            [{"drug_id": "D001", "targets": "bad", "target_details": "{not-json"}]
        ).to_csv(bridge_path, index=False)

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "step8_candidate_pack.py",
                "--step7_dir",
                str(step7_dir),
                "--bridge",
                str(bridge_path),
                "--outdir",
                str(outdir),
                "--topk",
                "1",
                "--docking_block_on_no_pdb",
                "0",
            ],
        )
        step8.main()

        shortlist_df = pd.read_csv(outdir / "step8_shortlist_top1.csv")
        assert shortlist_df["docking_feasibility_tier"].iloc[0] == "NO_STRUCTURE"
        assert "MALFORMED_TARGET_DETAILS" in str(shortlist_df["docking_risk_flags"].iloc[0])

    def test_empty_target_details_non_blocking(self):
        result = step8._select_docking_targets(
            target_details_raw="",
            mechanism_context="SIRT1 lipid plaque",
            endpoint_type="LIPID",
            primary_n=1,
            backup_n=2,
            structure_policy="pdb_first",
        )
        assert result["docking_feasibility_tier"] == "NO_STRUCTURE"
        assert "NO_TARGET_DETAILS" in result["docking_risk_flags"]

    def test_target_selection_stable_tie_break(self):
        payload = json.dumps(
            [
                {
                    "target_chembl_id": "CHEMBL200",
                    "target_name": "Target B",
                    "mechanism_of_action": "",
                    "uniprot": "P12345",
                    "pdb_ids": ["1ABC"],
                    "pdb_count": 1,
                    "has_alphafold": False,
                    "structure_source": "PDB",
                },
                {
                    "target_chembl_id": "CHEMBL100",
                    "target_name": "Target A",
                    "mechanism_of_action": "",
                    "uniprot": "Q54321",
                    "pdb_ids": ["2DEF"],
                    "pdb_count": 1,
                    "has_alphafold": False,
                    "structure_source": "PDB",
                },
            ]
        )
        result1 = step8._select_docking_targets(
            target_details_raw=payload,
            mechanism_context="no overlap context",
            endpoint_type="OTHER",
            primary_n=1,
            backup_n=2,
            structure_policy="pdb_first",
        )
        result2 = step8._select_docking_targets(
            target_details_raw=payload,
            mechanism_context="no overlap context",
            endpoint_type="OTHER",
            primary_n=1,
            backup_n=2,
            structure_policy="pdb_first",
        )
        assert result1["docking_primary_target_chembl_id"] == "CHEMBL100"
        assert result2["docking_primary_target_chembl_id"] == "CHEMBL100"
