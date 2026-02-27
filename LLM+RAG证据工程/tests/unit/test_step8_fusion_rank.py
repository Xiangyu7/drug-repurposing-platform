"""Unit tests for Step8 candidate pack script."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts import step8_fusion_rank as step8
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
                    "max_mechanism_score": "4.2",
                    "reversal_score": "-7.5",
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
                "step8_fusion_rank.py",
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
        candidate_xlsx = outdir / "step8_fusion_rank_report.xlsx"
        one_pager_md = outdir / "step8_one_pagers_top1.md"
        manifest = outdir / "step8_manifest.json"

        assert shortlist_csv.exists()
        assert candidate_xlsx.exists()
        assert one_pager_md.exists()
        assert manifest.exists()

        shortlist_df = pd.read_csv(shortlist_csv)
        assert "contract_version" in shortlist_df.columns
        assert shortlist_df["contract_version"].iloc[0] == STEP8_SHORTLIST_VERSION
        # Upstream scores loaded from bridge
        assert "mechanism_score" in shortlist_df.columns
        assert "reversal_score" in shortlist_df.columns
        assert float(shortlist_df["mechanism_score"].iloc[0]) == pytest.approx(4.2)
        assert float(shortlist_df["reversal_score"].iloc[0]) == pytest.approx(-7.5)
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
                "step8_fusion_rank.py",
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
                "step8_fusion_rank.py",
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


class TestSensitivityAnalysis:
    """Tests for _sensitivity_analysis() Monte Carlo weight perturbation."""

    def _make_df(self, drugs):
        """Build a DataFrame that mimics rank_key-ready data for SA tests."""
        rows = []
        for d in drugs:
            rows.append({
                "canonical_name": d["name"],
                "drug_id": d.get("drug_id", d["name"]),
                "unique_supporting_pmids_count": d.get("pmids", 5),
                "topic_match_ratio_filled": d.get("topic", 0.5),
                "total_score_0_100": d.get("llm_score", 50.0),
                "novelty_score": d.get("novelty", 0.5),
                "mechanism_score": d.get("mech", 0.0),
                "reversal_score": d.get("rev", 0.0),
                "uncertainty_score": d.get("unc", 0.0),
                "harm_or_neutral_sentence_count": d.get("harm", 0),
                "neg_trials_n": d.get("neg", 0),
                "safety_blacklist_hit": d.get("bl", 0),
                "rank_key": d.get("rank_key", 30.0),
            })
        return pd.DataFrame(rows)

    def test_dominant_drug_has_high_stability(self):
        """A drug that clearly dominates should have top_k_stability > 0.9."""
        drugs = [
            {"name": "dominant", "pmids": 30, "llm_score": 95, "mech": 5.0,
             "rev": -9.0, "novelty": 0.9, "rank_key": 80.0},
            {"name": "medium", "pmids": 5, "llm_score": 50, "mech": 2.0,
             "rev": -3.0, "novelty": 0.4, "rank_key": 40.0},
            {"name": "weak", "pmids": 1, "llm_score": 20, "mech": 0.0,
             "rev": 0.0, "novelty": 0.1, "rank_key": 10.0},
        ]
        df = self._make_df(drugs)
        result = step8._sensitivity_analysis(df, "origin", topk=1, n_iter=200, seed=42)

        assert len(result) == 3
        dominant = result[result["canonical_name"] == "dominant"].iloc[0]
        assert dominant["top_k_stability"] > 0.9
        assert dominant["baseline_rank"] == 1

    def test_similar_drugs_have_lower_stability(self):
        """Two drugs with close but slightly different scores: the weaker one
        should have lower top-1 stability than the dominant drug in
        test_dominant_drug_has_high_stability."""
        # drug_a edges out drug_b slightly: higher LLM score but lower mech.
        # Weight perturbations should sometimes swap their order.
        drugs = [
            {"name": "drug_a", "pmids": 10, "llm_score": 62, "mech": 2.8,
             "rev": -4.0, "novelty": 0.5, "rank_key": 45.0},
            {"name": "drug_b", "pmids": 10, "llm_score": 58, "mech": 3.2,
             "rev": -4.0, "novelty": 0.5, "rank_key": 44.0},
            {"name": "weak", "pmids": 1, "llm_score": 10, "mech": 0.0,
             "rev": 0.0, "novelty": 0.1, "rank_key": 5.0},
        ]
        df = self._make_df(drugs)
        result = step8._sensitivity_analysis(df, "origin", topk=1, n_iter=500, seed=42)

        # Neither drug should dominate top-1 as reliably as a truly dominant drug
        a_stab = result[result["canonical_name"] == "drug_a"]["top_k_stability"].iloc[0]
        b_stab = result[result["canonical_name"] == "drug_b"]["top_k_stability"].iloc[0]
        # At least one of the two should NOT be stable at top-1
        # (their scores are close enough that perturbations swap order sometimes)
        assert min(a_stab, b_stab) < 0.95

    def test_output_schema(self):
        """SA output should have the expected columns."""
        drugs = [
            {"name": "d1", "rank_key": 50.0},
            {"name": "d2", "rank_key": 30.0},
        ]
        df = self._make_df(drugs)
        result = step8._sensitivity_analysis(df, "origin", topk=2, n_iter=50, seed=1)

        expected_cols = {
            "canonical_name", "drug_id", "baseline_rank", "mean_rank",
            "rank_std", "rank_95ci_lower", "rank_95ci_upper",
            "top_k_stability", "n_perturbations",
        }
        assert expected_cols == set(result.columns)
        assert (result["n_perturbations"] == 50).all()

    def test_single_drug_returns_empty(self):
        """With only 1 drug, SA should return an empty DataFrame."""
        df = self._make_df([{"name": "solo", "rank_key": 50.0}])
        result = step8._sensitivity_analysis(df, "origin", topk=1, n_iter=100, seed=42)
        assert result.empty

    def test_cross_route_boosts_novelty(self):
        """Cross route should boost novelty weight via novelty_boost=1.3."""
        drugs = [
            {"name": "novel", "pmids": 5, "llm_score": 40, "mech": 0.0,
             "rev": 0.0, "novelty": 0.95, "rank_key": 50.0},
            {"name": "known", "pmids": 5, "llm_score": 40, "mech": 0.0,
             "rev": 0.0, "novelty": 0.05, "rank_key": 30.0},
        ]
        df = self._make_df(drugs)
        result_cross = step8._sensitivity_analysis(df, "cross", topk=1, n_iter=200, seed=42)
        result_origin = step8._sensitivity_analysis(df, "origin", topk=1, n_iter=200, seed=42)

        # In cross route, the novel drug should be even more stable at top-1
        novel_cross = result_cross[result_cross["canonical_name"] == "novel"]["top_k_stability"].iloc[0]
        novel_origin = result_origin[result_origin["canonical_name"] == "novel"]["top_k_stability"].iloc[0]
        assert novel_cross >= novel_origin

    def test_sa_integration_via_main(self, tmp_path, monkeypatch):
        """End-to-end: --sensitivity_n produces CSV and Excel sheet."""
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        bridge_dir = tmp_path / "bridge"
        dossiers_dir = tmp_path / "dossiers"
        for d in [step7_dir, bridge_dir, dossiers_dir]:
            d.mkdir(exist_ok=True)

        cards = []
        bridge_rows = []
        for i, (name, score, mech, rev) in enumerate([
            ("strong_drug", 85, 5.0, -8.0),
            ("medium_drug", 55, 2.0, -3.0),
            ("weak_drug", 25, 0.5, -0.5),
        ]):
            did = f"D{i:03d}"
            dp = dossiers_dir / f"{did}_{name}.json"
            dp.write_text(json.dumps({
                "drug_id": did, "canonical_name": name,
                "llm_structured": {
                    "supporting_evidence": [{"pmid": "12345678", "claim": "ok"}],
                    "harm_or_neutral_evidence": [],
                    "proposed_mechanisms": ["mechanism"],
                    "key_risks": [],
                },
            }), encoding="utf-8")
            cards.append({
                "drug_id": did, "canonical_name": name,
                "gate_decision": "GO",
                "scores": {"total_score_0_100": score},
                "dossier_path": str(dp),
            })
            bridge_rows.append({
                "drug_id": did, "targets": "Target",
                "target_details": json.dumps([{
                    "target_chembl_id": "CHEMBL0001", "target_name": "T",
                    "mechanism_of_action": "inhibitor", "uniprot": "P00000",
                    "pdb_ids": ["1ABC"], "pdb_count": 1,
                    "has_alphafold": False, "structure_source": "PDB",
                }]),
                "max_mechanism_score": str(mech),
                "reversal_score": str(rev),
            })

        (step7_dir / "step7_cards.json").write_text(json.dumps(cards), encoding="utf-8")
        pd.DataFrame(bridge_rows).to_csv(bridge_dir / "bridge.csv", index=False)
        pd.DataFrame(columns=["drug_raw", "nctId"]).to_csv(tmp_path / "neg.csv", index=False)

        monkeypatch.setattr(sys, "argv", [
            "step8_fusion_rank.py",
            "--step7_dir", str(step7_dir),
            "--neg", str(tmp_path / "neg.csv"),
            "--bridge", str(bridge_dir / "bridge.csv"),
            "--outdir", str(outdir),
            "--topk", "3",
            "--sensitivity_n", "100",
            "--sensitivity_seed", "42",
        ])
        step8.main()

        # SA CSV should exist
        sa_csv = outdir / "step8_sensitivity_analysis.csv"
        assert sa_csv.exists()
        sa_df = pd.read_csv(sa_csv)
        assert len(sa_df) == 3
        assert "top_k_stability" in sa_df.columns

        # Excel should have Sensitivity sheet
        import openpyxl
        wb = openpyxl.load_workbook(outdir / "step8_fusion_rank_report.xlsx")
        assert "Sensitivity" in wb.sheetnames
        wb.close()

        # Manifest should reference SA CSV in outputs.files
        manifest = json.loads((outdir / "step8_manifest.json").read_text())
        output_paths = list(manifest.get("outputs", {}).get("files", {}).keys())
        assert any("sensitivity" in p for p in output_paths)


class TestFusionRanking:
    """Tests for the multi-source fusion ranking formula in step8."""

    def _make_test_data(self, tmp_path, drugs):
        """Build step7 cards, bridge CSV, and neg CSV for ranking tests.

        *drugs* is a list of dicts, each with keys:
            drug_id, canonical_name, total_score, mechanism_score, reversal_score
        """
        step7_dir = tmp_path / "step7"
        outdir = tmp_path / "step8"
        bridge_dir = tmp_path / "bridge"
        dossiers_dir = tmp_path / "dossiers"
        for d in [step7_dir, bridge_dir, dossiers_dir]:
            d.mkdir(exist_ok=True)

        cards = []
        bridge_rows = []
        for drug in drugs:
            did = drug["drug_id"]
            name = drug["canonical_name"]
            dossier_path = dossiers_dir / f"{did}_{name}.json"
            dossier_path.write_text(json.dumps({
                "drug_id": did,
                "canonical_name": name,
                "llm_structured": {
                    "supporting_evidence": [{"pmid": "12345678", "claim": "benefit"}],
                    "harm_or_neutral_evidence": [],
                    "proposed_mechanisms": ["mechanism"],
                    "key_risks": [],
                },
            }), encoding="utf-8")
            cards.append({
                "drug_id": did,
                "canonical_name": name,
                "gate_decision": "GO",
                "scores": {"total_score_0_100": drug.get("total_score", 50.0)},
                "novelty_score": drug.get("novelty_score", 0.5),
                "dossier_path": str(dossier_path),
            })
            bridge_rows.append({
                "drug_id": did,
                "targets": "Target",
                "target_details": json.dumps([{
                    "target_chembl_id": "CHEMBL0001",
                    "target_name": "Target",
                    "mechanism_of_action": "inhibitor",
                    "uniprot": "P00000",
                    "pdb_ids": ["1ABC"],
                    "pdb_count": 1,
                    "has_alphafold": False,
                    "structure_source": "PDB",
                }]),
                "max_mechanism_score": str(drug.get("mechanism_score", 0.0)),
                "reversal_score": str(drug.get("reversal_score", 0.0)),
            })

        (step7_dir / "step7_cards.json").write_text(json.dumps(cards), encoding="utf-8")
        bridge_path = bridge_dir / "bridge.csv"
        pd.DataFrame(bridge_rows).to_csv(bridge_path, index=False)
        neg_path = tmp_path / "neg.csv"
        pd.DataFrame(columns=["drug_raw", "nctId"]).to_csv(neg_path, index=False)

        return step7_dir, bridge_path, neg_path, outdir

    def test_mechanism_score_affects_ranking(self, tmp_path, monkeypatch):
        """Drug with higher mechanism_score should rank higher (all else equal)."""
        drugs = [
            {"drug_id": "D001", "canonical_name": "low_mech", "total_score": 50,
             "mechanism_score": 1.0, "reversal_score": 0.0},
            {"drug_id": "D002", "canonical_name": "high_mech", "total_score": 50,
             "mechanism_score": 5.0, "reversal_score": 0.0},
        ]
        step7_dir, bridge_path, neg_path, outdir = self._make_test_data(tmp_path, drugs)

        monkeypatch.setattr(sys, "argv", [
            "step8_fusion_rank.py",
            "--step7_dir", str(step7_dir),
            "--neg", str(neg_path),
            "--bridge", str(bridge_path),
            "--outdir", str(outdir),
            "--topk", "2",
        ])
        step8.main()

        df = pd.read_csv(outdir / "step8_shortlist_top2.csv")
        # high_mech should be ranked first
        assert df.iloc[0]["canonical_name"] == "high_mech"
        assert float(df.iloc[0]["mechanism_score"]) > float(df.iloc[1]["mechanism_score"])

    def test_reversal_score_affects_ranking(self, tmp_path, monkeypatch):
        """Drug with more negative reversal_score (stronger reversal) should rank higher."""
        drugs = [
            {"drug_id": "D001", "canonical_name": "weak_rev", "total_score": 50,
             "mechanism_score": 3.0, "reversal_score": -1.0},
            {"drug_id": "D002", "canonical_name": "strong_rev", "total_score": 50,
             "mechanism_score": 3.0, "reversal_score": -9.0},
        ]
        step7_dir, bridge_path, neg_path, outdir = self._make_test_data(tmp_path, drugs)

        monkeypatch.setattr(sys, "argv", [
            "step8_fusion_rank.py",
            "--step7_dir", str(step7_dir),
            "--neg", str(neg_path),
            "--bridge", str(bridge_path),
            "--outdir", str(outdir),
            "--topk", "2",
        ])
        step8.main()

        df = pd.read_csv(outdir / "step8_shortlist_top2.csv")
        # strong_rev (more negative) should rank higher
        assert df.iloc[0]["canonical_name"] == "strong_rev"

    def test_rank_key_includes_all_components(self, tmp_path, monkeypatch):
        """rank_key should be > 0 and reflect combined scoring."""
        drugs = [
            {"drug_id": "D001", "canonical_name": "drug_a", "total_score": 80,
             "mechanism_score": 4.0, "reversal_score": -6.0},
        ]
        step7_dir, bridge_path, neg_path, outdir = self._make_test_data(tmp_path, drugs)

        monkeypatch.setattr(sys, "argv", [
            "step8_fusion_rank.py",
            "--step7_dir", str(step7_dir),
            "--neg", str(neg_path),
            "--bridge", str(bridge_path),
            "--outdir", str(outdir),
            "--topk", "1",
        ])
        step8.main()

        df = pd.read_csv(outdir / "step8_shortlist_top1.csv")
        rank_key = float(df["rank_key"].iloc[0])
        # With LLM_score=80*0.35=28, pmid_score>0, mechanism+reversal contrib,
        # rank_key should be substantial (> 30)
        assert rank_key > 30.0

    def test_zero_upstream_scores_still_works(self, tmp_path, monkeypatch):
        """Drugs with zero mechanism/reversal scores should still rank correctly."""
        drugs = [
            {"drug_id": "D001", "canonical_name": "no_upstream", "total_score": 70,
             "mechanism_score": 0.0, "reversal_score": 0.0},
            {"drug_id": "D002", "canonical_name": "has_upstream", "total_score": 70,
             "mechanism_score": 3.0, "reversal_score": -5.0},
        ]
        step7_dir, bridge_path, neg_path, outdir = self._make_test_data(tmp_path, drugs)

        monkeypatch.setattr(sys, "argv", [
            "step8_fusion_rank.py",
            "--step7_dir", str(step7_dir),
            "--neg", str(neg_path),
            "--bridge", str(bridge_path),
            "--outdir", str(outdir),
            "--topk", "2",
        ])
        step8.main()

        df = pd.read_csv(outdir / "step8_shortlist_top2.csv")
        # has_upstream should rank higher due to positive upstream scores
        assert df.iloc[0]["canonical_name"] == "has_upstream"
        # Both should have valid rank_key values
        assert all(pd.notna(df["rank_key"]))
