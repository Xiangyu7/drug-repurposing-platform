"""Test suite for data leakage audit.

Tests cover:
- audit_drug_overlap: no overlap, partial, full, empty sets
- audit_disease_overlap: same patterns
- audit_pair_overlap: clean vs leaked, drug overlap without pair overlap
- generate_leakage_report: passed/failed, recommendations
- save_leakage_report: JSON output validation
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from kg_explain.evaluation.leakage_audit import (
    audit_disease_overlap,
    audit_drug_overlap,
    audit_pair_overlap,
    generate_leakage_report,
    save_leakage_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_df(drugs, diseases):
    """Create a minimal DataFrame with drug_normalized and diseaseId columns."""
    return pd.DataFrame({"drug_normalized": drugs, "diseaseId": diseases})


# ---------------------------------------------------------------------------
# TestAuditDrugOverlap
# ---------------------------------------------------------------------------
class TestAuditDrugOverlap:
    """Tests for audit_drug_overlap."""

    def test_no_overlap(self):
        train = _make_df(["aspirin", "metformin"], ["D1", "D2"])
        test = _make_df(["atorvastatin", "lisinopril"], ["D3", "D4"])
        result = audit_drug_overlap(train, test)
        assert result["overlap_count"] == 0
        assert result["overlap_ratio"] == 0.0
        assert result["overlap_drugs"] == []
        assert sorted(result["test_only_drugs"]) == ["atorvastatin", "lisinopril"]

    def test_partial_overlap(self):
        train = _make_df(["aspirin", "metformin"], ["D1", "D2"])
        test = _make_df(["aspirin", "lisinopril"], ["D3", "D4"])
        result = audit_drug_overlap(train, test)
        assert result["overlap_count"] == 1
        assert result["overlap_drugs"] == ["aspirin"]
        assert result["test_only_drugs"] == ["lisinopril"]
        assert result["overlap_ratio"] == pytest.approx(0.5, abs=1e-4)

    def test_full_overlap(self):
        train = _make_df(["aspirin", "metformin"], ["D1", "D2"])
        test = _make_df(["aspirin", "metformin"], ["D3", "D4"])
        result = audit_drug_overlap(train, test)
        assert result["overlap_count"] == 2
        assert result["overlap_ratio"] == pytest.approx(1.0, abs=1e-4)
        assert result["test_only_drugs"] == []

    def test_empty_test_set(self):
        train = _make_df(["aspirin"], ["D1"])
        test = _make_df([], [])
        result = audit_drug_overlap(train, test)
        assert result["test_count"] == 0
        assert result["overlap_count"] == 0
        assert result["overlap_ratio"] == 0.0


# ---------------------------------------------------------------------------
# TestAuditDiseaseOverlap
# ---------------------------------------------------------------------------
class TestAuditDiseaseOverlap:
    """Tests for audit_disease_overlap."""

    def test_no_overlap(self):
        train = _make_df(["a"], ["EFO_001"])
        test = _make_df(["b"], ["EFO_002"])
        result = audit_disease_overlap(train, test)
        assert result["overlap_count"] == 0
        assert result["overlap_diseases"] == []

    def test_partial_overlap(self):
        train = _make_df(["a", "b"], ["EFO_001", "EFO_002"])
        test = _make_df(["c", "d"], ["EFO_002", "EFO_003"])
        result = audit_disease_overlap(train, test)
        assert result["overlap_count"] == 1
        assert result["overlap_diseases"] == ["EFO_002"]
        assert result["test_only_diseases"] == ["EFO_003"]

    def test_full_overlap(self):
        train = _make_df(["a", "b"], ["EFO_001", "EFO_002"])
        test = _make_df(["c", "d"], ["EFO_001", "EFO_002"])
        result = audit_disease_overlap(train, test)
        assert result["overlap_count"] == 2
        assert result["overlap_ratio"] == pytest.approx(1.0, abs=1e-4)

    def test_empty_test_set(self):
        train = _make_df(["a"], ["EFO_001"])
        test = _make_df([], [])
        result = audit_disease_overlap(train, test)
        assert result["test_count"] == 0
        assert result["overlap_ratio"] == 0.0


# ---------------------------------------------------------------------------
# TestAuditPairOverlap
# ---------------------------------------------------------------------------
class TestAuditPairOverlap:
    """Tests for audit_pair_overlap."""

    def test_no_pair_overlap_clean(self):
        train = _make_df(["aspirin"], ["EFO_001"])
        test = _make_df(["aspirin"], ["EFO_002"])
        result = audit_pair_overlap(train, test)
        assert result["clean"] is True
        assert result["overlap_count"] == 0
        assert result["overlap_pairs"] == []

    def test_pair_overlap_not_clean(self):
        train = _make_df(["aspirin", "metformin"], ["EFO_001", "EFO_002"])
        test = _make_df(["aspirin", "lisinopril"], ["EFO_001", "EFO_003"])
        result = audit_pair_overlap(train, test)
        assert result["clean"] is False
        assert result["overlap_count"] == 1
        assert result["overlap_pairs"] == [{"drug": "aspirin", "disease": "EFO_001"}]

    def test_drug_overlap_but_no_pair_overlap(self):
        """Same drug appears in both sets but paired with different diseases."""
        train = _make_df(["aspirin"], ["EFO_001"])
        test = _make_df(["aspirin"], ["EFO_999"])
        result = audit_pair_overlap(train, test)
        assert result["clean"] is True
        assert result["overlap_count"] == 0


# ---------------------------------------------------------------------------
# TestGenerateLeakageReport
# ---------------------------------------------------------------------------
class TestGenerateLeakageReport:
    """Tests for generate_leakage_report."""

    def test_clean_split_passes(self):
        train = _make_df(["aspirin", "metformin"], ["EFO_001", "EFO_002"])
        test = _make_df(["lisinopril"], ["EFO_003"])
        report = generate_leakage_report(train, test, "test_split")
        assert report["passed"] is True
        assert report["split_name"] == "test_split"
        assert report["pair_overlap"]["clean"] is True

    def test_leaked_pairs_fails(self):
        train = _make_df(["aspirin"], ["EFO_001"])
        test = _make_df(["aspirin"], ["EFO_001"])
        report = generate_leakage_report(train, test, "leaked_split")
        assert report["passed"] is False
        assert report["pair_overlap"]["overlap_count"] == 1
        # Should have a CRITICAL recommendation
        assert any("CRITICAL" in r for r in report["recommendations"])

    def test_high_drug_overlap_warning(self):
        """When >80% of test drugs are seen in training, emit a WARNING."""
        # 9 overlapping drugs, 1 new = 90% overlap
        train_drugs = [f"drug_{i}" for i in range(10)]
        train_diseases = [f"EFO_{i:03d}" for i in range(10)]
        test_drugs = [f"drug_{i}" for i in range(9)] + ["drug_new"]
        test_diseases = [f"EFO_{i + 100:03d}" for i in range(10)]  # different diseases → no pair leak
        train = _make_df(train_drugs, train_diseases)
        test = _make_df(test_drugs, test_diseases)
        report = generate_leakage_report(train, test, "high_overlap")
        assert report["passed"] is True  # no pair leakage
        assert report["seen_drug_test_fraction"] == pytest.approx(0.9, abs=0.01)
        assert any("WARNING" in r for r in report["recommendations"])

    def test_clean_no_warnings(self):
        """A completely clean split with low overlap should have no warnings."""
        train = _make_df(["aspirin"], ["EFO_001"])
        test = _make_df(["lisinopril"], ["EFO_999"])
        report = generate_leakage_report(train, test, "clean")
        assert report["passed"] is True
        assert any("No leakage" in r for r in report["recommendations"])

    def test_report_contains_all_sections(self):
        train = _make_df(["aspirin"], ["EFO_001"])
        test = _make_df(["metformin"], ["EFO_002"])
        report = generate_leakage_report(train, test)
        expected_keys = {
            "split_name", "passed", "drug_overlap", "disease_overlap",
            "pair_overlap", "seen_drug_test_fraction", "recommendations",
        }
        assert expected_keys == set(report.keys())


# ---------------------------------------------------------------------------
# TestSaveLeakageReport
# ---------------------------------------------------------------------------
class TestSaveLeakageReport:
    """Tests for save_leakage_report."""

    def test_writes_valid_json(self, tmp_path):
        report = generate_leakage_report(
            _make_df(["aspirin"], ["EFO_001"]),
            _make_df(["metformin"], ["EFO_002"]),
            "test_save",
        )
        out_file = tmp_path / "audit" / "leakage.json"
        save_leakage_report(report, out_file)
        assert out_file.exists()
        loaded = json.loads(out_file.read_text(encoding="utf-8"))
        assert loaded["split_name"] == "test_save"
        assert "passed" in loaded

    def test_contains_all_keys(self, tmp_path):
        report = generate_leakage_report(
            _make_df(["a"], ["D1"]),
            _make_df(["b"], ["D2"]),
        )
        out_file = tmp_path / "report.json"
        save_leakage_report(report, out_file)
        loaded = json.loads(out_file.read_text(encoding="utf-8"))
        expected_keys = {
            "split_name", "passed", "drug_overlap", "disease_overlap",
            "pair_overlap", "seen_drug_test_fraction", "recommendations",
        }
        assert expected_keys == set(loaded.keys())

    def test_creates_parent_directories(self, tmp_path):
        report = {"split_name": "test", "passed": True}
        deep_path = tmp_path / "a" / "b" / "c" / "report.json"
        save_leakage_report(report, deep_path)
        assert deep_path.exists()
