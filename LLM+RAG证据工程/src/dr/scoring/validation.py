"""Validation Planner - Generate validation plans for promising drugs

Creates structured validation plans for GO/MAYBE drugs, including:
- Recommended validation experiments
- Clinical trial design considerations
- Resource requirements
- Timeline estimates
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from pathlib import Path
import pandas as pd

from ..logger import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationPlan:
    """Structured validation plan for a drug candidate

    Attributes:
        drug_id: Drug identifier
        canonical_name: Canonical drug name
        gate_decision: GO/MAYBE/NO-GO
        priority: Priority tier (1=high, 2=medium, 3=low)
        validation_stage: Recommended starting stage
        experiments: List of recommended experiments
        trial_design: Clinical trial design notes
        resources: Resource requirements
        timeline_weeks: Estimated timeline in weeks
        cost_estimate_usd: Rough cost estimate
    """
    drug_id: str
    canonical_name: str
    gate_decision: str
    priority: int = 3
    validation_stage: str = "LITERATURE_REVIEW"
    experiments: List[str] = field(default_factory=list)
    trial_design: Dict[str, Any] = field(default_factory=dict)
    resources: Dict[str, Any] = field(default_factory=dict)
    timeline_weeks: int = 0
    cost_estimate_usd: str = "TBD"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "drug_id": self.drug_id,
            "canonical_name": self.canonical_name,
            "gate_decision": self.gate_decision,
            "priority": self.priority,
            "validation_stage": self.validation_stage,
            "experiments": self.experiments,
            "trial_design": self.trial_design,
            "resources": self.resources,
            "timeline_weeks": self.timeline_weeks,
            "cost_estimate_usd": self.cost_estimate_usd,
            "notes": self.notes
        }


class ValidationPlanner:
    """Generates validation plans for drug candidates

    Example:
        >>> planner = ValidationPlanner()
        >>> plan = planner.create_plan(card, dossier)
        >>> print(plan.validation_stage)
        PRECLINICAL_VALIDATION
    """

    def __init__(self):
        """Initialize validation planner"""
        logger.info("ValidationPlanner initialized")

    def create_plan(
        self,
        card: Any,  # HypothesisCard
        dossier: Dict[str, Any]
    ) -> ValidationPlan:
        """Create validation plan for a drug

        Args:
            card: HypothesisCard object
            dossier: Drug dossier

        Returns:
            ValidationPlan object
        """
        drug_id = card.drug_id
        canonical = card.canonical_name
        decision = card.gate_decision

        logger.debug("Creating validation plan for: %s (%s)", canonical, drug_id)

        # Determine priority based on score and decision
        priority = self._calculate_priority(card)

        # Determine validation stage
        stage = self._determine_validation_stage(card, dossier)

        # Generate experiments
        experiments = self._generate_experiments(card, dossier, stage)

        # Generate trial design
        trial_design = self._generate_trial_design(card, dossier)

        # Estimate resources
        resources = self._estimate_resources(stage, experiments)

        # Estimate timeline
        timeline = self._estimate_timeline(stage, experiments)

        # Generate notes
        notes = self._generate_notes(card, dossier, stage)

        plan = ValidationPlan(
            drug_id=drug_id,
            canonical_name=canonical,
            gate_decision=decision,
            priority=priority,
            validation_stage=stage,
            experiments=experiments,
            trial_design=trial_design,
            resources=resources,
            timeline_weeks=timeline,
            notes=notes
        )

        return plan

    def _calculate_priority(self, card: Any) -> int:
        """Calculate priority tier (1=high, 2=medium, 3=low)

        Args:
            card: HypothesisCard

        Returns:
            Priority tier (1-3)
        """
        total_score = card.scores.get("total_score_0_100", 0)
        decision = card.gate_decision

        if decision == "NO-GO":
            return 3

        if decision == "GO":
            if total_score >= 75:
                return 1  # High priority
            else:
                return 2  # Medium priority

        # MAYBE
        return 3  # Low priority

    def _determine_validation_stage(self, card: Any, dossier: Dict[str, Any]) -> str:
        """Determine appropriate validation stage

        Stages:
        - LITERATURE_REVIEW: Needs more evidence gathering
        - MECHANISM_VALIDATION: Test mechanism in vitro/in vivo
        - PRECLINICAL_VALIDATION: Animal model testing
        - CLINICAL_TRIAL_DESIGN: Ready for human trials
        - EXISTING_TRIAL_ANALYSIS: Analyze existing clinical data

        Args:
            card: HypothesisCard
            dossier: Drug dossier

        Returns:
            Validation stage string
        """
        benefit = card.evidence_summary.get("benefit", 0)
        total_pmids = card.evidence_summary.get("total_pmids", 0)
        mechanism_score = card.scores.get("mechanism_plausibility_0_20", 0)
        total_score = card.scores.get("total_score_0_100", 0)

        # Check if there's already clinical trial evidence in titles
        evidence_blocks = dossier.get("evidence_blocks", [])
        titles = [block.get("title", "").lower() for block in evidence_blocks]
        has_trial_evidence = any(
            "trial" in title or "clinical" in title or "patient" in title
            for title in titles
        )

        if card.gate_decision == "NO-GO":
            return "LITERATURE_REVIEW"

        if total_pmids < 5:
            return "LITERATURE_REVIEW"

        if has_trial_evidence and total_score >= 70:
            return "EXISTING_TRIAL_ANALYSIS"

        if benefit >= 10 and mechanism_score >= 15:
            return "CLINICAL_TRIAL_DESIGN"

        if benefit >= 5 and mechanism_score >= 12:
            return "PRECLINICAL_VALIDATION"

        if mechanism_score < 12:
            return "MECHANISM_VALIDATION"

        return "LITERATURE_REVIEW"

    def _generate_experiments(
        self,
        card: Any,
        dossier: Dict[str, Any],
        stage: str
    ) -> List[str]:
        """Generate recommended experiments based on stage

        Args:
            card: HypothesisCard
            dossier: Drug dossier
            stage: Validation stage

        Returns:
            List of experiment descriptions
        """
        experiments = []

        if stage == "LITERATURE_REVIEW":
            experiments.append("Comprehensive literature review (expand beyond PubMed)")
            experiments.append("Review clinical trial registries (ClinicalTrials.gov, EudraCT)")
            experiments.append("Assess drug availability and existing formulations")

        elif stage == "MECHANISM_VALIDATION":
            experiments.append("In vitro assays: endothelial cell dysfunction models")
            experiments.append("In vitro assays: macrophage foam cell formation")
            experiments.append("In vitro assays: LDL oxidation and uptake")
            experiments.append("Pathway analysis: confirm hypothesized mechanism")

        elif stage == "PRECLINICAL_VALIDATION":
            experiments.append("Mouse model: ApoE-/- mice on high-fat diet")
            experiments.append("Endpoint: Plaque area quantification (aortic root)")
            experiments.append("Endpoint: Plasma lipid profile (TC, LDL, HDL, TG)")
            experiments.append("Endpoint: Inflammatory markers (IL-6, TNF-α, CRP)")
            experiments.append("Safety assessment: Body weight, liver enzymes, complete blood count")

        elif stage == "CLINICAL_TRIAL_DESIGN":
            experiments.append("Phase 2 proof-of-concept trial design")
            experiments.append("Endpoint selection: CIMT, coronary CTA, or biomarkers")
            experiments.append("Power calculation and sample size estimation")
            experiments.append("Identify suitable patient population (high CV risk)")

        elif stage == "EXISTING_TRIAL_ANALYSIS":
            experiments.append("Systematic review of published trial data")
            experiments.append("Meta-analysis if multiple trials available")
            experiments.append("Request raw data from trial investigators (if possible)")
            experiments.append("Subgroup analysis for atherosclerosis-related outcomes")

        return experiments

    def _generate_trial_design(
        self,
        card: Any,
        dossier: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate clinical trial design considerations

        Args:
            card: HypothesisCard
            dossier: Drug dossier

        Returns:
            Trial design dictionary
        """
        design = {
            "phase": "Phase 2",
            "design_type": "Randomized, double-blind, placebo-controlled",
            "primary_endpoint": "Change in carotid intima-media thickness (CIMT) at 12 months",
            "secondary_endpoints": [
                "Change in coronary plaque volume (CTA)",
                "Change in LDL cholesterol",
                "Major adverse cardiovascular events (MACE)"
            ],
            "duration_months": 12,
            "estimated_n": 200,
            "inclusion_criteria": [
                "Adults 40-75 years",
                "Elevated CV risk (ASCVD risk score ≥7.5%)",
                "Baseline CIMT ≥0.7 mm",
                "LDL ≥70 mg/dL despite statin therapy"
            ],
            "exclusion_criteria": [
                "Recent MI or stroke (<3 months)",
                "Severe renal or hepatic impairment",
                "Active cancer",
                "Pregnancy or breastfeeding"
            ]
        }

        # Adjust based on safety score
        safety_score = card.scores.get("safety_fit_0_20", 20)
        if safety_score < 15:
            design["notes"] = "⚠️ Enhanced safety monitoring required due to safety concerns"
            design["secondary_endpoints"].append("Comprehensive safety panel (weekly for first month)")

        return design

    def _estimate_resources(
        self,
        stage: str,
        experiments: List[str]
    ) -> Dict[str, Any]:
        """Estimate resource requirements

        Args:
            stage: Validation stage
            experiments: List of experiments

        Returns:
            Resources dictionary
        """
        resources = {}

        if stage == "LITERATURE_REVIEW":
            resources = {
                "personnel": ["1 Research Analyst"],
                "equipment": ["None"],
                "materials": ["Database subscriptions"],
                "facilities": ["None"]
            }

        elif stage == "MECHANISM_VALIDATION":
            resources = {
                "personnel": ["1 Postdoc or Senior RA", "1 Research Assistant"],
                "equipment": ["Cell culture facility", "Flow cytometer", "Plate reader"],
                "materials": ["Cell lines", "Drug compound", "Assay kits"],
                "facilities": ["BSL-2 cell culture lab"]
            }

        elif stage == "PRECLINICAL_VALIDATION":
            resources = {
                "personnel": ["1 Postdoc", "2 Research Assistants", "1 Veterinary Technician"],
                "equipment": ["Mouse colony", "Histology equipment", "Clinical chemistry analyzer"],
                "materials": ["ApoE-/- mice (n=40)", "High-fat diet", "Drug formulation", "Histology supplies"],
                "facilities": ["AAALAC-accredited animal facility", "Histology core"]
            }

        elif stage in ["CLINICAL_TRIAL_DESIGN", "EXISTING_TRIAL_ANALYSIS"]:
            resources = {
                "personnel": ["1 Biostatistician", "1 Clinical Trial Manager", "1 Regulatory Specialist"],
                "equipment": ["CTA scanner (if imaging endpoint)", "CIMT ultrasound"],
                "materials": ["Drug supply (GMP)", "CRFs and eCRF system"],
                "facilities": ["Clinical research site(s)", "Data coordinating center"]
            }

        return resources

    def _estimate_timeline(
        self,
        stage: str,
        experiments: List[str]
    ) -> int:
        """Estimate timeline in weeks

        Args:
            stage: Validation stage
            experiments: List of experiments

        Returns:
            Estimated weeks
        """
        timeline_map = {
            "LITERATURE_REVIEW": 4,
            "MECHANISM_VALIDATION": 12,
            "PRECLINICAL_VALIDATION": 24,
            "CLINICAL_TRIAL_DESIGN": 16,
            "EXISTING_TRIAL_ANALYSIS": 8
        }

        return timeline_map.get(stage, 8)

    def _generate_notes(
        self,
        card: Any,
        dossier: Dict[str, Any],
        stage: str
    ) -> List[str]:
        """Generate additional notes for validation plan

        Args:
            card: HypothesisCard
            dossier: Drug dossier
            stage: Validation stage

        Returns:
            List of note strings
        """
        notes = []

        # Safety notes
        safety_score = card.scores.get("safety_fit_0_20", 20)
        if safety_score < 15:
            notes.append("⚠️ SAFETY: This drug has safety concerns. Detailed safety review required before proceeding.")

        # Evidence notes
        benefit = card.evidence_summary.get("benefit", 0)
        harm = card.evidence_summary.get("harm", 0)

        if harm > 0:
            notes.append(f"⚠️ EVIDENCE: {harm} papers suggest potential harm. Careful benefit/risk assessment needed.")

        if benefit >= 10:
            notes.append(f"✅ EVIDENCE: Strong literature support ({benefit} benefit papers).")

        # Mechanism notes
        if card.mechanism_hypothesis and "unclear" in card.mechanism_hypothesis.lower():
            notes.append("⚠️ MECHANISM: Mechanism of action not well-established. Consider mechanistic studies.")

        return notes

    def create_batch_plans(
        self,
        cards: List[Any],
        dossiers: List[Dict[str, Any]]
    ) -> List[ValidationPlan]:
        """Create validation plans for multiple drugs

        Args:
            cards: List of HypothesisCards
            dossiers: List of drug dossiers (same order)

        Returns:
            List of ValidationPlans
        """
        if len(cards) != len(dossiers):
            raise ValueError("cards and dossiers must have same length")

        plans = []
        for card, dossier in zip(cards, dossiers):
            plan = self.create_plan(card, dossier)
            plans.append(plan)

        logger.info("Created %d validation plans", len(plans))

        return plans

    def save_plans_csv(self, plans: List[ValidationPlan], output_path: str | Path) -> None:
        """Save validation plans as CSV

        Args:
            plans: List of ValidationPlans
            output_path: Output file path
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for plan in plans:
            row = {
                "drug_id": plan.drug_id,
                "canonical_name": plan.canonical_name,
                "gate_decision": plan.gate_decision,
                "priority": plan.priority,
                "validation_stage": plan.validation_stage,
                "timeline_weeks": plan.timeline_weeks,
                "experiments_count": len(plan.experiments),
                "notes_count": len(plan.notes)
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        df.to_csv(output_path, index=False, encoding="utf-8-sig")

        logger.info("Saved %d validation plans to: %s", len(plans), output_path)
