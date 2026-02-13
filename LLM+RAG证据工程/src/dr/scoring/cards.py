"""Hypothesis Card Builder - Generate structured drug summaries

Creates hypothesis cards that summarize:
- Evidence strength and quality
- Mechanism of action
- Safety profile
- Clinical translatability
- Key supporting PMIDs

Format: Both JSON (structured) and Markdown (human-readable)
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from pathlib import Path

from ..logger import get_logger

logger = get_logger(__name__)


@dataclass
class HypothesisCard:
    """Structured hypothesis card for a drug candidate

    Attributes:
        drug_id: Drug identifier
        canonical_name: Canonical drug name
        gate_decision: GO/MAYBE/NO-GO
        gate_reasons: Reasons for gating decision
        scores: Multi-dimensional scores
        evidence_summary: Summary of evidence
        key_pmids: Top supporting PMIDs
        mechanism_hypothesis: Hypothesized mechanism
        next_steps: Recommended next steps
    """
    drug_id: str
    canonical_name: str
    gate_decision: str
    gate_reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    key_pmids: List[str] = field(default_factory=list)
    mechanism_hypothesis: str = ""
    next_steps: List[str] = field(default_factory=list)
    dossier_path: str = ""
    decision_channel: str = "exploit"
    novelty_score: float = 0.0
    uncertainty_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "drug_id": self.drug_id,
            "canonical_name": self.canonical_name,
            "gate_decision": self.gate_decision,
            "gate_reasons": self.gate_reasons,
            "scores": self.scores,
            "evidence_summary": self.evidence_summary,
            "key_pmids": self.key_pmids,
            "mechanism_hypothesis": self.mechanism_hypothesis,
            "next_steps": self.next_steps,
            "dossier_path": self.dossier_path,
            "decision_channel": self.decision_channel,
            "novelty_score": round(float(self.novelty_score), 4),
            "uncertainty_score": round(float(self.uncertainty_score), 4),
        }

    def to_markdown(self) -> str:
        """Generate markdown representation of card

        Returns:
            Formatted markdown string
        """
        lines = []

        # Header
        lines.append(f"# {self.canonical_name.title()}")
        lines.append("")
        lines.append(f"**Drug ID**: {self.drug_id}")
        lines.append(f"**Decision**: {self.gate_decision}")
        lines.append(
            f"**Track**: {self.decision_channel} | "
            f"Novelty={self.novelty_score:.2f} | Uncertainty={self.uncertainty_score:.2f}"
        )
        if self.gate_reasons:
            lines.append(f"**Reasons**: {'; '.join(self.gate_reasons)}")
        lines.append("")

        # Scores
        lines.append("## Scores")
        lines.append("")
        total = self.scores.get("total_score_0_100", 0)
        lines.append(f"**Total Score**: {total:.1f}/100")
        lines.append("")
        lines.append("| Dimension | Score | Max |")
        lines.append("|-----------|-------|-----|")
        lines.append(f"| Evidence Strength | {self.scores.get('evidence_strength_0_30', 0):.1f} | 30 |")
        lines.append(f"| Mechanism Plausibility | {self.scores.get('mechanism_plausibility_0_20', 0):.1f} | 20 |")
        lines.append(f"| Translatability | {self.scores.get('translatability_0_20', 0):.1f} | 20 |")
        lines.append(f"| Safety Fit | {self.scores.get('safety_fit_0_20', 0):.1f} | 20 |")
        lines.append(f"| Practicality | {self.scores.get('practicality_0_10', 0):.1f} | 10 |")
        lines.append("")

        # Evidence Summary
        lines.append("## Evidence Summary")
        lines.append("")
        summary = self.evidence_summary
        benefit = summary.get("benefit", 0)
        harm = summary.get("harm", 0)
        neutral = summary.get("neutral", 0)
        unknown = summary.get("unknown", 0)
        total_pmids = summary.get("total_pmids", 0)

        lines.append(f"- **Total PMIDs**: {total_pmids}")
        lines.append(f"- **Benefit Papers**: {benefit}")
        lines.append(f"- **Harm Papers**: {harm}")
        lines.append(f"- **Neutral Papers**: {neutral}")
        lines.append(f"- **Unknown/Unclear**: {unknown}")
        lines.append("")

        # Mechanism
        if self.mechanism_hypothesis:
            lines.append("## Hypothesized Mechanism")
            lines.append("")
            lines.append(self.mechanism_hypothesis)
            lines.append("")

        # Key Evidence
        if self.key_pmids:
            lines.append("## Key Supporting Evidence")
            lines.append("")
            for i, pmid in enumerate(self.key_pmids[:5], 1):
                lines.append(f"{i}. PMID:{pmid} - https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
            lines.append("")

        # Next Steps
        if self.next_steps:
            lines.append("## Recommended Next Steps")
            lines.append("")
            for step in self.next_steps:
                lines.append(f"- {step}")
            lines.append("")

        # Dossier Link
        if self.dossier_path:
            lines.append("## Detailed Dossier")
            lines.append("")
            lines.append(f"Full evidence dossier: `{self.dossier_path}`")
            lines.append("")

        return "\n".join(lines)


class HypothesisCardBuilder:
    """Builds hypothesis cards from dossiers, scores, and gating decisions

    Example:
        >>> builder = HypothesisCardBuilder()
        >>> card = builder.build_card(dossier, scores, gating_decision)
        >>> print(card.to_markdown())
    """

    def __init__(self):
        """Initialize card builder"""
        logger.info("HypothesisCardBuilder initialized")

    def build_card(
        self,
        dossier: Dict[str, Any],
        scores: Dict[str, float],
        gating_decision: Any,
        dossier_path: Optional[str] = None
    ) -> HypothesisCard:
        """Build hypothesis card for a drug

        Args:
            dossier: Drug dossier from Step6
            scores: Scores from DrugScorer
            gating_decision: GatingDecision object
            dossier_path: Optional path to dossier file

        Returns:
            HypothesisCard object
        """
        drug_id = dossier.get("drug_id", "unknown")
        canonical = dossier.get("canonical_name", "unknown")

        logger.debug("Building card for: %s (%s)", canonical, drug_id)

        # Extract evidence summary
        evidence_count = dossier.get("evidence_count", {})
        unknown_count = evidence_count.get("unknown", 0) + evidence_count.get("unclear", 0)
        evidence_summary = {
            "benefit": evidence_count.get("benefit", 0),
            "harm": evidence_count.get("harm", 0),
            "neutral": evidence_count.get("neutral", 0),
            "unknown": unknown_count,
            "total_pmids": dossier.get("total_pmids", 0)
        }

        # Extract key PMIDs (benefit papers)
        key_pmids = self._extract_key_pmids(dossier)

        # Generate mechanism hypothesis
        mechanism = self._generate_mechanism_hypothesis(dossier, canonical)

        # Generate next steps
        next_steps = self._generate_next_steps(
            gating_decision.decision.value,
            getattr(gating_decision, "decision_channel", "exploit"),
            evidence_summary,
            scores
        )

        card = HypothesisCard(
            drug_id=drug_id,
            canonical_name=canonical,
            gate_decision=gating_decision.decision.value,
            gate_reasons=gating_decision.gate_reasons,
            scores=scores,
            evidence_summary=evidence_summary,
            key_pmids=key_pmids,
            mechanism_hypothesis=mechanism,
            next_steps=next_steps,
            dossier_path=dossier_path or "",
            decision_channel=getattr(gating_decision, "decision_channel", "exploit"),
            novelty_score=float(getattr(gating_decision, "novelty_score", 0.0) or 0.0),
            uncertainty_score=float(getattr(gating_decision, "uncertainty_score", 0.0) or 0.0),
        )

        return card

    def _extract_key_pmids(self, dossier: Dict[str, Any]) -> List[str]:
        """Extract top benefit PMIDs as key evidence

        Args:
            dossier: Drug dossier

        Returns:
            List of PMIDs (max 10)
        """
        evidence_blocks = dossier.get("evidence_blocks", [])

        # Filter benefit papers
        benefit_blocks = [
            block for block in evidence_blocks
            if block.get("direction") == "benefit"
        ]

        # Take top 10 benefit PMIDs
        key_pmids = [block.get("pmid") for block in benefit_blocks[:10]]

        return [pmid for pmid in key_pmids if pmid]

    def _generate_mechanism_hypothesis(
        self,
        dossier: Dict[str, Any],
        canonical_name: str
    ) -> str:
        """Generate hypothesized mechanism of action

        Args:
            dossier: Drug dossier
            canonical_name: Drug name

        Returns:
            Mechanism hypothesis string
        """
        evidence_count = dossier.get("evidence_count", {})
        benefit = evidence_count.get("benefit", 0)
        target_disease = self._target_disease(dossier)

        if benefit == 0:
            return (
                f"Limited evidence available for {canonical_name} in {target_disease}. "
                "Mechanism unclear."
            )

        # Look at top benefit paper titles for mechanism hints
        evidence_blocks = dossier.get("evidence_blocks", [])
        benefit_titles = [
            block.get("title", "").lower()
            for block in evidence_blocks
            if block.get("direction") == "benefit"
        ][:5]

        # Simple keyword-based mechanism inference
        mechanisms = []

        title_text = " ".join(benefit_titles)

        if "inflammation" in title_text or "inflammatory" in title_text:
            mechanisms.append("anti-inflammatory effects")
        if "oxidant" in title_text or "oxidation" in title_text or "antioxidant" in title_text:
            mechanisms.append("antioxidant activity")
        if "lipid" in title_text or "cholesterol" in title_text or "ldl" in title_text:
            mechanisms.append("lipid modulation")
        if "endothel" in title_text:
            mechanisms.append("endothelial protection")
        if "macrophage" in title_text or "foam cell" in title_text:
            mechanisms.append("macrophage modulation")
        if "plaque" in title_text and ("stabil" in title_text or "regress" in title_text):
            mechanisms.append("plaque stabilization/regression")

        if mechanisms:
            mech_str = ", ".join(mechanisms)
            return (
                f"{canonical_name.title()} may reduce {target_disease} through {mech_str}. "
                f"Based on {benefit} supporting publications, the drug shows potential benefit "
                "in preclinical and/or clinical studies."
            )
        return (
            f"{canonical_name.title()} shows evidence of benefit in {benefit} publications "
            f"for {target_disease}. Specific mechanism requires further investigation of "
            "literature."
        )

    def _target_disease(self, dossier: Dict[str, Any]) -> str:
        """Resolve target disease label from dossier metadata."""
        for key in ("target_disease", "disease", "disease_name"):
            value = dossier.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        metadata = dossier.get("metadata", {})
        if isinstance(metadata, dict):
            for key in ("target_disease", "disease", "disease_name"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "atherosclerosis"

    def _generate_next_steps(
        self,
        gate_decision: str,
        decision_channel: str,
        evidence_summary: Dict[str, Any],
        scores: Dict[str, float]
    ) -> List[str]:
        """Generate recommended next steps based on decision

        Args:
            gate_decision: GO/MAYBE/NO-GO
            evidence_summary: Evidence metrics
            scores: Score dict

        Returns:
            List of recommended actions
        """
        steps = []

        if gate_decision == "GO":
            steps.append("✅ Proceed to detailed validation planning")
            steps.append("Review full evidence dossier and identify key mechanistic papers")
            steps.append("Design preclinical validation experiments (if mechanism unclear)")
            steps.append("Evaluate clinical trial feasibility and endpoints")
            steps.append("Assess drug availability and regulatory pathway")

        elif gate_decision == "MAYBE":
            if decision_channel == "explore":
                steps.append("🔎 Explore-track candidate - preserve for discovery, not immediate rejection")
                steps.append("Run orthogonal mechanism screens (target engagement + pathway readout)")
                steps.append("Perform cross-disease plausibility review on top route-derived PMIDs")
                steps.append("Promote to exploit only after at least one reproducible positive assay")
            else:
                steps.append("⚠️ Borderline candidate - requires additional evidence review")
                steps.append("Manual review of top 20 papers to confirm benefit/harm classification")
                steps.append("Search for additional evidence (expanded PubMed queries)")

            # Specific recommendations based on weak dimensions
            safety_score = scores.get("safety_fit_0_20", 20)
            evidence_score = scores.get("evidence_strength_0_30", 0)

            if safety_score < 15:
                steps.append("⚠️ Conduct detailed safety review before proceeding")
            if evidence_score < 15:
                steps.append("Gather more evidence - current coverage may be insufficient")

            steps.append("Re-score after evidence review to determine GO/NO-GO")

        else:  # NO-GO
            steps.append("❌ Do not advance to validation at this time")

            benefit = evidence_summary.get("benefit", 0)
            harm = evidence_summary.get("harm", 0)
            total_pmids = evidence_summary.get("total_pmids", 0)

            if benefit < 2:
                steps.append("Insufficient benefit evidence - consider removing from pipeline")
            if harm > benefit:
                steps.append("⚠️ Harm evidence outweighs benefit - significant safety concerns")
            if total_pmids < 5:
                steps.append("Limited literature - may indicate insufficient research interest")

            steps.append("Archive dossier for future reference")

        return steps

    def build_batch(
        self,
        dossiers: List[Dict[str, Any]],
        scores_list: List[Dict[str, float]],
        gating_decisions: List[Any],
        dossier_paths: Optional[List[str]] = None
    ) -> List[HypothesisCard]:
        """Build cards for multiple drugs

        Args:
            dossiers: List of drug dossiers
            scores_list: List of score dicts
            gating_decisions: List of GatingDecision objects
            dossier_paths: Optional list of dossier file paths

        Returns:
            List of HypothesisCards
        """
        if dossier_paths is None:
            dossier_paths = [""] * len(dossiers)

        if not (len(dossiers) == len(scores_list) == len(gating_decisions) == len(dossier_paths)):
            raise ValueError("All input lists must have same length")

        cards = []
        for dossier, scores, decision, path in zip(dossiers, scores_list, gating_decisions, dossier_paths):
            card = self.build_card(dossier, scores, decision, path)
            cards.append(card)

        logger.info("Built %d hypothesis cards", len(cards))

        return cards

    def save_cards_json(self, cards: List[HypothesisCard], output_path: str | Path) -> None:
        """Save cards as JSON

        Args:
            cards: List of HypothesisCards
            output_path: Output file path
        """
        import json

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cards_data = [card.to_dict() for card in cards]

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(cards_data, f, indent=2, ensure_ascii=False)

        logger.info("Saved %d cards to: %s", len(cards), output_path)

    def save_cards_markdown(self, cards: List[HypothesisCard], output_path: str | Path) -> None:
        """Save cards as markdown document

        Args:
            cards: List of HypothesisCards
            output_path: Output file path
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# LLM+RAG证据工程 Hypothesis Cards",
            "",
            f"**Generated**: {self._get_timestamp()}",
            f"**Total Candidates**: {len(cards)}",
            ""
        ]

        # Summary table
        go_cards = [c for c in cards if c.gate_decision == "GO"]
        maybe_cards = [c for c in cards if c.gate_decision == "MAYBE"]
        no_go_cards = [c for c in cards if c.gate_decision == "NO-GO"]

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- ✅ **GO**: {len(go_cards)} drugs")
        lines.append(f"- ⚠️ **MAYBE**: {len(maybe_cards)} drugs")
        lines.append(f"- ❌ **NO-GO**: {len(no_go_cards)} drugs")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Individual cards (sorted by decision and score)
        sorted_cards = sorted(
            cards,
            key=lambda c: (
                {"GO": 0, "MAYBE": 1, "NO-GO": 2}.get(c.gate_decision, 3),
                -c.scores.get("total_score_0_100", 0)
            )
        )

        for card in sorted_cards:
            lines.append(card.to_markdown())
            lines.append("---")
            lines.append("")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))

        logger.info("Saved %d cards to: %s", len(cards), output_path)

    def _get_timestamp(self) -> str:
        """Get current timestamp string"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
