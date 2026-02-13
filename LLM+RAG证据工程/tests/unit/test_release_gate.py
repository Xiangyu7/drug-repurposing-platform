"""Tests for ReleaseGate - shortlist publication quality enforcement."""

import pytest
import pandas as pd

from src.dr.scoring.release_gate import (
    ReleaseGate,
    ReleaseGateConfig,
    ReleaseCheckResult,
)
from src.dr.evaluation.human_review import ReviewRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shortlist(gates: list[str], n: int | None = None) -> pd.DataFrame:
    """Build a shortlist DataFrame with the given gate decisions.

    If n is provided, it overrides the length derived from gates.
    """
    if n is not None and len(gates) < n:
        gates = gates + ["GO"] * (n - len(gates))
    return pd.DataFrame({
        "canonical_name": [f"drug_{i}" for i in range(len(gates))],
        "drug_id": [f"D{i:03d}" for i in range(len(gates))],
        "gate": gates,
        "endpoint_type": ["CV_EVENTS"] * len(gates),
        "total_score_0_100": [70.0] * len(gates),
        "safety_blacklist_hit": [False] * len(gates),
        "supporting_sentence_count": [5] * len(gates),
        "unique_supporting_pmids_count": [3] * len(gates),
        "harm_or_neutral_sentence_count": [1] * len(gates),
        "topic_match_ratio": [0.8] * len(gates),
        "neg_trials_n": [0] * len(gates),
        "dossier_json": ["{}"] * len(gates),
        "dossier_md": [""] * len(gates),
        "rank_key": list(range(len(gates))),
    })


def _make_good_reviews(n: int = 10) -> list[ReviewRecord]:
    """Create reviews where pipeline and human agree perfectly.

    Each drug gets two reviewers so IRR is computable.
    """
    reviews = []
    for i in range(n):
        drug_id = f"drug_{i}"
        # Pipeline GO -> Human ADVANCE (agreement)
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev1",
            pipeline_decision="GO",
            human_decision="ADVANCE",
            review_date="2026-01-01",
        ))
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev2",
            pipeline_decision="GO",
            human_decision="ADVANCE",
            review_date="2026-01-01",
        ))
    return reviews


def _make_high_kill_reviews(n: int = 10) -> list[ReviewRecord]:
    """Create reviews where many pipeline GO drugs are rejected by humans.

    Kill rate will be high (>50%).
    """
    reviews = []
    for i in range(n):
        drug_id = f"drug_{i}"
        # Pipeline says GO but human says REJECT
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev1",
            pipeline_decision="GO",
            human_decision="REJECT",
            review_date="2026-01-01",
        ))
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev2",
            pipeline_decision="GO",
            human_decision="REJECT",
            review_date="2026-01-01",
        ))
    return reviews


def _make_high_miss_reviews(n: int = 10) -> list[ReviewRecord]:
    """Create reviews where many pipeline NO-GO drugs are advanced by humans.

    Miss rate will be high (>50%).
    """
    reviews = []
    for i in range(n):
        drug_id = f"drug_{i}"
        # Pipeline says NO-GO but human says ADVANCE
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev1",
            pipeline_decision="NO-GO",
            human_decision="ADVANCE",
            review_date="2026-01-01",
        ))
        reviews.append(ReviewRecord(
            drug_id=drug_id,
            reviewer="rev2",
            pipeline_decision="NO-GO",
            human_decision="ADVANCE",
            review_date="2026-01-01",
        ))
    return reviews


def _make_single_reviewer_reviews(n: int = 10) -> list[ReviewRecord]:
    """Create reviews with only a single reviewer per drug (no dual review)."""
    reviews = []
    for i in range(n):
        reviews.append(ReviewRecord(
            drug_id=f"drug_{i}",
            reviewer="rev1",
            pipeline_decision="GO",
            human_decision="ADVANCE",
            review_date="2026-01-01",
        ))
    return reviews


