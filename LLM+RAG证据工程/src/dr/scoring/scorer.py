"""Drug Scorer - Calculate multi-dimensional scores from evidence

Scores drugs on 5 dimensions based on Step6 dossier evidence:
1. Evidence Strength (0-30 pts): Quality and quantity of supporting evidence
2. Mechanism Plausibility (0-20 pts): Biological rationale
3. Translatability (0-20 pts): Clinical trial feasibility
4. Safety Fit (0-20 pts): Safety profile and risks
5. Practicality (0-10 pts): Implementation feasibility

Total: 0-100 points
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
from pathlib import Path

from ..logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScoringConfig:
    """Configuration for drug scoring

    Attributes:
        min_benefit_for_high_evidence: Minimum benefit papers for high evidence score
        min_benefit_for_med_evidence: Minimum benefit papers for medium evidence score
        harm_penalty_per_paper: Points deducted per harm paper
        neutral_penalty_per_paper: Points deducted per neutral paper
        min_pmids_for_mechanism: Minimum PMIDs to justify mechanism score
        safety_blacklist_penalty: Points deducted if drug on safety blacklist
    """
    # Evidence strength thresholds
    min_benefit_for_high_evidence: int = 10
    min_benefit_for_med_evidence: int = 5
    min_benefit_for_low_evidence: int = 2

    # Penalties
    harm_penalty_per_paper: float = 1.0
    neutral_penalty_per_paper: float = 0.5

    # Mechanism scoring
    min_pmids_for_mechanism: int = 5

    # Safety
    safety_blacklist_penalty: float = 6.0
    safety_blacklist_patterns: list = None

    def __post_init__(self):
        if self.safety_blacklist_patterns is None:
            # Common drugs with known safety concerns
            self.safety_blacklist_patterns = [
                r"\bdexamethasone\b",
                r"\bprednisone\b",
                r"\bprednisolone\b",
                r"\bhydrocortisone\b",
                r"\bwarfarin\b",
            ]


class DrugScorer:
    """Calculates multi-dimensional scores for drugs based on evidence

    Example:
        >>> scorer = DrugScorer()
        >>> dossier = json.load(open("dossier.json"))
        >>> scores = scorer.score_drug(dossier)
        >>> print(f"Total: {scores['total_score_0_100']}")
        Total: 76.0
    """

    def __init__(self, config: Optional[ScoringConfig] = None):
        """Initialize scorer with configuration

        Args:
            config: Scoring configuration (uses defaults if None)
        """
        self.config = config or ScoringConfig()
        logger.info("DrugScorer initialized")

    def score_drug(self, dossier: Dict[str, Any]) -> Dict[str, float]:
        """Calculate all scores for a drug

        Args:
            dossier: Drug dossier from Step6 (JSON format)

        Returns:
            Dictionary with individual and total scores:
            {
                "evidence_strength_0_30": float,
                "mechanism_plausibility_0_20": float,
                "translatability_0_20": float,
                "safety_fit_0_20": float,
                "practicality_0_10": float,
                "total_score_0_100": float
            }
        """
        drug_id = dossier.get("drug_id", "unknown")
        canonical = dossier.get("canonical_name", "unknown")

        logger.debug("Scoring drug: %s (%s)", canonical, drug_id)

        # Calculate individual dimensions
        evidence_score = self._score_evidence_strength(dossier)
        mechanism_score = self._score_mechanism_plausibility(dossier)
        translatability_score = self._score_translatability(dossier)
        safety_score = self._score_safety_fit(dossier, canonical)
        practicality_score = self._score_practicality(dossier)

        total = (
            evidence_score +
            mechanism_score +
            translatability_score +
            safety_score +
            practicality_score
        )

        scores = {
            "evidence_strength_0_30": round(evidence_score, 1),
            "mechanism_plausibility_0_20": round(mechanism_score, 1),
            "translatability_0_20": round(translatability_score, 1),
            "safety_fit_0_20": round(safety_score, 1),
            "practicality_0_10": round(practicality_score, 1),
            "total_score_0_100": round(total, 1)
        }

        logger.debug("Scores for %s: total=%.1f (evidence=%.1f, mechanism=%.1f, trans=%.1f, safety=%.1f, pract=%.1f)",
                    canonical, total, evidence_score, mechanism_score,
                    translatability_score, safety_score, practicality_score)

        return scores

    def _score_evidence_strength(self, dossier: Dict[str, Any]) -> float:
        """Score evidence strength (0-30 points)

        Based on:
        - Number of benefit papers (primary factor)
        - Total PMIDs (coverage)
        - Penalties for harm/neutral papers

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-30
        """
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        harm = evidence_count.get("harm", 0)
        neutral = evidence_count.get("neutral", 0)
        total_pmids = dossier.get("total_pmids", 0)

        # Base score from benefit papers (0-30)
        if benefit >= self.config.min_benefit_for_high_evidence:
            base_score = 30.0
        elif benefit >= self.config.min_benefit_for_med_evidence:
            # Linear interpolation between med and high
            ratio = (benefit - self.config.min_benefit_for_med_evidence) / \
                    (self.config.min_benefit_for_high_evidence - self.config.min_benefit_for_med_evidence)
            base_score = 15.0 + ratio * 15.0
        elif benefit >= self.config.min_benefit_for_low_evidence:
            # Linear interpolation between low and med
            ratio = (benefit - self.config.min_benefit_for_low_evidence) / \
                    (self.config.min_benefit_for_med_evidence - self.config.min_benefit_for_low_evidence)
            base_score = 8.0 + ratio * 7.0
        elif benefit == 1:
            base_score = 4.0
        else:
            base_score = 0.0

        # Penalty for harm/neutral evidence
        penalty = (
            harm * self.config.harm_penalty_per_paper +
            neutral * self.config.neutral_penalty_per_paper
        )

        # Bonus for high coverage (more PMIDs screened)
        coverage_bonus = min(3.0, total_pmids / 30.0)

        final_score = max(0.0, min(30.0, base_score - penalty + coverage_bonus))

        return final_score

    def _score_mechanism_plausibility(self, dossier: Dict[str, Any]) -> float:
        """Score mechanism plausibility (0-20 points)

        Based on:
        - Number of supporting PMIDs (shows mechanism is studied)
        - Benefit/total ratio (consistency of evidence)

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-20
        """
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        harm = evidence_count.get("harm", 0)
        neutral = evidence_count.get("neutral", 0)
        unknown = evidence_count.get("unknown", 0)

        total_classified = benefit + harm + neutral
        total_all = total_classified + unknown

        if total_all == 0:
            return 6.0  # Minimal score for no evidence

        # Base score from number of papers (mechanism is studied)
        total_pmids = dossier.get("total_pmids", 0)
        if total_pmids >= 50:
            base_score = 12.0
        elif total_pmids >= 20:
            base_score = 10.0
        elif total_pmids >= self.config.min_pmids_for_mechanism:
            base_score = 8.0
        else:
            base_score = 6.0

        # Consistency bonus (benefit ratio)
        if total_classified > 0:
            benefit_ratio = benefit / total_classified
            consistency_bonus = benefit_ratio * 8.0  # Up to 8 points
        else:
            consistency_bonus = 0.0

        final_score = min(20.0, base_score + consistency_bonus)

        return final_score

    def _score_translatability(self, dossier: Dict[str, Any]) -> float:
        """Score translatability to clinical trials (0-20 points)

        Based on:
        - Total PMIDs (shows clinical research interest)
        - Benefit papers (positive findings attract funding)

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-20
        """
        total_pmids = dossier.get("total_pmids", 0)
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)

        # Base score from research activity
        if total_pmids >= 50:
            base_score = 12.0
        elif total_pmids >= 20:
            base_score = 10.0
        elif total_pmids >= 10:
            base_score = 8.0
        elif total_pmids >= 5:
            base_score = 6.0
        else:
            base_score = 4.0

        # Bonus for strong benefit evidence (attracts trial funding)
        if benefit >= 10:
            benefit_bonus = 8.0
        elif benefit >= 5:
            benefit_bonus = 4.0
        elif benefit >= 2:
            benefit_bonus = 2.0
        else:
            benefit_bonus = 0.0

        final_score = min(20.0, base_score + benefit_bonus)

        return final_score

    def _score_safety_fit(self, dossier: Dict[str, Any], canonical_name: str) -> float:
        """Score safety fit (0-20 points)

        Based on:
        - Harm evidence (major penalty)
        - Safety blacklist (drugs with known safety issues)
        - Benefit/harm ratio

        Args:
            dossier: Drug dossier
            canonical_name: Drug canonical name

        Returns:
            Score from 0-20
        """
        import re

        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        harm = evidence_count.get("harm", 0)

        # Start with full score
        base_score = 20.0

        # Check safety blacklist
        blacklist_hit = False
        for pattern in self.config.safety_blacklist_patterns:
            if re.search(pattern, canonical_name.lower()):
                blacklist_hit = True
                base_score -= self.config.safety_blacklist_penalty
                logger.warning("Safety blacklist hit for %s (pattern: %s)",
                             canonical_name, pattern)
                break

        # Penalty for harm evidence (major)
        if harm > 0:
            # Harm penalty scales with number of harm papers
            harm_penalty = min(8.0, harm * 2.0)  # Up to 8 points deducted
            base_score -= harm_penalty

        # Bonus if benefit >> harm (strong safety profile)
        if benefit > 0 and harm == 0:
            # No harm papers is good sign
            safety_bonus = min(2.0, benefit / 5.0)
            base_score += safety_bonus
        elif benefit > harm * 3:
            # Benefit outweighs harm 3:1
            safety_bonus = 1.0
            base_score += safety_bonus

        final_score = max(0.0, min(20.0, base_score))

        return final_score

    def _score_practicality(self, dossier: Dict[str, Any]) -> float:
        """Score practicality of implementation (0-10 points)

        Simple heuristic based on evidence availability.
        In full version, would consider:
        - Drug availability
        - Route of administration
        - Cost
        - Regulatory status

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-10
        """
        total_pmids = dossier.get("total_pmids", 0)

        # More research = more likely to be available/practical
        if total_pmids >= 50:
            return 8.0
        elif total_pmids >= 20:
            return 6.0
        elif total_pmids >= 10:
            return 4.0
        elif total_pmids >= 5:
            return 2.0
        else:
            return 1.0
