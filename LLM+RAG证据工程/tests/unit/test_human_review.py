"""Tests for dr.evaluation.human_review.

Covers:
- compute_error_rates: known GO/REJECT, NO-GO/ADVANCE cases
- compute_error_rates: empty list -> all zeros
- compute_review_irr: needs 2+ reviewers per drug, uses compute_cohens_kappa
- generate_audit_report: verify all fields populated
- AuditReport.summary: verify text output
- load_reviews: from temp CSV file, valid and invalid rows
- ReviewRecord.validate: empty drug_id, invalid decisions
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.dr.evaluation.human_review import (
    ReviewRecord,
    AuditReport,
    PIPELINE_DECISIONS,
    HUMAN_DECISIONS,
    compute_error_rates,
    compute_review_irr,
    generate_audit_report,
    load_reviews,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_review(
    drug_id: str = "drug_a",
    reviewer: str = "reviewer_1",
    pipeline_decision: str = "GO",
    human_decision: str = "ADVANCE",
    review_date: str = "2026-01-15",
    confidence: str = "HIGH",
    notes: str = "",
) -> ReviewRecord:
    """Helper to create a ReviewRecord with sensible defaults."""
    return ReviewRecord(
        drug_id=drug_id,
        reviewer=reviewer,
        pipeline_decision=pipeline_decision,
        human_decision=human_decision,
        review_date=review_date,
        confidence=confidence,
        notes=notes,
    )


def _write_review_csv(path: Path, rows: list[dict]) -> None:
    """Write a list of dicts to a CSV file with header."""
    import csv
    fieldnames = [
        "drug_id", "reviewer", "pipeline_decision", "human_decision",
        "review_date", "confidence", "notes",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Tests: compute_error_rates
# ---------------------------------------------------------------------------

class TestComputeErrorRates:
    """Tests for kill rate, miss rate, and error rate computation."""

    def test_known_kill_rate(self):
        """GO/REJECT cases should produce correct kill_rate."""
        reviews = [
            _make_review(drug_id="d1", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d2", pipeline_decision="GO", human_decision="REJECT"),
            _make_review(drug_id="d3", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d4", pipeline_decision="GO", human_decision="REJECT"),
        ]
        rates = compute_error_rates(reviews)
        # 2 out of 4 GO drugs rejected -> kill_rate = 0.5
        assert rates["kill_rate"] == 0.5

    def test_known_miss_rate(self):
        """NO-GO/ADVANCE cases should produce correct miss_rate."""
        reviews = [
            _make_review(drug_id="d1", pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(drug_id="d2", pipeline_decision="NO-GO", human_decision="ADVANCE"),
            _make_review(drug_id="d3", pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(drug_id="d4", pipeline_decision="NO-GO", human_decision="REJECT"),
        ]
        rates = compute_error_rates(reviews)
        # 1 out of 4 NO-GO drugs advanced -> miss_rate = 0.25
        assert rates["miss_rate"] == 0.25

    def test_mixed_decisions_error_rate(self):
        """Overall error rate from mixed decisions."""
        reviews = [
            _make_review(pipeline_decision="GO", human_decision="ADVANCE"),      # agree
            _make_review(pipeline_decision="GO", human_decision="REJECT"),        # disagree
            _make_review(pipeline_decision="NO-GO", human_decision="REJECT"),     # agree
            _make_review(pipeline_decision="MAYBE", human_decision="HOLD"),       # agree
            _make_review(pipeline_decision="MAYBE", human_decision="ADVANCE"),    # disagree
        ]
        rates = compute_error_rates(reviews)
        # 2 out of 5 disagree -> error_rate = 0.4
        assert rates["error_rate"] == 0.4

    def test_empty_list_returns_zeros(self):
        """Empty review list should return all zero rates."""
        rates = compute_error_rates([])
        assert rates["kill_rate"] == 0.0
        assert rates["miss_rate"] == 0.0
        assert rates["error_rate"] == 0.0

    def test_no_go_reviews_kill_rate_zero(self):
        """When there are no GO reviews, kill_rate should be 0."""
        reviews = [
            _make_review(pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(pipeline_decision="MAYBE", human_decision="HOLD"),
        ]
        rates = compute_error_rates(reviews)
        assert rates["kill_rate"] == 0.0

    def test_no_nogo_reviews_miss_rate_zero(self):
        """When there are no NO-GO reviews, miss_rate should be 0."""
        reviews = [
            _make_review(pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(pipeline_decision="MAYBE", human_decision="HOLD"),
        ]
        rates = compute_error_rates(reviews)
        assert rates["miss_rate"] == 0.0

    def test_perfect_agreement(self):
        """All decisions agree -> zero error rates."""
        reviews = [
            _make_review(pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(pipeline_decision="MAYBE", human_decision="HOLD"),
        ]
        rates = compute_error_rates(reviews)
        assert rates["kill_rate"] == 0.0
        assert rates["miss_rate"] == 0.0
        assert rates["error_rate"] == 0.0


# ---------------------------------------------------------------------------
# Tests: compute_review_irr
# ---------------------------------------------------------------------------

class TestComputeReviewIRR:
    """Tests for inter-rater reliability computation."""

    def test_needs_two_reviewers_per_drug(self):
        """IRR requires at least 2 reviewers for the same drug."""
        reviews = [
            _make_review(drug_id="d1", reviewer="r1", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="r2", human_decision="ADVANCE"),
            _make_review(drug_id="d2", reviewer="r1", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r2", human_decision="REJECT"),
        ]
        kappa = compute_review_irr(reviews)
        # Perfect agreement -> kappa should be high (1.0 for same label)
        assert kappa == 1.0

    def test_single_reviewer_returns_negative(self):
        """With only single-reviewed drugs, IRR should be -1.0."""
        reviews = [
            _make_review(drug_id="d1", reviewer="r1", human_decision="ADVANCE"),
            _make_review(drug_id="d2", reviewer="r1", human_decision="REJECT"),
        ]
        kappa = compute_review_irr(reviews)
        assert kappa == -1.0

    def test_empty_reviews_returns_negative(self):
        """Empty review list should return -1.0."""
        kappa = compute_review_irr([])
        assert kappa == -1.0

    def test_disagreement_lowers_kappa(self):
        """Disagreement between reviewers should lower kappa."""
        reviews = [
            _make_review(drug_id="d1", reviewer="r1", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="r2", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r1", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r2", human_decision="ADVANCE"),
        ]
        kappa = compute_review_irr(reviews)
        # Complete disagreement -> kappa should be negative or close to -1
        assert kappa < 0

    def test_uses_first_two_reviewers(self):
        """When 3+ reviewers exist, uses first two in the list."""
        reviews = [
            _make_review(drug_id="d1", reviewer="r1", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="r2", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="r3", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r1", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r2", human_decision="REJECT"),
        ]
        kappa = compute_review_irr(reviews)
        # r1 and r2 agree on both -> perfect agreement
        assert kappa == 1.0


# ---------------------------------------------------------------------------
# Tests: generate_audit_report
# ---------------------------------------------------------------------------

class TestGenerateAuditReport:
    """Tests for the periodic audit report generator."""

    def test_all_fields_populated(self):
        """Report should have all required fields."""
        reviews = [
            _make_review(drug_id="d1", reviewer="r1", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="r2", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d2", reviewer="r1", pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="r2", pipeline_decision="NO-GO", human_decision="REJECT"),
        ]
        report = generate_audit_report(reviews, "2026-Q1")

        assert report.period == "2026-Q1"
        assert report.n_reviews == 4
        assert isinstance(report.irr_kappa, float)
        assert isinstance(report.kill_rate, float)
        assert isinstance(report.miss_rate, float)
        assert isinstance(report.error_rate, float)
        assert isinstance(report.per_reviewer, dict)
        assert report.target_kill_rate == 0.15
        assert report.target_miss_rate == 0.10

    def test_per_reviewer_metrics(self):
        """Per-reviewer breakdown should be populated."""
        reviews = [
            _make_review(drug_id="d1", reviewer="alice", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d2", reviewer="alice", pipeline_decision="GO", human_decision="REJECT"),
            _make_review(drug_id="d3", reviewer="bob", pipeline_decision="NO-GO", human_decision="REJECT"),
        ]
        report = generate_audit_report(reviews, "2026-Q1")

        assert "alice" in report.per_reviewer
        assert "bob" in report.per_reviewer
        assert report.per_reviewer["alice"]["n_reviews"] == 2.0
        assert report.per_reviewer["bob"]["n_reviews"] == 1.0
        # alice: 1 agree (GO->ADVANCE), 1 disagree (GO->REJECT) -> 0.5 agreement
        assert report.per_reviewer["alice"]["agreement_rate"] == 0.5
        # bob: 1 agree (NO-GO->REJECT) -> 1.0 agreement
        assert report.per_reviewer["bob"]["agreement_rate"] == 1.0

    def test_custom_target_rates(self):
        """Custom target kill/miss rates should be set on the report."""
        reviews = [_make_review()]
        report = generate_audit_report(
            reviews, "2026-Q1",
            target_kill_rate=0.20,
            target_miss_rate=0.05,
        )
        assert report.target_kill_rate == 0.20
        assert report.target_miss_rate == 0.05

    def test_to_dict(self):
        """to_dict() should return a serializable dictionary."""
        reviews = [_make_review()]
        report = generate_audit_report(reviews, "2026-Q1")
        d = report.to_dict()

        assert isinstance(d, dict)
        assert d["period"] == "2026-Q1"
        assert "kill_rate" in d
        assert "miss_rate" in d


# ---------------------------------------------------------------------------
# Tests: AuditReport.summary
# ---------------------------------------------------------------------------

class TestAuditReportSummary:
    """Tests for the text summary output."""

    def test_summary_contains_key_info(self):
        """Summary text should contain period, rates, and reviewer info."""
        reviews = [
            _make_review(drug_id="d1", reviewer="alice", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d1", reviewer="bob", pipeline_decision="GO", human_decision="ADVANCE"),
            _make_review(drug_id="d2", reviewer="alice", pipeline_decision="NO-GO", human_decision="REJECT"),
            _make_review(drug_id="d2", reviewer="bob", pipeline_decision="NO-GO", human_decision="REJECT"),
        ]
        report = generate_audit_report(reviews, "2026-Q1")
        summary = report.summary()

        assert "2026-Q1" in summary
        assert "Kill rate" in summary
        assert "Miss rate" in summary
        assert "IRR Kappa" in summary
        assert "Reviews:" in summary

    def test_summary_shows_ok_for_passing_rates(self):
        """Summary should show 'OK' when rates are within targets."""
        report = AuditReport(
            period="2026-Q1",
            n_reviews=10,
            irr_kappa=0.8,
            kill_rate=0.05,   # below 0.15 target
            miss_rate=0.02,   # below 0.10 target
            error_rate=0.1,
        )
        summary = report.summary()
        assert "OK" in summary

    def test_summary_shows_exceeded_for_failing_rates(self):
        """Summary should show 'EXCEEDED' when rates exceed targets."""
        report = AuditReport(
            period="2026-Q1",
            n_reviews=10,
            irr_kappa=0.3,
            kill_rate=0.25,   # above 0.15 target
            miss_rate=0.20,   # above 0.10 target
            error_rate=0.4,
        )
        summary = report.summary()
        assert "EXCEEDED" in summary

    def test_kill_rate_ok_property(self):
        """kill_rate_ok should be True when kill_rate <= target."""
        report = AuditReport(
            period="test", n_reviews=1, irr_kappa=0,
            kill_rate=0.10, miss_rate=0.05, error_rate=0,
            target_kill_rate=0.15,
        )
        assert report.kill_rate_ok is True

        report.kill_rate = 0.20
        assert report.kill_rate_ok is False

    def test_miss_rate_ok_property(self):
        """miss_rate_ok should be True when miss_rate <= target."""
        report = AuditReport(
            period="test", n_reviews=1, irr_kappa=0,
            kill_rate=0, miss_rate=0.05, error_rate=0,
            target_miss_rate=0.10,
        )
        assert report.miss_rate_ok is True

        report.miss_rate = 0.15
        assert report.miss_rate_ok is False

    def test_summary_includes_per_reviewer(self):
        """Summary should include per-reviewer breakdown when available."""
        report = AuditReport(
            period="test", n_reviews=2, irr_kappa=0.5,
            kill_rate=0.1, miss_rate=0.05, error_rate=0.1,
            per_reviewer={
                "alice": {"agreement_rate": 0.8, "n_reviews": 5.0},
                "bob": {"agreement_rate": 0.6, "n_reviews": 3.0},
            },
        )
        summary = report.summary()
        assert "alice" in summary
        assert "bob" in summary
        assert "Per-reviewer" in summary


# ---------------------------------------------------------------------------
# Tests: load_reviews
# ---------------------------------------------------------------------------

class TestLoadReviews:
    """Tests for loading reviews from CSV files."""

    def test_load_valid_csv(self, tmp_path):
        """Valid CSV rows should produce ReviewRecord objects."""
        csv_path = tmp_path / "reviews.csv"
        _write_review_csv(csv_path, [
            {
                "drug_id": "aspirin",
                "reviewer": "alice",
                "pipeline_decision": "GO",
                "human_decision": "ADVANCE",
                "review_date": "2026-01-15",
                "confidence": "HIGH",
                "notes": "clear benefit",
            },
            {
                "drug_id": "metformin",
                "reviewer": "bob",
                "pipeline_decision": "NO-GO",
                "human_decision": "REJECT",
                "review_date": "2026-01-16",
                "confidence": "MED",
                "notes": "",
            },
        ])

        records = load_reviews(str(csv_path))
        assert len(records) == 2
        assert records[0].drug_id == "aspirin"
        assert records[0].reviewer == "alice"
        assert records[0].pipeline_decision == "GO"
        assert records[0].human_decision == "ADVANCE"
        assert records[1].drug_id == "metformin"

    def test_load_skips_invalid_rows(self, tmp_path):
        """Rows with validation issues should be skipped."""
        csv_path = tmp_path / "reviews.csv"
        _write_review_csv(csv_path, [
            {
                "drug_id": "aspirin",
                "reviewer": "alice",
                "pipeline_decision": "GO",
                "human_decision": "ADVANCE",
                "review_date": "2026-01-15",
                "confidence": "HIGH",
                "notes": "",
            },
            {
                "drug_id": "",  # empty drug_id -> invalid
                "reviewer": "bob",
                "pipeline_decision": "GO",
                "human_decision": "ADVANCE",
                "review_date": "2026-01-16",
                "confidence": "MED",
                "notes": "",
            },
            {
                "drug_id": "metformin",
                "reviewer": "carol",
                "pipeline_decision": "INVALID_DECISION",  # invalid
                "human_decision": "ADVANCE",
                "review_date": "2026-01-17",
                "confidence": "MED",
                "notes": "",
            },
        ])

        records = load_reviews(str(csv_path))
        assert len(records) == 1  # only the valid row
        assert records[0].drug_id == "aspirin"

    def test_load_nonexistent_file_raises(self, tmp_path):
        """Loading from a non-existent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_reviews(str(tmp_path / "nonexistent.csv"))

    def test_load_case_normalization(self, tmp_path):
        """Pipeline/human decisions should be uppercased."""
        csv_path = tmp_path / "reviews.csv"
        _write_review_csv(csv_path, [
            {
                "drug_id": "aspirin",
                "reviewer": "alice",
                "pipeline_decision": "go",
                "human_decision": "advance",
                "review_date": "2026-01-15",
                "confidence": "high",
                "notes": "",
            },
        ])

        records = load_reviews(str(csv_path))
        assert len(records) == 1
        assert records[0].pipeline_decision == "GO"
        assert records[0].human_decision == "ADVANCE"
        assert records[0].confidence == "HIGH"


