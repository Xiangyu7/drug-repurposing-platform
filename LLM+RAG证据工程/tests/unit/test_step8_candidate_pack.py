"""Unit tests for Step8 candidate pack script."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts import step8_candidate_pack as step8


class TestStep8CandidatePack:
    def test_main_generates_outputs(self, tmp_path, monkeypatch):
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        data_dir = tmp_path / "data"
        dossiers_dir = tmp_path / "dossiers"
        step7_dir.mkdir()
        data_dir.mkdir()
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

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "step8_candidate_pack.py",
                "--step7_dir",
                str(step7_dir),
                "--neg",
                str(neg_path),
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
        assert shortlist_df["contract_version"].iloc[0] == "1.0.0"

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
