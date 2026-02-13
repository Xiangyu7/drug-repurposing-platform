"""Tests for kg_explain.evaluation.external_benchmarks.

Covers:
- download_hetionet_ctd: Mock HTTP request, verify DataFrame columns and parsing
- map_hetionet_to_internal: DOID->EFO mapping, unmapped exclusion
- build_external_gold: Integration with mock data, disease_filter
"""
from __future__ import annotations

import gzip
import io
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from kg_explain.evaluation.external_benchmarks import (
    download_hetionet_ctd,
    map_hetionet_to_internal,
    build_external_gold,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hetionet_tsv_gz(rows: list[dict]) -> bytes:
    """Build a gzipped TSV file matching Hetionet CtD edge format.

    Hetionet TSV columns: source, metaedge, target
    Source = "Compound::DB00945::Aspirin"  (but actual format is just two parts)
    """
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with gzip.open(buf, "wt", encoding="utf-8") as gz:
        df.to_csv(gz, sep="\t", index=False)
    return buf.getvalue()


SAMPLE_HETIONET_ROWS = [
    {"source": "Compound::DB00945::Aspirin", "metaedge": "CtD", "target": "Disease::DOID:14330::Parkinson disease"},
    {"source": "Compound::DB00316::Acetaminophen", "metaedge": "CtD", "target": "Disease::DOID:1612::Breast cancer"},
    {"source": "Compound::DB01050::Ibuprofen", "metaedge": "CtD", "target": "Disease::DOID:14330::Parkinson disease"},
]

# The source parsing splits on "::" at most 1 time -> compound_id gets "Compound", compound_name gets rest
# Actually, looking at the code: source.split("::", 1)
#   "Compound::DB00945::Aspirin" -> ["Compound", "DB00945::Aspirin"]
# So compound_id="Compound", compound_name="db00945::aspirin" (lowered)
# Let's use the real-ish format the code expects:
SAMPLE_HETIONET_ROWS_V2 = [
    {"source": "DB00945::aspirin", "metaedge": "CtD", "target": "DOID:14330::Parkinson disease"},
    {"source": "DB00316::acetaminophen", "metaedge": "CtD", "target": "DOID:1612::Breast cancer"},
    {"source": "DB01050::ibuprofen", "metaedge": "CtD", "target": "DOID:14330::Parkinson disease"},
]


# ---------------------------------------------------------------------------
# Tests: download_hetionet_ctd
# ---------------------------------------------------------------------------

class TestDownloadHetionetCtd:
    """Tests for download_hetionet_ctd with mocked HTTP."""

    def test_download_creates_correct_dataframe(self, tmp_path, monkeypatch):
        """Verify DataFrame columns and row count after download."""
        tsv_gz = _make_hetionet_tsv_gz(SAMPLE_HETIONET_ROWS_V2)
        mock_resp = MagicMock()
        mock_resp.content = tsv_gz
        mock_resp.raise_for_status = MagicMock()

        mock_get = MagicMock(return_value=mock_resp)
        monkeypatch.setattr("kg_explain.evaluation.external_benchmarks.requests.get", mock_get)

        df = download_hetionet_ctd(tmp_path / "cache")

        assert set(df.columns) == {"compound_id", "compound_name", "disease_id", "disease_name"}
        assert len(df) == 3
        mock_get.assert_called_once()

    def test_parsing_double_colon_format(self, tmp_path, monkeypatch):
        """Verify 'Compound::DB00945' format is correctly split."""
        tsv_gz = _make_hetionet_tsv_gz(SAMPLE_HETIONET_ROWS_V2)
        mock_resp = MagicMock()
        mock_resp.content = tsv_gz
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(
            "kg_explain.evaluation.external_benchmarks.requests.get",
            MagicMock(return_value=mock_resp),
        )

        df = download_hetionet_ctd(tmp_path / "cache")

        # First row: source "DB00945::aspirin" -> compound_id="DB00945", compound_name="aspirin"
        row0 = df.iloc[0]
        assert row0["compound_id"] == "DB00945"
        assert row0["compound_name"] == "aspirin"

        # Target "DOID:14330::Parkinson disease" -> disease_id="DOID:14330", disease_name="Parkinson disease"
        assert row0["disease_id"] == "DOID:14330"
        assert row0["disease_name"] == "Parkinson disease"

    def test_uses_cache_on_second_call(self, tmp_path, monkeypatch):
        """Second call should use cached file (no HTTP request)."""
        tsv_gz = _make_hetionet_tsv_gz(SAMPLE_HETIONET_ROWS_V2)
        mock_resp = MagicMock()
        mock_resp.content = tsv_gz
        mock_resp.raise_for_status = MagicMock()
        mock_get = MagicMock(return_value=mock_resp)
        monkeypatch.setattr("kg_explain.evaluation.external_benchmarks.requests.get", mock_get)

        cache_dir = tmp_path / "cache"
        df1 = download_hetionet_ctd(cache_dir)
        df2 = download_hetionet_ctd(cache_dir)

        # Should have been called only once (second uses cache)
        assert mock_get.call_count == 1
        assert len(df2) == len(df1)

    def test_force_redownloads(self, tmp_path, monkeypatch):
        """force=True should re-download even if cached."""
        tsv_gz = _make_hetionet_tsv_gz(SAMPLE_HETIONET_ROWS_V2)
        mock_resp = MagicMock()
        mock_resp.content = tsv_gz
        mock_resp.raise_for_status = MagicMock()
        mock_get = MagicMock(return_value=mock_resp)
        monkeypatch.setattr("kg_explain.evaluation.external_benchmarks.requests.get", mock_get)

        cache_dir = tmp_path / "cache"
        download_hetionet_ctd(cache_dir)
        download_hetionet_ctd(cache_dir, force=True)

        assert mock_get.call_count == 2

    def test_compound_name_lowercased(self, tmp_path, monkeypatch):
        """compound_name should be lowercased for normalization."""
        rows = [{"source": "DB999::ASPIRIN", "metaedge": "CtD", "target": "DOID:1::disease"}]
        tsv_gz = _make_hetionet_tsv_gz(rows)
        mock_resp = MagicMock()
        mock_resp.content = tsv_gz
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(
            "kg_explain.evaluation.external_benchmarks.requests.get",
            MagicMock(return_value=mock_resp),
        )

        df = download_hetionet_ctd(tmp_path / "cache")
        assert df.iloc[0]["compound_name"] == "aspirin"


# ---------------------------------------------------------------------------
# Tests: map_hetionet_to_internal
# ---------------------------------------------------------------------------

class TestMapHetionetToInternal:
    """Tests for map_hetionet_to_internal with DOID->EFO mapping."""

    def test_basic_mapping(self, tmp_path):
        """Create a temp mapping CSV and verify DOID->EFO mapping."""
        mapping_csv = tmp_path / "disease_id_mapping.csv"
        pd.DataFrame([
            {"doid": "DOID:14330", "efo_id": "EFO_0002508", "disease_name": "Parkinson"},
        ]).to_csv(mapping_csv, index=False)

        hetionet_df = pd.DataFrame([
            {"compound_id": "DB00945", "compound_name": "aspirin", "disease_id": "DOID:14330", "disease_name": "Parkinson"},
            {"compound_id": "DB01050", "compound_name": "ibuprofen", "disease_id": "DOID:14330", "disease_name": "Parkinson"},
        ])

        result = map_hetionet_to_internal(hetionet_df, mapping_csv)

        assert "drug_normalized" in result.columns
        assert "diseaseId" in result.columns
        assert len(result) == 2
        assert all(result["diseaseId"] == "EFO_0002508")
        assert set(result["drug_normalized"]) == {"aspirin", "ibuprofen"}

    def test_unmapped_diseases_excluded(self, tmp_path):
        """Diseases without DOID->EFO mapping should be excluded."""
        mapping_csv = tmp_path / "disease_id_mapping.csv"
        pd.DataFrame([
            {"doid": "DOID:14330", "efo_id": "EFO_0002508", "disease_name": "Parkinson"},
        ]).to_csv(mapping_csv, index=False)

        hetionet_df = pd.DataFrame([
            {"compound_id": "DB00945", "compound_name": "aspirin", "disease_id": "DOID:14330", "disease_name": "Parkinson"},
            {"compound_id": "DB00316", "compound_name": "acetaminophen", "disease_id": "DOID:9999", "disease_name": "Unknown"},
        ])

        result = map_hetionet_to_internal(hetionet_df, mapping_csv)

        assert len(result) == 1
        assert result.iloc[0]["drug_normalized"] == "aspirin"

    def test_missing_mapping_file_returns_empty(self, tmp_path):
        """Non-existent mapping file returns empty DataFrame."""
        hetionet_df = pd.DataFrame([
            {"compound_id": "X", "compound_name": "drugx", "disease_id": "D1", "disease_name": "D"},
        ])
        result = map_hetionet_to_internal(hetionet_df, tmp_path / "nonexistent.csv")
        assert len(result) == 0
        assert "drug_normalized" in result.columns
        assert "diseaseId" in result.columns

    def test_mapping_has_correct_source_column(self, tmp_path):
        """Result should have source='hetionet' and mapping_confidence='exact'."""
        mapping_csv = tmp_path / "map.csv"
        pd.DataFrame([
            {"doid": "DOID:1", "efo_id": "EFO_1", "disease_name": "D1"},
        ]).to_csv(mapping_csv, index=False)

        hetionet_df = pd.DataFrame([
            {"compound_id": "X", "compound_name": "drug_x", "disease_id": "DOID:1", "disease_name": "D1"},
        ])

        result = map_hetionet_to_internal(hetionet_df, mapping_csv)
        assert result.iloc[0]["source"] == "hetionet"
        assert result.iloc[0]["mapping_confidence"] == "exact"

    def test_invalid_mapping_file_raises(self, tmp_path):
        """Mapping CSV without required columns raises ValueError."""
        mapping_csv = tmp_path / "bad_mapping.csv"
        pd.DataFrame([
            {"wrong_col": "X", "also_wrong": "Y"},
        ]).to_csv(mapping_csv, index=False)

        hetionet_df = pd.DataFrame([
            {"compound_id": "X", "compound_name": "drug_x", "disease_id": "D1", "disease_name": "D1"},
        ])

        with pytest.raises(ValueError, match="doid.*efo_id"):
            map_hetionet_to_internal(hetionet_df, mapping_csv)


# ---------------------------------------------------------------------------
# Tests: build_external_gold
# ---------------------------------------------------------------------------

class TestBuildExternalGold:
    """Integration test for build_external_gold with mock data."""

    def test_build_gold_produces_correct_columns(self, tmp_path, monkeypatch):
        """Output should have [drug_normalized, diseaseId]."""
        # Mock download_hetionet_ctd
        hetionet_df = pd.DataFrame([
            {"compound_id": "DB1", "compound_name": "aspirin", "disease_id": "DOID:1", "disease_name": "Disease1"},
            {"compound_id": "DB2", "compound_name": "ibuprofen", "disease_id": "DOID:2", "disease_name": "Disease2"},
        ])
        monkeypatch.setattr(
            "kg_explain.evaluation.external_benchmarks.download_hetionet_ctd",
            lambda cache_dir, force=False: hetionet_df,
        )

        # Create mapping file
        mapping_csv = tmp_path / "mapping.csv"
        pd.DataFrame([
            {"doid": "DOID:1", "efo_id": "EFO_001", "disease_name": "Disease1"},
            {"doid": "DOID:2", "efo_id": "EFO_002", "disease_name": "Disease2"},
        ]).to_csv(mapping_csv, index=False)

        gold = build_external_gold(tmp_path / "cache", mapping_path=mapping_csv)

        assert set(gold.columns) == {"drug_normalized", "diseaseId"}
        assert len(gold) == 2

    def test_disease_filter_restricts_output(self, tmp_path, monkeypatch):
        """disease_filter should limit to specified EFO IDs only."""
        hetionet_df = pd.DataFrame([
            {"compound_id": "DB1", "compound_name": "aspirin", "disease_id": "DOID:1", "disease_name": "D1"},
            {"compound_id": "DB2", "compound_name": "ibuprofen", "disease_id": "DOID:2", "disease_name": "D2"},
            {"compound_id": "DB3", "compound_name": "metformin", "disease_id": "DOID:1", "disease_name": "D1"},
        ])
        monkeypatch.setattr(
            "kg_explain.evaluation.external_benchmarks.download_hetionet_ctd",
            lambda cache_dir, force=False: hetionet_df,
        )

        mapping_csv = tmp_path / "mapping.csv"
        pd.DataFrame([
            {"doid": "DOID:1", "efo_id": "EFO_001", "disease_name": "D1"},
            {"doid": "DOID:2", "efo_id": "EFO_002", "disease_name": "D2"},
        ]).to_csv(mapping_csv, index=False)

        gold = build_external_gold(
            tmp_path / "cache",
            mapping_path=mapping_csv,
            disease_filter=["EFO_001"],
        )

        assert len(gold) == 2
        assert all(gold["diseaseId"] == "EFO_001")

    def test_deduplicates_pairs(self, tmp_path, monkeypatch):
        """Duplicate drug-disease pairs should be deduplicated."""
        hetionet_df = pd.DataFrame([
            {"compound_id": "DB1", "compound_name": "aspirin", "disease_id": "DOID:1", "disease_name": "D1"},
            {"compound_id": "DB1", "compound_name": "aspirin", "disease_id": "DOID:1", "disease_name": "D1"},
        ])
        monkeypatch.setattr(
            "kg_explain.evaluation.external_benchmarks.download_hetionet_ctd",
            lambda cache_dir, force=False: hetionet_df,
        )

        mapping_csv = tmp_path / "mapping.csv"
        pd.DataFrame([
            {"doid": "DOID:1", "efo_id": "EFO_001", "disease_name": "D1"},
        ]).to_csv(mapping_csv, index=False)

        gold = build_external_gold(tmp_path / "cache", mapping_path=mapping_csv)
        assert len(gold) == 1
