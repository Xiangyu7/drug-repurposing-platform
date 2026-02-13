"""Tests for stratified sampling and coverage analysis."""

import pytest

from src.dr.evaluation.gold_standard import GoldStandardRecord
from src.dr.evaluation.stratified_sampling import (
    compute_stratum_coverage,
    identify_gaps,
    stratified_sample,
    coverage_report,
)


def _make_records(specs):
    """Helper: create GoldStandardRecord list from (direction, model, drug) tuples."""
    records = []
    for i, (direction, model, drug) in enumerate(specs):
        records.append(GoldStandardRecord(
            pmid=str(10000 + i),
            drug_name=drug,
            direction=direction,
            model=model,
            endpoint="BIOMARKER",
            confidence="HIGH",
        ))
    return records


class TestStratumCoverage:
    """Test coverage computation."""

    def test_basic_coverage(self):
        records = _make_records([
            ("benefit", "human", "aspirin"),
            ("benefit", "animal", "metformin"),
            ("harm", "human", "warfarin"),
        ])
        cov = compute_stratum_coverage(records)
        assert cov["direction"]["benefit"] == 2
        assert cov["direction"]["harm"] == 1
        assert cov["model"]["human"] == 2
        assert cov["model"]["animal"] == 1
        assert len(cov["drug_name"]) == 3

    def test_empty_records(self):
        cov = compute_stratum_coverage([])
        assert cov["direction"] == {}
        assert cov["drug_name"] == {}


class TestIdentifyGaps:
    """Test gap identification."""

    def test_all_gaps_with_few_records(self):
        records = _make_records([
            ("benefit", "human", "aspirin"),
            ("benefit", "human", "aspirin"),
        ])
        gaps = identify_gaps(records, min_per_stratum=5)
        assert any("harm" in g for g in gaps)
        assert any("neutral" in g for g in gaps)
        assert any("animal" in g for g in gaps)
        assert any("drug diversity" in g for g in gaps)

    def test_no_gaps_with_good_coverage(self):
        # Build a well-covered dataset
        specs = []
        drugs = [f"drug_{i}" for i in range(12)]
        for d in ("benefit", "harm", "neutral", "unclear"):
            for m in ("human", "animal", "cell"):
                for drug in drugs[:2]:
                    specs.append((d, m, drug))
        records = _make_records(specs)
        gaps = identify_gaps(records, min_per_stratum=5)
        # Should have no direction/model gaps
        direction_gaps = [g for g in gaps if "direction=" in g]
        assert len(direction_gaps) == 0

    def test_drug_dominance_detected(self):
        # One drug dominates > 30%
        records = _make_records([
            ("benefit", "human", "aspirin"),
            ("benefit", "human", "aspirin"),
            ("benefit", "human", "aspirin"),
            ("harm", "animal", "metformin"),
        ])
        gaps = identify_gaps(records, min_per_stratum=1)
        assert any("over-represented" in g for g in gaps)


class TestStratifiedSample:
    """Test stratified sampling."""

    def test_basic_sampling(self):
        records = _make_records([
            ("benefit", "human", "drug_a"),
            ("benefit", "animal", "drug_b"),
            ("harm", "human", "drug_c"),
            ("harm", "animal", "drug_d"),
            ("neutral", "human", "drug_e"),
            ("neutral", "cell", "drug_f"),
            ("unclear", "human", "drug_g"),
            ("unclear", "animal", "drug_h"),
        ])
        sampled = stratified_sample(records, n=4)
        assert len(sampled) == 4
        # Should have at least one from each direction
        directions = {r.direction for r in sampled}
        assert len(directions) >= 2  # At least 2 different directions

    def test_deterministic_with_seed(self):
        records = _make_records([
            ("benefit", "human", f"drug_{i}") for i in range(20)
        ])
        s1 = stratified_sample(records, n=5, seed=42)
        s2 = stratified_sample(records, n=5, seed=42)
        assert [r.pmid for r in s1] == [r.pmid for r in s2]

    def test_empty_pool(self):
        assert stratified_sample([], n=10) == []

    def test_request_more_than_available(self):
        records = _make_records([("benefit", "human", "drug_a")])
        sampled = stratified_sample(records, n=100)
        assert len(sampled) <= 1


class TestCoverageReport:
    """Test coverage report generation."""

    def test_report_is_string(self):
        records = _make_records([
            ("benefit", "human", "aspirin"),
            ("harm", "animal", "warfarin"),
        ])
        report = coverage_report(records)
        assert isinstance(report, str)
        assert "Total records: 2" in report
        assert "benefit" in report
        assert "harm" in report
