"""Evaluation framework for DR evidence extraction pipeline

Provides:
- Gold-standard management (V1 and V2 with dual-annotation)
- Extraction metrics (accuracy, P/R/F1, confusion matrices)
- Inter-Annotator Agreement (Cohen's Kappa)
- Stratified sampling and coverage analysis
"""

from .annotation import (
    AnnotationPair,
    IAAReport,
    compute_cohens_kappa,
    compute_iaa,
    load_dual_annotations,
)
from .stratified_sampling import (
    stratified_sample,
    compute_stratum_coverage,
    identify_gaps,
    coverage_report,
)

__all__ = [
    "AnnotationPair",
    "IAAReport",
    "compute_cohens_kappa",
    "compute_iaa",
    "load_dual_annotations",
    "stratified_sample",
    "compute_stratum_coverage",
    "identify_gaps",
    "coverage_report",
]