# ---------------------------------------------------------------------------
# Tests: ReviewRecord.validate
# ---------------------------------------------------------------------------

class TestReviewRecordValidate:
    """Tests for ReviewRecord validation logic."""

    def test_valid_record_no_issues(self):
        """Valid record should have no validation issues."""
        record = _make_review()
        issues = record.validate()
        assert issues == []

    def test_empty_drug_id(self):
        """Empty drug_id should be flagged."""
        record = _make_review(drug_id="")
        issues = record.validate()
        assert any("drug_id" in i for i in issues)

    def test_empty_reviewer(self):
        """Empty reviewer should be flagged."""
        record = _make_review(reviewer="")
        issues = record.validate()
        assert any("reviewer" in i for i in issues)

    def test_invalid_pipeline_decision(self):
        """Invalid pipeline_decision should be flagged."""
        record = _make_review(pipeline_decision="INVALID")
        issues = record.validate()
        assert any("pipeline_decision" in i for i in issues)

    def test_invalid_human_decision(self):
        """Invalid human_decision should be flagged."""
        record = _make_review(human_decision="INVALID")
        issues = record.validate()
        assert any("human_decision" in i for i in issues)

    def test_all_valid_pipeline_decisions(self):
        """All PIPELINE_DECISIONS should pass validation."""
        for decision in PIPELINE_DECISIONS:
            record = _make_review(pipeline_decision=decision)
            issues = record.validate()
            assert not any("pipeline_decision" in i for i in issues), \
                f"{decision} should be valid"

    def test_all_valid_human_decisions(self):
        """All HUMAN_DECISIONS should pass validation."""
        for decision in HUMAN_DECISIONS:
            record = _make_review(human_decision=decision)
            issues = record.validate()
            assert not any("human_decision" in i for i in issues), \
                f"{decision} should be valid"

    def test_multiple_issues(self):
        """Record with multiple problems should report all issues."""
        record = ReviewRecord(
            drug_id="",
            reviewer="",
            pipeline_decision="BAD",
            human_decision="WRONG",
            review_date="2026-01-01",
        )
        issues = record.validate()
        assert len(issues) >= 3  # drug_id, reviewer, pipeline_decision, human_decision
