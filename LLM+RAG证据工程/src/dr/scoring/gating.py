"""Gating Engine - GO/MAYBE/NO-GO decisions for drug candidates

Applies hard gates (disqualifying criteria) and soft gates (scoring thresholds)
to determine which drugs should advance to validation.

Gates:
- Hard gates: Immediate disqualification (e.g., < 2 benefit papers)
- Soft gates: Score-based thresholds (e.g., total score < 50)
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List
from enum import Enum

from ..logger import get_logger

logger = get_logger(__name__)


class GateDecision(str, Enum):
    """Gating decision values"""
    GO = "GO"
    MAYBE = "MAYBE"
    NO_GO = "NO-GO"


@dataclass
class GatingConfig:
    """Configuration for gating decisions

    Hard gates (immediate NO-GO):
    - min_benefit_papers: Minimum benefit evidence required
    - max_harm_ratio: Maximum harm/(benefit+harm) ratio allowed
    - min_total_pmids: Minimum literature coverage

    Soft gates (scoring thresholds):
    - go_threshold: Score for GO decision
    - maybe_threshold: Score for MAYBE decision
    """
    # Hard gates
    min_benefit_papers: int = 2
    max_harm_ratio: float = 0.5  # If harm > 50% of classified, reject
    min_total_pmids: int = 3
    blacklist_is_hard_gate: bool = True

    # Soft gates (scoring)
    go_threshold: float = 60.0
    maybe_threshold: float = 40.0


@dataclass
class GatingDecision:
    """Result of gating analysis

    Attributes:
        decision: GO/MAYBE/NO-GO
        gate_reasons: List of reasons for NO-GO or MAYBE
        scores: Dict of scores
        metrics: Dict of evidence metrics
    """
    decision: GateDecision
    gate_reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "decision": self.decision.value,
            "gate_reasons": self.gate_reasons,
            "scores": self.scores,
            "metrics": self.metrics
        }


class GatingEngine:
    """Applies gating rules to determine drug advancement decisions

    Example:
        >>> engine = GatingEngine()
        >>> decision = engine.evaluate(dossier, scores)
        >>> print(decision.decision)
        GateDecision.GO
    """

    def __init__(self, config: GatingConfig = None):
        """Initialize gating engine

        Args:
            config: Gating configuration (uses defaults if None)
        """
        self.config = config or GatingConfig()
        logger.info("GatingEngine initialized")

    def evaluate(
        self,
        dossier: Dict[str, Any],
        scores: Dict[str, float],
        canonical_name: str = ""
    ) -> GatingDecision:
        """Evaluate gating decision for a drug

        Args:
            dossier: Drug dossier from Step6
            scores: Scores from DrugScorer
            canonical_name: Drug canonical name (for blacklist check)

        Returns:
            GatingDecision with decision and reasons
        """
        drug_id = dossier.get("drug_id", "unknown")
        canonical = canonical_name or dossier.get("canonical_name", "unknown")

        logger.debug("Evaluating gates for: %s (%s)", canonical, drug_id)

        # Extract metrics
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        harm = evidence_count.get("harm", 0)
        neutral = evidence_count.get("neutral", 0)
        unknown = evidence_count.get("unknown", 0)
        total_pmids = dossier.get("total_pmids", 0)
        total_score = scores.get("total_score_0_100", 0.0)
        safety_score = scores.get("safety_fit_0_20", 0.0)

        # Collect metrics
        metrics = {
            "benefit": benefit,
            "harm": harm,
            "neutral": neutral,
            "unknown": unknown,
            "total_pmids": total_pmids,
            "total_score": total_score,
            "safety_score": safety_score
        }

        # Apply hard gates
        hard_gate_reasons = self._check_hard_gates(
            benefit, harm, neutral, total_pmids, canonical, safety_score
        )

        if hard_gate_reasons:
            logger.info("NO-GO (hard gates): %s - %s", canonical, "; ".join(hard_gate_reasons))
            return GatingDecision(
                decision=GateDecision.NO_GO,
                gate_reasons=hard_gate_reasons,
                scores=scores,
                metrics=metrics
            )

        # Apply soft gates (scoring)
        soft_gate_result, soft_gate_reasons = self._check_soft_gates(total_score)

        if soft_gate_result == GateDecision.NO_GO:
            logger.info("NO-GO (soft gates): %s - %s", canonical, "; ".join(soft_gate_reasons))
        elif soft_gate_result == GateDecision.MAYBE:
            logger.info("MAYBE: %s - %s", canonical, "; ".join(soft_gate_reasons) if soft_gate_reasons else "borderline score")
        else:
            logger.info("GO: %s (score=%.1f)", canonical, total_score)

        return GatingDecision(
            decision=soft_gate_result,
            gate_reasons=soft_gate_reasons,
            scores=scores,
            metrics=metrics
        )

    def _check_hard_gates(
        self,
        benefit: int,
        harm: int,
        neutral: int,
        total_pmids: int,
        canonical_name: str,
        safety_score: float
    ) -> List[str]:
        """Check hard gates (immediate disqualification)

        Args:
            benefit: Number of benefit papers
            harm: Number of harm papers
            neutral: Number of neutral papers
            total_pmids: Total PMIDs
            canonical_name: Drug name
            safety_score: Safety score

        Returns:
            List of reasons for NO-GO (empty if passes all gates)
        """
        reasons = []

        # Gate 1: Minimum benefit evidence
        if benefit < self.config.min_benefit_papers:
            reasons.append(f"benefit<{self.config.min_benefit_papers}")

        # Gate 2: Minimum literature coverage
        if total_pmids < self.config.min_total_pmids:
            reasons.append(f"pmids<{self.config.min_total_pmids}")

        # Gate 3: Harm ratio (if too much harm relative to benefit)
        total_classified = benefit + harm + neutral
        if total_classified > 0:
            harm_ratio = harm / total_classified
            if harm_ratio > self.config.max_harm_ratio:
                reasons.append(f"harm_ratio>{self.config.max_harm_ratio:.1f}")

        # Gate 4: Safety blacklist (if configured as hard gate)
        if self.config.blacklist_is_hard_gate:
            # Check if safety_score < 15 (indicates blacklist hit or major safety issues)
            if safety_score < 15.0:
                reasons.append("safety_concern")

        return reasons

    def _check_soft_gates(
        self,
        total_score: float
    ) -> tuple[GateDecision, List[str]]:
        """Check soft gates (score-based thresholds)

        Args:
            total_score: Total score (0-100)

        Returns:
            (decision, reasons) tuple
        """
        reasons = []

        if total_score >= self.config.go_threshold:
            return GateDecision.GO, []
        elif total_score >= self.config.maybe_threshold:
            reasons.append(f"score<{self.config.go_threshold}")
            return GateDecision.MAYBE, reasons
        else:
            reasons.append(f"score<{self.config.maybe_threshold}")
            return GateDecision.NO_GO, reasons

    def batch_evaluate(
        self,
        dossiers: List[Dict[str, Any]],
        scores_list: List[Dict[str, float]]
    ) -> List[GatingDecision]:
        """Evaluate multiple drugs at once

        Args:
            dossiers: List of drug dossiers
            scores_list: List of score dicts (same order as dossiers)

        Returns:
            List of GatingDecisions
        """
        if len(dossiers) != len(scores_list):
            raise ValueError("dossiers and scores_list must have same length")

        decisions = []
        for dossier, scores in zip(dossiers, scores_list):
            canonical = dossier.get("canonical_name", "unknown")
            decision = self.evaluate(dossier, scores, canonical)
            decisions.append(decision)

        # Log summary
        go_count = sum(1 for d in decisions if d.decision == GateDecision.GO)
        maybe_count = sum(1 for d in decisions if d.decision == GateDecision.MAYBE)
        no_go_count = sum(1 for d in decisions if d.decision == GateDecision.NO_GO)

        logger.info("Batch gating complete: %d GO, %d MAYBE, %d NO-GO",
                   go_count, maybe_count, no_go_count)

        return decisions
