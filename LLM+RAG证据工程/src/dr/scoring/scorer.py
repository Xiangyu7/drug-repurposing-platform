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
try:
    from ..monitoring import track_drug_scoring
except Exception:  # pragma: no cover - monitoring is optional at runtime
    def track_drug_scoring(scores: Dict[str, float]):
        return None

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

        track_drug_scoring(scores)
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

        v2: Based on mechanistic evidence quality, not just PMID count.
        - Whether proposed mechanisms exist in LLM extraction
        - KG-derived target information (drug has known targets)
        - Benefit/total ratio (consistency of evidence direction)
        - Penalty-free for low-literature drugs (novel candidates)

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

        # --- Component 1: Mechanistic knowledge (0-8 pts) ---
        # Does the dossier contain actual mechanism data from KG or LLM?
        llm_data = dossier.get("llm_structured") or {}
        mechanisms = llm_data.get("proposed_mechanisms") or []
        targets = dossier.get("targets") or dossier.get("target_details") or ""
        has_targets = bool(targets)

        if len(mechanisms) >= 3 and has_targets:
            mechanism_base = 8.0  # Rich mechanistic understanding
        elif len(mechanisms) >= 1 or has_targets:
            mechanism_base = 5.0  # Some mechanistic data
        elif total_all > 0:
            mechanism_base = 3.0  # Literature exists but no explicit mechanism
        else:
            mechanism_base = 1.0  # No evidence at all — minimal, NOT 6.0

        # --- Component 2: Evidence consistency (0-8 pts) ---
        if total_classified > 0:
            benefit_ratio = benefit / total_classified
            consistency_bonus = benefit_ratio * 8.0  # Up to 8 points
        elif benefit > 0:
            consistency_bonus = 4.0  # Some benefit, nothing classified against
        else:
            consistency_bonus = 0.0

        # --- Component 3: Evidence volume (0-4 pts, log-capped) ---
        # Modest credit for having more evidence, but log-capped to avoid
        # dominating the score for well-studied drugs
        import math
        if total_all > 0:
            volume_bonus = min(4.0, math.log1p(total_all) * 1.2)
        else:
            volume_bonus = 0.0

        final_score = min(20.0, mechanism_base + consistency_bonus + volume_bonus)
        return final_score

    def _score_translatability(self, dossier: Dict[str, Any]) -> float:
        """Score translatability to clinical trials (0-20 points)

        v2: Based on REAL translational signals, NOT PubMed count proxy.
        - Existing clinical trial evidence (from dossier CT.gov data)
        - Drug approval status / max clinical phase
        - Evidence of human data (not just animal/in-vitro)
        - Benefit-to-harm consistency ratio

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-20
        """
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        harm = evidence_count.get("harm", 0)
        neutral = evidence_count.get("neutral", 0)

        # --- Component 1: Clinical trial signals (0-8 pts) ---
        # Does this drug have existing trial data for related indications?
        trial_data = dossier.get("trial_data") or dossier.get("neg_trials") or []
        ctgov_hits = dossier.get("ctgov_hits", 0)
        # Bridge data may carry max_phase from KG
        max_phase = float(dossier.get("max_phase", 0) or 0)

        trial_score = 0.0
        if max_phase >= 4:
            trial_score = 8.0   # Approved drug → highest translatability
        elif max_phase >= 3:
            trial_score = 6.0
        elif max_phase >= 2:
            trial_score = 4.0
        elif max_phase >= 1 or ctgov_hits > 0 or len(trial_data) > 0:
            trial_score = 2.0
        else:
            trial_score = 1.0   # Unknown phase — still give minimal credit

        # --- Component 2: Evidence consistency (0-6 pts) ---
        # High benefit/total ratio = more likely to translate
        total_classified = benefit + harm + neutral
        if total_classified > 0:
            benefit_ratio = benefit / total_classified
            consistency_score = benefit_ratio * 6.0
        elif benefit > 0:
            consistency_score = 3.0  # Some benefit, no classified
        else:
            consistency_score = 1.0  # No evidence yet — not zero

        # --- Component 3: Evidence diversity bonus (0-6 pts) ---
        # Multiple independent benefit sources = stronger signal
        # Uses log to avoid penalizing low-literature drugs too heavily
        import math
        if benefit >= 5:
            diversity_score = 6.0
        elif benefit >= 3:
            diversity_score = 4.0
        elif benefit >= 1:
            diversity_score = 2.0
        else:
            diversity_score = 0.0

        final_score = min(20.0, trial_score + consistency_score + diversity_score)
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

        v2: Based on REAL drug properties, NOT PubMed count proxy.
        - Drug approval status (approved drugs are immediately practical)
        - Route of administration (oral > injectable > topical for repurposing)
        - Whether the drug has known targets (actionable mechanism)
        - Availability signals from dossier metadata

        Args:
            dossier: Drug dossier

        Returns:
            Score from 0-10
        """
        # --- Component 1: Approval / availability (0-5 pts) ---
        max_phase = float(dossier.get("max_phase", 0) or 0)
        is_approved = max_phase >= 4 or bool(dossier.get("is_approved", False))

        if is_approved:
            availability_score = 5.0   # Already approved = most practical
        elif max_phase >= 3:
            availability_score = 3.5
        elif max_phase >= 2:
            availability_score = 2.5
        elif max_phase >= 1:
            availability_score = 1.5
        else:
            availability_score = 1.0   # Unknown — minimal credit, not zero

        # --- Component 2: Mechanism clarity (0-3 pts) ---
        # Does the dossier contain target/mechanism info?
        llm_data = dossier.get("llm_structured") or {}
        mechanisms = llm_data.get("proposed_mechanisms") or []
        targets = dossier.get("targets") or dossier.get("target_details") or ""
        has_mechanism = len(mechanisms) > 0 or bool(targets)

        if has_mechanism:
            mechanism_score = 3.0
        else:
            mechanism_score = 1.0  # No mechanism info — still possible

        # --- Component 3: Safety feasibility (0-2 pts) ---
        # A drug with no harm signals is more practical to advance
        evidence_count = dossier.get("evidence_count", {})
        harm = evidence_count.get("harm", 0)
        if harm == 0:
            safety_feasibility = 2.0
        elif harm <= 2:
            safety_feasibility = 1.0
        else:
            safety_feasibility = 0.0

        final_score = min(10.0, availability_score + mechanism_score + safety_feasibility)
        return final_score
