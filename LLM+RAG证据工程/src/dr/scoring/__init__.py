"""Phase 4: Scoring Layer

Transforms evidence from Step6 dossiers into:
- Drug scores (evidence strength, mechanism, translatability, safety, practicality)
- Gating decisions (GO/MAYBE/NO-GO)
- Hypothesis cards (structured summaries)
- Validation plans (next steps for promising drugs)

Modules:
- scorer: DrugScorer class for calculating multi-dimensional scores
- gating: GatingEngine for GO/NO-GO decisions
- cards: HypothesisCardBuilder for generating hypothesis cards
- validation: ValidationPlanner for creating validation plans
"""

from .scorer import DrugScorer, ScoringConfig
from .gating import GatingEngine, GatingConfig, GatingDecision, GateDecision
from .cards import HypothesisCardBuilder, HypothesisCard
from .validation import ValidationPlanner, ValidationPlan

__all__ = [
    "DrugScorer",
    "ScoringConfig",
    "GatingEngine",
    "GatingConfig",
    "GatingDecision",
    "GateDecision",
    "HypothesisCardBuilder",
    "HypothesisCard",
    "ValidationPlanner",
    "ValidationPlan",
]
