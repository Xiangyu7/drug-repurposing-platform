"""Gating Engine - GO/MAYBE/NO-GO decisions for drug candidates

Applies hard gates (disqualifying criteria) and soft gates (scoring thresholds)
to determine which drugs should advance to validation.

Gates:
- Hard gates: Immediate disqualification (e.g., < 2 benefit papers)
- Soft gates: Score-based thresholds (e.g., total score < 50)
- Explore track: Keeps high-novelty candidates in MAYBE instead of hard filtering
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Tuple
from enum import Enum

from ..logger import get_logger
try:
    from ..monitoring import track_gating_decision
except Exception:  # pragma: no cover - monitoring is optional at runtime
    def track_gating_decision(decision: str, gate_reasons: list = None):
        return None

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
    # v2: relaxed from 2→1 to avoid killing novel candidates with sparse literature.
    # A single benefit paper is sufficient evidence to warrant further investigation;
    # the scoring system already penalizes low-evidence drugs continuously.
    min_benefit_papers: int = 1
    max_harm_ratio: float = 0.5  # If harm > 50% of classified, reject
    min_total_pmids: int = 2  # relaxed from 3→2
    # Blacklist as soft gate (v2): blacklisted drugs get a scoring penalty but are
    # NOT hard-rejected.  This preserves recall for repurposing — a drug with known
    # systemic risk (e.g. prednisone) may still be a strong candidate for a
    # specific indication.  NO-GO drugs are output to explore_nogo.csv for review.
    blacklist_is_hard_gate: bool = False

    # Soft gates (scoring)
    go_threshold: float = 60.0
    maybe_threshold: float = 40.0

    # Explore track (recall-first lane for repurposing discovery)
    # v2: lowered thresholds to be more permissive — the purpose of explore
    # track is to preserve novel candidates that hard gates would kill.
    enable_explore_track: bool = True
    explore_min_total_pmids: int = 1
    explore_min_benefit: int = 0   # relaxed: 0 benefit OK for explore (mechanism-only candidates)
    explore_min_novelty_score: float = 0.30  # relaxed from 0.45
    explore_max_harm_ratio: float = 0.70     # relaxed from 0.60
    explore_maybe_floor: float = 15.0        # relaxed from 25.0


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
    decision_channel: str = "exploit"
    novelty_score: float = 0.0
    uncertainty_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "decision": self.decision.value,
            "gate_reasons": self.gate_reasons,
            "scores": self.scores,
            "metrics": self.metrics,
            "decision_channel": self.decision_channel,
            "novelty_score": round(float(self.novelty_score), 4),
            "uncertainty_score": round(float(self.uncertainty_score), 4),
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
        novelty_score = self._compute_novelty_score(dossier, benefit, harm, neutral, unknown, total_pmids, total_score)
        uncertainty_score = self._compute_uncertainty_score(benefit, harm, neutral, unknown, total_pmids)
        harm_ratio = self._harm_ratio(benefit, harm, neutral)

        # Collect metrics
        metrics = {
            "benefit": benefit,
            "harm": harm,
            "neutral": neutral,
            "unknown": unknown,
            "total_pmids": total_pmids,
            "total_score": total_score,
            "safety_score": safety_score,
            "harm_ratio": round(harm_ratio, 4),
            "novelty_score": round(float(novelty_score), 4),
            "uncertainty_score": round(float(uncertainty_score), 4),
            "route_coverage": int(((dossier.get("retrieval") or {}).get("route_coverage", 0)) or 0),
            "cross_disease_hits": int(((dossier.get("retrieval") or {}).get("cross_disease_hits", 0)) or 0),
        }

        # Apply hard gates
        hard_gate_reasons = self._check_hard_gates(
            benefit, harm, neutral, total_pmids, canonical, safety_score
        )

        if hard_gate_reasons:
            if self._eligible_for_explore(
                total_score=total_score,
                benefit=benefit,
                harm_ratio=harm_ratio,
                total_pmids=total_pmids,
                novelty_score=novelty_score,
            ):
                reasons = hard_gate_reasons + ["explore_track_override"]
                logger.info("MAYBE (explore override): %s - %s", canonical, "; ".join(reasons))
                decision_obj = GatingDecision(
                    decision=GateDecision.MAYBE,
                    gate_reasons=reasons,
                    scores=scores,
                    metrics=metrics,
                    decision_channel="explore",
                    novelty_score=novelty_score,
                    uncertainty_score=uncertainty_score,
                )
                track_gating_decision(decision_obj.decision.value, decision_obj.gate_reasons)
                return decision_obj

            logger.info("NO-GO (hard gates): %s - %s", canonical, "; ".join(hard_gate_reasons))
            decision_obj = GatingDecision(
                decision=GateDecision.NO_GO,
                gate_reasons=hard_gate_reasons,
                scores=scores,
                metrics=metrics,
                decision_channel="exploit",
                novelty_score=novelty_score,
                uncertainty_score=uncertainty_score,
            )
            track_gating_decision(decision_obj.decision.value, decision_obj.gate_reasons)
            return decision_obj

        # Apply soft gates (scoring)
        soft_gate_result, soft_gate_reasons = self._check_soft_gates(total_score)
        decision_channel = "exploit"

        if (
            soft_gate_result in {GateDecision.MAYBE, GateDecision.NO_GO}
            and self._eligible_for_explore(
                total_score=total_score,
                benefit=benefit,
                harm_ratio=harm_ratio,
                total_pmids=total_pmids,
                novelty_score=novelty_score,
            )
        ):
            soft_gate_result = GateDecision.MAYBE
            if "explore_track" not in soft_gate_reasons:
                soft_gate_reasons = list(soft_gate_reasons) + ["explore_track"]
            decision_channel = "explore"

        if soft_gate_result == GateDecision.NO_GO:
            logger.info("NO-GO (soft gates): %s - %s", canonical, "; ".join(soft_gate_reasons))
        elif soft_gate_result == GateDecision.MAYBE:
            logger.info(
                "MAYBE (%s): %s - %s",
                decision_channel,
                canonical,
                "; ".join(soft_gate_reasons) if soft_gate_reasons else "borderline score",
            )
        else:
            logger.info("GO: %s (score=%.1f)", canonical, total_score)

        decision_obj = GatingDecision(
            decision=soft_gate_result,
            gate_reasons=soft_gate_reasons,
            scores=scores,
            metrics=metrics,
            decision_channel=decision_channel,
            novelty_score=novelty_score,
            uncertainty_score=uncertainty_score,
        )
        track_gating_decision(decision_obj.decision.value, decision_obj.gate_reasons)
        return decision_obj

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

    def _harm_ratio(self, benefit: int, harm: int, neutral: int) -> float:
        total_classified = benefit + harm + neutral
        if total_classified <= 0:
            return 0.0
        return float(harm) / float(total_classified)

    def _compute_novelty_score(
        self,
        dossier: Dict[str, Any],
        benefit: int,
        harm: int,
        neutral: int,
        unknown: int,
        total_pmids: int,
        total_score: float,
    ) -> float:
        retrieval = dossier.get("retrieval") or {}
        route_coverage = int(retrieval.get("route_coverage", 0) or 0)
        cross_disease_hits = int(retrieval.get("cross_disease_hits", 0) or 0)
        routes_total = int(retrieval.get("routes_total", 0) or 0)
        llm = dossier.get("llm_structured") or {}
        mechanisms = llm.get("proposed_mechanisms") or []

        novelty = 0.0
        if routes_total > 0:
            novelty += min(0.35, (route_coverage / max(1, routes_total)) * 0.35)
        novelty += min(0.25, float(cross_disease_hits) * 0.08)
        novelty += min(0.20, len(mechanisms) / 8.0 * 0.20)

        # Reward "not fully proven yet" candidates to avoid precision-only collapse.
        if benefit >= 1 and total_score < self.config.go_threshold:
            novelty += 0.10
        if total_pmids <= 3 and (benefit + harm + neutral + unknown) > 0:
            novelty += 0.05
        return max(0.0, min(1.0, novelty))

    def _compute_uncertainty_score(
        self,
        benefit: int,
        harm: int,
        neutral: int,
        unknown: int,
        total_pmids: int,
    ) -> float:
        total = benefit + harm + neutral + unknown
        unknown_ratio = (unknown / total) if total > 0 else 1.0
        low_coverage = 1.0 - min(1.0, float(total_pmids) / 12.0)
        class_imbalance = 1.0 if (benefit + harm + neutral) == 0 else 0.0
        uncertainty = 0.50 * unknown_ratio + 0.35 * low_coverage + 0.15 * class_imbalance
        return max(0.0, min(1.0, uncertainty))

    def _eligible_for_explore(
        self,
        total_score: float,
        benefit: int,
        harm_ratio: float,
        total_pmids: int,
        novelty_score: float,
    ) -> bool:
        if not self.config.enable_explore_track:
            return False
        if novelty_score < self.config.explore_min_novelty_score:
            return False
        if total_pmids < self.config.explore_min_total_pmids:
            return False
        if benefit < self.config.explore_min_benefit:
            return False
        if harm_ratio > self.config.explore_max_harm_ratio:
            return False
        if total_score < self.config.explore_maybe_floor:
            return False
        return True

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