def _write_review_csv(tmp_path, reviews: list[ReviewRecord]) -> str:
    """Write review records to a CSV file and return the path."""
    path = tmp_path / "reviews.csv"
    header = "drug_id,reviewer,pipeline_decision,human_decision,review_date,confidence,notes\n"
    rows = []
    for r in reviews:
        rows.append(
            f"{r.drug_id},{r.reviewer},{r.pipeline_decision},"
            f"{r.human_decision},{r.review_date},{r.confidence},{r.notes}"
        )
    path.write_text(header + "\n".join(rows), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# TestCheckShortlistComposition
# ---------------------------------------------------------------------------

class TestCheckShortlistComposition:
    def test_all_go_no_blockers(self):
        gate = ReleaseGate(ReleaseGateConfig())
        df = _make_shortlist(["GO", "GO", "GO", "GO"])
        result = gate.check_shortlist_composition(df)
        assert result.passed
        assert len(result.blockers) == 0
        assert result.metrics["n_go"] == 4
        assert result.metrics["go_ratio"] == 1.0

    def test_has_nogo_with_block_nogo_true(self):
        cfg = ReleaseGateConfig(block_nogo=True)
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "GO", "NO-GO"])
        result = gate.check_shortlist_composition(df)
        assert not result.passed
        assert any("NO-GO" in b for b in result.blockers)

    def test_nogo_with_block_nogo_false(self):
        cfg = ReleaseGateConfig(block_nogo=False, min_go_ratio=0.3)
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "GO", "NO-GO"])
        result = gate.check_shortlist_composition(df)
        assert result.passed
        assert len(result.blockers) == 0

    def test_low_go_ratio_blocked(self):
        cfg = ReleaseGateConfig(block_nogo=False, min_go_ratio=0.8)
        gate = ReleaseGate(cfg)
        # 2 GO, 3 MAYBE -> go_ratio = 0.4
        df = _make_shortlist(["GO", "GO", "MAYBE", "MAYBE", "MAYBE"])
        result = gate.check_shortlist_composition(df)
        assert not result.passed
        assert any("GO ratio" in b for b in result.blockers)

    def test_empty_shortlist(self):
        gate = ReleaseGate(ReleaseGateConfig())
        df = pd.DataFrame()
        result = gate.check_shortlist_composition(df)
        assert not result.passed
        assert any("empty" in b for b in result.blockers)

    def test_gate_decision_column_also_works(self):
        """Should detect 'gate_decision' column as an alternative to 'gate'."""
        gate = ReleaseGate(ReleaseGateConfig(block_nogo=False, min_go_ratio=0.0))
        df = pd.DataFrame({
            "canonical_name": ["drug_0"],
            "drug_id": ["D000"],
            "gate_decision": ["GO"],
        })
        result = gate.check_shortlist_composition(df)
        assert result.passed
        assert result.metrics["n_go"] == 1


# ---------------------------------------------------------------------------
# TestCheckHumanReview
# ---------------------------------------------------------------------------

