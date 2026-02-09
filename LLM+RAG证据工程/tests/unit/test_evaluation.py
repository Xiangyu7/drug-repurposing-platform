"""Tests for evaluation framework (gold standard + metrics)"""

import pytest
import tempfile
from pathlib import Path

from src.dr.evaluation.gold_standard import (
    GoldStandardRecord,
    load_gold_standard,
    save_gold_standard,
    bootstrap_from_dossiers,
    VALID_DIRECTIONS,
    VALID_MODELS,
)
from src.dr.evaluation.metrics import (
    evaluate_field,
    evaluate_extraction,
    compare_methods,
    FieldMetrics,
    ExtractionMetrics,
)


# ============================================================
# GoldStandardRecord Tests
# ============================================================

class TestGoldStandardRecord:
    def test_valid_record(self):
        r = GoldStandardRecord(
            pmid="12345678", drug_name="resveratrol",
            direction="benefit", model="animal", endpoint="PLAQUE_IMAGING"
        )
        assert r.validate() == []

    def test_empty_pmid(self):
        r = GoldStandardRecord(
            pmid="", drug_name="resveratrol",
            direction="benefit", model="animal", endpoint="PLAQUE_IMAGING"
        )
        issues = r.validate()
        assert any("pmid" in i for i in issues)

    def test_invalid_direction(self):
        r = GoldStandardRecord(
            pmid="12345678", drug_name="resveratrol",
            direction="good", model="animal", endpoint="PLAQUE_IMAGING"
        )
        issues = r.validate()
        assert any("direction" in i for i in issues)

    def test_invalid_model(self):
        r = GoldStandardRecord(
            pmid="12345678", drug_name="resveratrol",
            direction="benefit", model="fish", endpoint="PLAQUE_IMAGING"
        )
        issues = r.validate()
        assert any("model" in i for i in issues)

    def test_all_valid_directions(self):
        for d in VALID_DIRECTIONS:
            r = GoldStandardRecord(
                pmid="123", drug_name="test", direction=d,
                model="human", endpoint="OTHER"
            )
            assert r.validate() == [], f"direction '{d}' should be valid"


# ============================================================
# Load / Save Tests
# ============================================================

