"""Human review metrics: IRR, error rates, and periodic audit reports.

Tracks:
- Inter-Rater Reliability (Cohen's Kappa) across review rounds
- Kill rate: fraction of pipeline GO drugs rejected by human review
- Miss rate: fraction of human-selected drugs that pipeline ranked as NO-GO
- Per-reviewer accuracy calibration

Usage:
    reviews = load_reviews("human_reviews.csv")
    report = generate_audit_report(reviews, "2026-Q1")
    print(f"Kill rate: {report.kill_rate:.1%}, Miss rate: {report.miss_rate:.1%}")
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logger import get_logger

logger = get_logger(__name__)


# Valid decision values
PIPELINE_DECISIONS = {"GO", "MAYBE", "NO-GO"}
HUMAN_DECISIONS = {"ADVANCE", "HOLD", "REJECT"}


@dataclass
class ReviewRecord:
    """A single human review of a gating decision.

    Attributes:
        drug_id: Drug identifier
        reviewer: Reviewer name
        pipeline_decision: Pipeline's gating decision (GO/MAYBE/NO-GO)
        human_decision: Human reviewer's decision (ADVANCE/HOLD/REJECT)
        review_date: ISO date string
        confidence: Review confidence (HIGH/MED/LOW)
        notes: Reviewer notes
    """
    drug_id: str
    reviewer: str
    pipeline_decision: str
    human_decision: str
    review_date: str
    confidence: str = "MED"
    notes: str = ""

    def validate(self) -> List[str]:
        issues = []
        if not self.drug_id:
            issues.append("drug_id is empty")
        if not self.reviewer:
            issues.append("reviewer is empty")
        if self.pipeline_decision not in PIPELINE_DECISIONS:
            issues.append(f"invalid pipeline_decision: {self.pipeline_decision}")
        if self.human_decision not in HUMAN_DECISIONS:
            issues.append(f"invalid human_decision: {self.human_decision}")
        return issues


@dataclass
class AuditReport:
    """Summary of human review performance over a period.

    Attributes:
        period: Reporting period (e.g., "2026-Q1")
        n_reviews: Total number of reviews
        irr_kappa: Inter-rater reliability (Cohen's Kappa), -1 if not computable
        kill_rate: Pipeline GO → Human REJECT rate
        miss_rate: Pipeline NO-GO → Human ADVANCE rate
        error_rate: Overall disagreement rate
        per_reviewer: Per-reviewer metrics
        target_kill_rate: Target max kill rate
        target_miss_rate: Target max miss rate
        details: Detailed breakdown
    """
    period: str
    n_reviews: int
    irr_kappa: float
    kill_rate: float
    miss_rate: float
    error_rate: float
    per_reviewer: Dict[str, Dict[str, float]] = field(default_factory=dict)
    target_kill_rate: float = 0.15
    target_miss_rate: float = 0.10
    details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def kill_rate_ok(self) -> bool:
        return self.kill_rate <= self.target_kill_rate

    @property
    def miss_rate_ok(self) -> bool:
        return self.miss_rate <= self.target_miss_rate

    def summary(self) -> str:
        lines = [
            f"=== Audit Report: {self.period} ===",
            f"Reviews: {self.n_reviews}",
            f"IRR Kappa: {self.irr_kappa:.3f}",
            f"Kill rate: {self.kill_rate:.1%} (target: <={self.target_kill_rate:.1%}) "
            f"{'OK' if self.kill_rate_ok else 'EXCEEDED'}",
            f"Miss rate: {self.miss_rate:.1%} (target: <={self.target_miss_rate:.1%}) "
            f"{'OK' if self.miss_rate_ok else 'EXCEEDED'}",
            f"Error rate: {self.error_rate:.1%}",
        ]
        if self.per_reviewer:
            lines.append("Per-reviewer:")
            for reviewer, metrics in self.per_reviewer.items():
                lines.append(
                    f"  {reviewer}: agree={metrics.get('agreement_rate', 0):.1%}, "
                    f"n={metrics.get('n_reviews', 0)}"
                )
        return "\n".join(lines)


def compute_review_irr(reviews: List[ReviewRecord]) -> float:
    """Compute IRR (Cohen's Kappa) across reviewers for the same drugs.

    Requires at least 2 reviewers who reviewed the same drugs.
    Uses the simplified decision mapping:
        Pipeline GO  + Human ADVANCE = "agree_advance"
        Pipeline NO-GO + Human REJECT = "agree_reject"
        Otherwise = disagreement

    Returns:
        Cohen's Kappa, or -1.0 if not computable.
    """
    # Group by drug_id
    by_drug: Dict[str, List[ReviewRecord]] = defaultdict(list)
    for r in reviews:
        by_drug[r.drug_id].append(r)

    # Find drugs with 2+ reviewers
    labels_a: List[str] = []
    labels_b: List[str] = []

    for drug_id, drug_reviews in by_drug.items():
        if len(drug_reviews) < 2:
            continue
        # Use first two reviewers
        labels_a.append(drug_reviews[0].human_decision)
        labels_b.append(drug_reviews[1].human_decision)

    if len(labels_a) < 2:
        logger.warning("Not enough dual-reviewed drugs for IRR computation")
        return -1.0

    # Import from annotation module
    from .annotation import compute_cohens_kappa
    try:
        return compute_cohens_kappa(labels_a, labels_b)
    except ValueError:
        return -1.0


def compute_error_rates(reviews: List[ReviewRecord]) -> Dict[str, float]:
    """Compute kill rate, miss rate, and overall error rate.

    Kill rate: P(human=REJECT | pipeline=GO)
    Miss rate: P(human=ADVANCE | pipeline=NO-GO)
    Error rate: overall disagreement between pipeline intent and human decision

    Returns:
        {"kill_rate": float, "miss_rate": float, "error_rate": float}
    """
    if not reviews:
        return {"kill_rate": 0.0, "miss_rate": 0.0, "error_rate": 0.0}

    # Kill: pipeline says GO, human says REJECT
    go_reviews = [r for r in reviews if r.pipeline_decision == "GO"]
    go_rejected = [r for r in go_reviews if r.human_decision == "REJECT"]
    kill_rate = len(go_rejected) / len(go_reviews) if go_reviews else 0.0

    # Miss: pipeline says NO-GO, human says ADVANCE
    nogo_reviews = [r for r in reviews if r.pipeline_decision == "NO-GO"]
    nogo_advanced = [r for r in nogo_reviews if r.human_decision == "ADVANCE"]
    miss_rate = len(nogo_advanced) / len(nogo_reviews) if nogo_reviews else 0.0

    # Overall error: any disagreement in intent
    # GO↔ADVANCE, NO-GO↔REJECT, MAYBE↔HOLD
    intent_map = {"GO": "ADVANCE", "NO-GO": "REJECT", "MAYBE": "HOLD"}
    errors = sum(
        1 for r in reviews
        if intent_map.get(r.pipeline_decision) != r.human_decision
    )
    error_rate = errors / len(reviews) if reviews else 0.0

    return {
        "kill_rate": round(kill_rate, 4),
        "miss_rate": round(miss_rate, 4),
        "error_rate": round(error_rate, 4),
    }


def generate_audit_report(
    reviews: List[ReviewRecord],
    period: str,
    target_kill_rate: float = 0.15,
    target_miss_rate: float = 0.10,
) -> AuditReport:
    """Generate a periodic audit report with targets comparison.

    Args:
        reviews: List of review records for the period
        period: Reporting period identifier (e.g., "2026-Q1")
        target_kill_rate: Target maximum kill rate
        target_miss_rate: Target maximum miss rate

    Returns:
        AuditReport with full metrics
    """
    irr = compute_review_irr(reviews)
    rates = compute_error_rates(reviews)

    # Per-reviewer metrics
    by_reviewer: Dict[str, List[ReviewRecord]] = defaultdict(list)
    for r in reviews:
        by_reviewer[r.reviewer].append(r)

    per_reviewer: Dict[str, Dict[str, float]] = {}
    intent_map = {"GO": "ADVANCE", "NO-GO": "REJECT", "MAYBE": "HOLD"}
    for reviewer, rev_reviews in by_reviewer.items():
        n = len(rev_reviews)
        agree = sum(
            1 for r in rev_reviews
            if intent_map.get(r.pipeline_decision) == r.human_decision
        )
        per_reviewer[reviewer] = {
            "n_reviews": float(n),
            "agreement_rate": round(agree / n, 4) if n > 0 else 0.0,
        }

    report = AuditReport(
        period=period,
        n_reviews=len(reviews),
        irr_kappa=round(irr, 4),
        kill_rate=rates["kill_rate"],
        miss_rate=rates["miss_rate"],
        error_rate=rates["error_rate"],
        per_reviewer=per_reviewer,
        target_kill_rate=target_kill_rate,
        target_miss_rate=target_miss_rate,
    )

    logger.info("Audit report generated for %s: %s", period, report.summary())
    return report


def load_reviews(path: str) -> List[ReviewRecord]:
    """Load review records from CSV.

    Expected columns: drug_id, reviewer, pipeline_decision, human_decision,
                      review_date, confidence, notes
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Review file not found: {path}")

    records: List[ReviewRecord] = []
    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            record = ReviewRecord(
                drug_id=row.get("drug_id", "").strip(),
                reviewer=row.get("reviewer", "").strip(),
                pipeline_decision=row.get("pipeline_decision", "").strip().upper(),
                human_decision=row.get("human_decision", "").strip().upper(),
                review_date=row.get("review_date", "").strip(),
                confidence=row.get("confidence", "MED").strip().upper(),
                notes=row.get("notes", "").strip(),
            )
            issues = record.validate()
            if issues:
                logger.warning("Review row %d has issues: %s", i, issues)
            else:
                records.append(record)

    logger.info("Loaded %d review records from %s", len(records), path)
    return records