class TestCheckHumanReview:
    def test_good_reviews_pass(self):
        cfg = ReleaseGateConfig(
            max_kill_rate=0.15,
            max_miss_rate=0.10,
            min_irr_kappa=0.0,
            require_dual_review=True,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_good_reviews(10))
        assert result.passed
        assert len(result.blockers) == 0
        assert result.metrics["kill_rate"] == 0.0
        assert result.metrics["miss_rate"] == 0.0

    def test_high_kill_rate_blocked(self):
        cfg = ReleaseGateConfig(
            max_kill_rate=0.15,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_high_kill_reviews(10))
        assert not result.passed
        assert any("kill rate" in b for b in result.blockers)

    def test_high_miss_rate_blocked(self):
        cfg = ReleaseGateConfig(
            max_miss_rate=0.10,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_high_miss_reviews(10))
        assert not result.passed
        assert any("miss rate" in b for b in result.blockers)

    def test_low_irr_blocked(self):
        """When IRR is computable but below threshold, it should block."""
        # Mix of agreement and disagreement to get low kappa
        reviews = []
        for i in range(10):
            drug_id = f"drug_{i}"
            reviews.append(ReviewRecord(
                drug_id=drug_id, reviewer="rev1",
                pipeline_decision="GO", human_decision="ADVANCE",
                review_date="2026-01-01",
            ))
            # Second reviewer disagrees half the time
            human = "REJECT" if i % 2 == 0 else "ADVANCE"
            reviews.append(ReviewRecord(
                drug_id=drug_id, reviewer="rev2",
                pipeline_decision="GO", human_decision=human,
                review_date="2026-01-01",
            ))
        cfg = ReleaseGateConfig(
            min_irr_kappa=0.9,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
            require_dual_review=True,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(reviews)
        assert not result.passed
        assert any("IRR kappa" in b for b in result.blockers)

    def test_no_dual_reviews_with_require_dual(self):
        cfg = ReleaseGateConfig(
            require_dual_review=True,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
            min_irr_kappa=0.6,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_single_reviewer_reviews(10))
        assert not result.passed
        assert any("dual reviews required" in b for b in result.blockers)

    def test_reviews_from_path(self, tmp_path):
        """Load reviews from CSV via tmp_path."""
        reviews = _make_good_reviews(6)
        csv_path = _write_review_csv(tmp_path, reviews)
        cfg = ReleaseGateConfig(
            min_review_count=5,
            min_irr_kappa=0.0,
            require_dual_review=False,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(csv_path)
        assert result.passed
        assert result.metrics["n_reviews"] >= 5

    def test_reviews_from_list(self):
        cfg = ReleaseGateConfig(
            min_review_count=3,
            min_irr_kappa=0.0,
            require_dual_review=False,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
        )
        gate = ReleaseGate(cfg)
        reviews = _make_good_reviews(5)
        result = gate.check_human_review(reviews)
        assert result.passed

    def test_too_few_reviews(self):
        cfg = ReleaseGateConfig(min_review_count=100)
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_good_reviews(3))
        assert not result.passed
        assert any("reviews" in b and "minimum" in b for b in result.blockers)


# ---------------------------------------------------------------------------
# TestCheckAll
# ---------------------------------------------------------------------------

class TestCheckAll:
    def test_combines_all_checks(self):
        cfg = ReleaseGateConfig(
            block_nogo=True,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=3,
        )
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "GO", "GO"])
        reviews = _make_good_reviews(5)
        result = gate.check_all(df, reviews=reviews)
        assert result.passed
        assert "composition.n_go" in result.metrics
        assert "review.n_reviews" in result.metrics

    def test_partial_failures(self):
        """Shortlist passes but review fails."""
        cfg = ReleaseGateConfig(
            block_nogo=False,
            min_go_ratio=0.0,
            max_kill_rate=0.01,  # Very strict
            max_miss_rate=1.0,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=5,
        )
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "GO", "GO"])
        reviews = _make_high_kill_reviews(10)
        result = gate.check_all(df, reviews=reviews)
        assert not result.passed
        assert any("kill rate" in b for b in result.blockers)
        # Composition should have passed (no composition blockers)
        assert not any("NO-GO" in b for b in result.blockers)

    def test_all_pass(self):
        cfg = ReleaseGateConfig(
            block_nogo=False,
            min_go_ratio=0.0,
            max_kill_rate=1.0,
            max_miss_rate=1.0,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=3,
        )
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "MAYBE"])
        reviews = _make_good_reviews(5)
        result = gate.check_all(df, reviews=reviews)
        assert result.passed
        assert len(result.blockers) == 0

    def test_check_all_without_reviews(self):
        """When reviews=None, human review check is skipped."""
        cfg = ReleaseGateConfig(block_nogo=False, min_go_ratio=0.0)
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO"])
        result = gate.check_all(df, reviews=None)
        assert result.passed
        assert "review.n_reviews" not in result.metrics


# ---------------------------------------------------------------------------
# TestReleaseCheckResult
# ---------------------------------------------------------------------------