class TestGoldStandardIO:
    def test_save_and_load_roundtrip(self):
        records = [
            GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS", "HIGH"),
            GoldStandardRecord("222", "drug_b", "harm", "animal", "PLAQUE_IMAGING", "MED"),
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = f.name

        save_gold_standard(records, path)
        loaded = load_gold_standard(path)

        assert len(loaded) == 2
        assert loaded[0].pmid == "111"
        assert loaded[0].direction == "benefit"
        assert loaded[1].pmid == "222"
        assert loaded[1].direction == "harm"

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_gold_standard("/nonexistent/path.csv")

    def test_load_missing_columns(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            f.write("pmid,drug_name\n")
            f.write("123,test\n")
            path = f.name

        with pytest.raises(ValueError, match="Missing required columns"):
            load_gold_standard(path)

    def test_load_skips_invalid_rows(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            f.write("pmid,drug_name,direction,model,endpoint\n")
            f.write("111,drug_a,benefit,human,CV_EVENTS\n")
            f.write(",drug_b,benefit,human,OTHER\n")  # empty pmid
            f.write("333,drug_c,badvalue,human,OTHER\n")  # invalid direction
            path = f.name

        records = load_gold_standard(path)
        assert len(records) == 1
        assert records[0].pmid == "111"


# ============================================================
# Bootstrap Tests
# ============================================================

class TestBootstrap:
    def test_bootstrap_from_dossiers(self, tmp_path):
        import json
        dossier = {
            "canonical_name": "test_drug",
            "llm_structured": {
                "supporting_evidence": [
                    {"pmid": "111", "direction": "benefit", "model": "human",
                     "endpoint": "CV_EVENTS", "confidence": 0.9, "claim": "test claim"},
                    {"pmid": "222", "direction": "benefit", "model": "animal",
                     "endpoint": "PLAQUE_IMAGING", "confidence": 0.3, "claim": "low conf"},
                ],
                "harm_or_neutral_evidence": [
                    {"pmid": "333", "direction": "harm", "model": "human",
                     "endpoint": "BIOMARKER", "confidence": 0.8, "claim": "harm claim"},
                ]
            }
        }
        (tmp_path / "test.json").write_text(json.dumps(dossier))

        records = bootstrap_from_dossiers(str(tmp_path), min_confidence=0.7)
        assert len(records) == 2  # 111 (0.9) and 333 (0.8), not 222 (0.3)
        pmids = {r.pmid for r in records}
        assert "111" in pmids
        assert "333" in pmids
        assert "222" not in pmids

    def test_bootstrap_empty_dir(self, tmp_path):
        records = bootstrap_from_dossiers(str(tmp_path))
        assert records == []

    def test_bootstrap_nonexistent_dir(self):
        records = bootstrap_from_dossiers("/nonexistent/path")
        assert records == []

    def test_bootstrap_deduplicates(self, tmp_path):
        import json
        dossier = {
            "canonical_name": "test_drug",
            "llm_structured": {
                "supporting_evidence": [
                    {"pmid": "111", "direction": "benefit", "model": "human",
                     "endpoint": "CV_EVENTS", "confidence": 0.9, "claim": "claim1"},
                    {"pmid": "111", "direction": "benefit", "model": "human",
                     "endpoint": "CV_EVENTS", "confidence": 0.9, "claim": "claim2"},
                ],
                "harm_or_neutral_evidence": []
            }
        }
        (tmp_path / "test.json").write_text(json.dumps(dossier))
        records = bootstrap_from_dossiers(str(tmp_path), min_confidence=0.7)
        assert len(records) == 1


# ============================================================
# Metrics Tests
# ============================================================

class TestFieldMetrics:
    def test_perfect_accuracy(self):
        fm = evaluate_field(
            ["benefit", "harm", "neutral"],
            ["benefit", "harm", "neutral"]
        )
        assert fm.accuracy == 1.0
        assert fm.correct == 3
        assert fm.total == 3

    def test_zero_accuracy(self):
        fm = evaluate_field(
            ["harm", "benefit", "unclear"],
            ["benefit", "harm", "neutral"]
        )
        assert fm.accuracy == 0.0
        assert fm.correct == 0

    def test_partial_accuracy(self):
        fm = evaluate_field(
            ["benefit", "harm", "neutral", "benefit"],
            ["benefit", "benefit", "neutral", "harm"]
        )
        assert fm.accuracy == 0.5
        assert fm.correct == 2

    def test_confusion_matrix(self):
        fm = evaluate_field(
            ["benefit", "benefit", "harm"],
            ["benefit", "harm", "harm"]
        )
        assert fm.confusion["benefit"]["benefit"] == 1
        assert fm.confusion["benefit"]["harm"] == 1
        assert fm.confusion["harm"]["harm"] == 1

    def test_class_precision_recall(self):
        fm = evaluate_field(
            ["benefit", "benefit", "harm", "harm"],
            ["benefit", "harm", "harm", "benefit"]
        )
        # benefit: TP=1, FP=1, FN=1 => P=0.5, R=0.5
        assert fm.class_metrics["benefit"]["precision"] == 0.5
        assert fm.class_metrics["benefit"]["recall"] == 0.5

    def test_length_mismatch(self):
        with pytest.raises(ValueError, match="Length mismatch"):
            evaluate_field(["a", "b"], ["a"])

    def test_empty_lists(self):
        fm = evaluate_field([], [])
        assert fm.accuracy == 0.0
        assert fm.total == 0


class TestExtractionMetrics:
    def test_basic_evaluation(self):
        gold = [
            GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS"),
            GoldStandardRecord("222", "drug_a", "harm", "animal", "PLAQUE_IMAGING"),
        ]
        predictions = [
            {"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "human"},
            {"pmid": "222", "drug_name": "drug_a", "direction": "harm", "model": "animal"},
        ]
        metrics = evaluate_extraction(predictions, gold, fields=["direction", "model"])
        assert metrics.matched_samples == 2
        assert metrics.overall_accuracy == 1.0

    def test_partial_match(self):
        gold = [
            GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS"),
            GoldStandardRecord("222", "drug_a", "harm", "animal", "PLAQUE_IMAGING"),
        ]
        predictions = [
            {"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "cell"},
        ]
        metrics = evaluate_extraction(predictions, gold, fields=["direction"])
        assert metrics.matched_samples == 1
        assert metrics.unmatched_gold == 1

    def test_no_matches(self):
        gold = [GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS")]
        predictions = [{"pmid": "999", "drug_name": "drug_b", "direction": "benefit"}]
        metrics = evaluate_extraction(predictions, gold)
        assert metrics.matched_samples == 0
        assert metrics.unmatched_gold == 1
        assert metrics.unmatched_predictions == 1

    def test_case_insensitive_matching(self):
        gold = [GoldStandardRecord("111", "Drug_A", "benefit", "human", "CV_EVENTS")]
        predictions = [{"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "human"}]
        metrics = evaluate_extraction(predictions, gold)
        assert metrics.matched_samples == 1

    def test_summary_output(self):
        gold = [GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS")]
        predictions = [{"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "human"}]
        metrics = evaluate_extraction(predictions, gold)
        summary = metrics.summary()
        assert "accuracy" in summary.lower()

    def test_to_dict_serializable(self):
        gold = [GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS")]
        predictions = [{"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "human"}]
        metrics = evaluate_extraction(predictions, gold)
        d = metrics.to_dict()
        import json
        json.dumps(d)  # should not raise


class TestCompareethods:
    def test_comparison(self):
        gold = [
            GoldStandardRecord("111", "drug_a", "benefit", "human", "CV_EVENTS"),
            GoldStandardRecord("222", "drug_a", "harm", "animal", "PLAQUE_IMAGING"),
        ]
        results_a = [
            {"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "human"},
            {"pmid": "222", "drug_name": "drug_a", "direction": "harm", "model": "animal"},
        ]
        results_b = [
            {"pmid": "111", "drug_name": "drug_a", "direction": "benefit", "model": "cell"},
            {"pmid": "222", "drug_name": "drug_a", "direction": "neutral", "model": "animal"},
        ]
        report = compare_methods(results_a, results_b, gold, "llm", "rule")
        assert report.metrics_a.overall_accuracy > report.metrics_b.overall_accuracy
        assert report.field_deltas["direction"] > 0
        summary = report.summary()
        assert "llm" in summary and "rule" in summary
