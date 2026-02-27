"""Unit tests for ops/merge_sig_to_bridge.py."""

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest

# Import the merge module from ops/
_OPS_DIR = Path(__file__).resolve().parents[3] / "ops"


@pytest.fixture()
def merge_mod():
    """Import merge_sig_to_bridge from ops/ directory."""
    sys.path.insert(0, str(_OPS_DIR))
    try:
        mod = importlib.import_module("merge_sig_to_bridge")
        yield mod
    finally:
        sys.path.pop(0)
        sys.modules.pop("merge_sig_to_bridge", None)


class TestMergeSigToBridge:
    """Tests for merge_sig_to_bridge.merge()."""

    def test_basic_merge(self, tmp_path, merge_mod):
        """Matched drugs get reversal_score; unmatched stay NaN."""
        bridge = pd.DataFrame([
            {"drug_id": "D001", "canonical_name": "Atorvastatin", "targets": "HMGCR"},
            {"drug_id": "D002", "canonical_name": "Metformin", "targets": "AMPK"},
            {"drug_id": "D003", "canonical_name": "Aspirin", "targets": "COX2"},
        ])
        sig = pd.DataFrame([
            {"drug": "atorvastatin", "final_reversal_score": -8.5},
            {"drug": "metformin", "final_reversal_score": -3.2},
            {"drug": "unknown_drug", "final_reversal_score": -1.1},
        ])
        bp = tmp_path / "bridge.csv"
        sp = tmp_path / "sig.csv"
        bridge.to_csv(bp, index=False)
        sig.to_csv(sp, index=False)

        merge_mod.merge(str(bp), str(sp))

        result = pd.read_csv(bp)
        assert "reversal_score" in result.columns
        assert result.loc[result["canonical_name"] == "Atorvastatin", "reversal_score"].iloc[0] == pytest.approx(-8.5)
        assert result.loc[result["canonical_name"] == "Metformin", "reversal_score"].iloc[0] == pytest.approx(-3.2)
        assert pd.isna(result.loc[result["canonical_name"] == "Aspirin", "reversal_score"].iloc[0])

    def test_case_insensitive_match(self, tmp_path, merge_mod):
        """Drug matching should be case-insensitive."""
        bridge = pd.DataFrame([{"drug_id": "D001", "canonical_name": "ATORVASTATIN", "targets": "T"}])
        sig = pd.DataFrame([{"drug": "atorvastatin", "final_reversal_score": -5.0}])
        bp = tmp_path / "bridge.csv"
        sp = tmp_path / "sig.csv"
        bridge.to_csv(bp, index=False)
        sig.to_csv(sp, index=False)

        merge_mod.merge(str(bp), str(sp))

        result = pd.read_csv(bp)
        assert result["reversal_score"].iloc[0] == pytest.approx(-5.0)

    def test_missing_sig_file_skips(self, tmp_path, merge_mod):
        """If sig-rank file doesn't exist, bridge is untouched."""
        bridge = pd.DataFrame([{"drug_id": "D001", "canonical_name": "Drug", "targets": "T"}])
        bp = tmp_path / "bridge.csv"
        bridge.to_csv(bp, index=False)

        merge_mod.merge(str(bp), str(tmp_path / "nonexistent.csv"))

        result = pd.read_csv(bp)
        assert "reversal_score" not in result.columns  # untouched

    def test_missing_bridge_file_exits(self, tmp_path, merge_mod):
        """If bridge file doesn't exist, sys.exit(1)."""
        with pytest.raises(SystemExit):
            merge_mod.merge(str(tmp_path / "no_bridge.csv"), str(tmp_path / "sig.csv"))

    def test_sig_missing_columns_skips(self, tmp_path, merge_mod):
        """If sig CSV lacks expected columns, skip merge gracefully."""
        bridge = pd.DataFrame([{"drug_id": "D001", "canonical_name": "Drug", "targets": "T"}])
        sig = pd.DataFrame([{"name": "x", "score": 1.0}])  # wrong columns
        bp = tmp_path / "bridge.csv"
        sp = tmp_path / "sig.csv"
        bridge.to_csv(bp, index=False)
        sig.to_csv(sp, index=False)

        merge_mod.merge(str(bp), str(sp))

        result = pd.read_csv(bp)
        assert "reversal_score" not in result.columns  # not merged

    def test_nan_scores_excluded(self, tmp_path, merge_mod):
        """NaN final_reversal_score should not be merged."""
        bridge = pd.DataFrame([{"drug_id": "D001", "canonical_name": "Drug", "targets": "T"}])
        sig = pd.DataFrame([{"drug": "drug", "final_reversal_score": float("nan")}])
        bp = tmp_path / "bridge.csv"
        sp = tmp_path / "sig.csv"
        bridge.to_csv(bp, index=False)
        sig.to_csv(sp, index=False)

        merge_mod.merge(str(bp), str(sp))

        result = pd.read_csv(bp)
        assert pd.isna(result["reversal_score"].iloc[0])

    def test_preserves_existing_columns(self, tmp_path, merge_mod):
        """Merge should not drop existing bridge columns."""
        bridge = pd.DataFrame([
            {"drug_id": "D001", "canonical_name": "Drug", "targets": "T", "max_mechanism_score": "3.5"},
        ])
        sig = pd.DataFrame([{"drug": "drug", "final_reversal_score": -2.0}])
        bp = tmp_path / "bridge.csv"
        sp = tmp_path / "sig.csv"
        bridge.to_csv(bp, index=False)
        sig.to_csv(sp, index=False)

        merge_mod.merge(str(bp), str(sp))

        result = pd.read_csv(bp)
        assert "max_mechanism_score" in result.columns
        assert "targets" in result.columns
        assert result["reversal_score"].iloc[0] == pytest.approx(-2.0)