class TestReleaseCheckResult:
    def test_summary_output(self):
        result = ReleaseCheckResult(
            blockers=["issue A"],
            warnings=["warn B"],
            metrics={"n_go": 5, "go_ratio": 0.8},
            strict=True,
        )
        summary = result.summary()
        assert "BLOCKED" in summary
        assert "issue A" in summary
        assert "warn B" in summary
        assert "n_go" in summary

    def test_passed_property_no_blockers(self):
        result = ReleaseCheckResult(blockers=[], strict=True)
        assert result.passed is True

    def test_passed_property_with_blockers(self):
        result = ReleaseCheckResult(blockers=["problem"], strict=True)
        assert result.passed is False

    def test_summary_passed(self):
        result = ReleaseCheckResult(blockers=[], strict=True)
        assert "PASSED" in result.summary()


# ---------------------------------------------------------------------------
# TestSoftMode
# ---------------------------------------------------------------------------

class TestSoftMode:
    def test_blockers_become_warnings(self):
        cfg = ReleaseGateConfig(block_nogo=True, strict=False)
        gate = ReleaseGate(cfg)
        df = _make_shortlist(["GO", "NO-GO"])
        result = gate.check_shortlist_composition(df)
        assert result.passed is True
        assert len(result.blockers) == 0
        assert any("NO-GO" in w for w in result.warnings)

    def test_result_passed_true_even_with_issues(self):
        cfg = ReleaseGateConfig(
            block_nogo=True,
            min_go_ratio=0.9,
            strict=False,
        )
        gate = ReleaseGate(cfg)
        # 1 GO, 3 MAYBE -> GO ratio 0.25 and no NO-GO to block
        df = _make_shortlist(["GO", "MAYBE", "MAYBE", "MAYBE"])
        result = gate.check_shortlist_composition(df)
        assert result.passed is True
        assert len(result.blockers) == 0
        assert len(result.warnings) > 0

    def test_empty_shortlist_soft(self):
        cfg = ReleaseGateConfig(strict=False)
        gate = ReleaseGate(cfg)
        df = pd.DataFrame()
        result = gate.check_shortlist_composition(df)
        assert result.passed is True
        assert len(result.blockers) == 0
        assert any("empty" in w for w in result.warnings)

    def test_human_review_soft_mode(self):
        cfg = ReleaseGateConfig(
            max_kill_rate=0.01,
            min_irr_kappa=0.0,
            require_dual_review=False,
            min_review_count=5,
            strict=False,
        )
        gate = ReleaseGate(cfg)
        result = gate.check_human_review(_make_high_kill_reviews(10))
        assert result.passed is True
        assert len(result.blockers) == 0
        assert any("kill rate" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# TestFromConfig
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_round_trip_from_config_dict(self):
        config_dict = {
            "block_nogo": False,
            "min_go_ratio": 0.6,
            "max_kill_rate": 0.20,
            "max_miss_rate": 0.15,
            "min_irr_kappa": 0.7,
            "require_dual_review": False,
            "min_review_count": 10,
            "strict": False,
        }
        gate = ReleaseGate.from_config(config_dict)
        assert gate.config.block_nogo is False
        assert gate.config.min_go_ratio == 0.6
        assert gate.config.max_kill_rate == 0.20
        assert gate.config.max_miss_rate == 0.15
        assert gate.config.min_irr_kappa == 0.7
        assert gate.config.require_dual_review is False
        assert gate.config.min_review_count == 10
        assert gate.config.strict is False

    def test_default_values(self):
        gate = ReleaseGate.from_config({})
        assert gate.config.block_nogo is True
        assert gate.config.min_go_ratio == 0.5
        assert gate.config.max_kill_rate == 0.15
        assert gate.config.max_miss_rate == 0.10
        assert gate.config.min_irr_kappa == 0.6
        assert gate.config.require_dual_review is True
        assert gate.config.min_review_count == 5
        assert gate.config.strict is True

    def test_partial_config(self):
        gate = ReleaseGate.from_config({"block_nogo": False, "strict": False})
        assert gate.config.block_nogo is False
        assert gate.config.strict is False
        # Rest should be defaults
        assert gate.config.min_go_ratio == 0.5
        assert gate.config.max_kill_rate == 0.15
