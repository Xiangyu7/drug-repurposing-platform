"""Tests for Inter-Annotator Agreement (IAA) computation."""

import csv
import tempfile
from pathlib import Path

import pytest

from src.dr.evaluation.annotation import (
    AnnotationPair,
    IAAReport,
    compute_cohens_kappa,
    compute_iaa,
    load_dual_annotations,
)


class TestCohensKappa:
    """Test Cohen's Kappa computation."""

    def test_perfect_agreement(self):
        labels_a = ["benefit", "harm", "neutral", "benefit", "harm"]
        labels_b = ["benefit", "harm", "neutral", "benefit", "harm"]
        assert compute_cohens_kappa(labels_a, labels_b) == 1.0

    def test_complete_disagreement(self):
        labels_a = ["benefit", "benefit", "benefit"]
        labels_b = ["harm", "harm", "harm"]
        kappa = compute_cohens_kappa(labels_a, labels_b)
        # When label distributions don't overlap at all, p_e=0, p_o=0 → kappa=0
        assert kappa <= 0.0  # At most chance-level

    def test_chance_agreement(self):
        """Two raters who randomly assign labels should get kappa ~0."""
        # Simulated: each rater assigns 50/50, with 50% observed agreement
        labels_a = ["benefit"] * 5 + ["harm"] * 5
        labels_b = ["benefit", "harm"] * 5
        kappa = compute_cohens_kappa(labels_a, labels_b)
        assert abs(kappa) < 0.3  # Close to 0

    def test_moderate_agreement(self):
        """Known moderate agreement case."""
        labels_a = ["benefit", "harm", "neutral", "benefit", "benefit",
                     "harm", "neutral", "benefit", "harm", "neutral"]
        labels_b = ["benefit", "harm", "benefit", "benefit", "harm",
                     "harm", "neutral", "benefit", "harm", "neutral"]
        kappa = compute_cohens_kappa(labels_a, labels_b)
        assert 0.3 < kappa < 0.9

    def test_all_same_label(self):
        """All items have same label - kappa is undefined, return 1.0 by convention."""
        labels_a = ["benefit"] * 5
        labels_b = ["benefit"] * 5
        assert compute_cohens_kappa(labels_a, labels_b) == 1.0

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            compute_cohens_kappa([], [])

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="same length"):
            compute_cohens_kappa(["a", "b"], ["a"])


class TestComputeIAA:
    """Test multi-field IAA computation."""

    def test_single_field(self):
        pairs = [
            AnnotationPair("r1", "A", "B", "benefit", "benefit", "direction"),
            AnnotationPair("r2", "A", "B", "harm", "harm", "direction"),
            AnnotationPair("r3", "A", "B", "benefit", "harm", "direction"),
        ]
        reports = compute_iaa(pairs)
        assert "direction" in reports
        assert reports["direction"].n_pairs == 3
        assert reports["direction"].raw_agreement == pytest.approx(2 / 3, abs=0.01)

    def test_multiple_fields(self):
        pairs = [
            AnnotationPair("r1", "A", "B", "benefit", "benefit", "direction"),
            AnnotationPair("r1", "A", "B", "human", "human", "model"),
            AnnotationPair("r2", "A", "B", "harm", "neutral", "direction"),
            AnnotationPair("r2", "A", "B", "animal", "animal", "model"),
        ]
        reports = compute_iaa(pairs)
        assert "direction" in reports
        assert "model" in reports
        assert reports["model"].raw_agreement == 1.0
        assert reports["direction"].raw_agreement == 0.5

    def test_empty_pairs(self):
        reports = compute_iaa([])
        assert reports == {}

    def test_confusion_matrix(self):
        pairs = [
            AnnotationPair("r1", "A", "B", "benefit", "benefit", "direction"),
            AnnotationPair("r2", "A", "B", "benefit", "harm", "direction"),
            AnnotationPair("r3", "A", "B", "harm", "harm", "direction"),
        ]
        reports = compute_iaa(pairs)
        cm = reports["direction"].confusion_matrix
        assert cm["benefit"]["benefit"] == 1
        assert cm["benefit"]["harm"] == 1
        assert cm["harm"]["harm"] == 1


class TestLoadDualAnnotations:
    """Test loading dual annotations from CSV."""

    def test_load_valid_v2_csv(self, tmp_path):
        csv_path = tmp_path / "gold_v2.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "pmid", "drug_name", "direction", "model", "endpoint",
                "annotator", "annotator_b", "direction_b", "model_b", "endpoint_b",
            ])
            writer.writeheader()
            writer.writerow({
                "pmid": "12345", "drug_name": "resveratrol",
                "direction": "benefit", "model": "human", "endpoint": "CV_EVENTS",
                "annotator": "expert1", "annotator_b": "expert2",
                "direction_b": "benefit", "model_b": "human", "endpoint_b": "CV_EVENTS",
            })
            writer.writerow({
                "pmid": "67890", "drug_name": "metformin",
                "direction": "neutral", "model": "animal", "endpoint": "BIOMARKER",
                "annotator": "expert1", "annotator_b": "expert2",
                "direction_b": "benefit", "model_b": "animal", "endpoint_b": "BIOMARKER",
            })

        pairs = load_dual_annotations(str(csv_path))
        assert len(pairs) == 6  # 2 rows x 3 fields
        direction_pairs = [p for p in pairs if p.field == "direction"]
        assert len(direction_pairs) == 2

    def test_missing_b_columns_skipped(self, tmp_path):
        csv_path = tmp_path / "gold_partial.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "pmid", "drug_name", "direction", "model", "endpoint",
                "annotator", "annotator_b", "direction_b", "model_b", "endpoint_b",
            ])
            writer.writeheader()
            # Row with no annotator_b data
            writer.writerow({
                "pmid": "12345", "drug_name": "aspirin",
                "direction": "benefit", "model": "human", "endpoint": "CV_EVENTS",
                "annotator": "expert1", "annotator_b": "",
                "direction_b": "", "model_b": "", "endpoint_b": "",
            })

        pairs = load_dual_annotations(str(csv_path))
        assert len(pairs) == 0  # No dual annotations

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_dual_annotations("/nonexistent/path.csv")
