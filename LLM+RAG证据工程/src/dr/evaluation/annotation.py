"""Inter-Annotator Agreement (IAA) computation for gold-standard evaluation.

Provides Cohen's Kappa, raw agreement, and confusion matrices
for dual-annotated gold-standard records. Used to quantify
annotation reliability and detect systematic biases.

Industrial-grade features:
- Cohen's Kappa with chance-correction
- Per-field IAA (direction, model, endpoint)
- Confusion matrix generation
- Support for v2 dual-annotation CSV format
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from ..logger import get_logger

logger = get_logger(__name__)


@dataclass
class AnnotationPair:
    """A single item annotated by two annotators.

    Attributes:
        record_id: Unique identifier (typically pmid + drug_name)
        annotator_a: First annotator name
        annotator_b: Second annotator name
        label_a: First annotator's label
        label_b: Second annotator's label
        field: Which field this pair measures (e.g., "direction", "model")
    """
    record_id: str
    annotator_a: str
    annotator_b: str
    label_a: str
    label_b: str
    field: str


@dataclass
class IAAReport:
    """Inter-Annotator Agreement report for one field.

    Attributes:
        field: Field name (e.g., "direction")
        n_pairs: Number of annotation pairs
        raw_agreement: Proportion of exact matches (0-1)
        cohens_kappa: Chance-corrected agreement (-1 to 1)
        confusion_matrix: {label_a: {label_b: count}}
        per_class_agreement: {label: agreement_rate}
    """
    field: str
    n_pairs: int
    raw_agreement: float
    cohens_kappa: float
    confusion_matrix: Dict[str, Dict[str, int]]
    per_class_agreement: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable summary."""
        return (
            f"IAA({self.field}): n={self.n_pairs}, "
            f"agreement={self.raw_agreement:.3f}, "
            f"kappa={self.cohens_kappa:.3f}"
        )

    def to_dict(self) -> Dict:
        """JSON-serializable dict."""
        return asdict(self)


def compute_cohens_kappa(
    labels_a: List[str],
    labels_b: List[str],
) -> float:
    """Compute Cohen's Kappa between two annotators.

    Formula:
        kappa = (p_o - p_e) / (1 - p_e)
    where p_o is observed agreement and p_e is expected agreement by chance.

    Args:
        labels_a: Labels from annotator A
        labels_b: Labels from annotator B

    Returns:
        Kappa in [-1, 1]. Returns 0.0 if undefined (all same label).

    Raises:
        ValueError: If label lists have different lengths or are empty.
    """
    if len(labels_a) != len(labels_b):
        raise ValueError(
            f"Label lists must have same length: {len(labels_a)} vs {len(labels_b)}"
        )
    if len(labels_a) == 0:
        raise ValueError("Label lists must not be empty")

    n = len(labels_a)

    # Observed agreement
    p_o = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n

    # Count label frequencies for each annotator
    all_labels = sorted(set(labels_a) | set(labels_b))
    freq_a: Dict[str, int] = defaultdict(int)
    freq_b: Dict[str, int] = defaultdict(int)
    for a, b in zip(labels_a, labels_b):
        freq_a[a] += 1
        freq_b[b] += 1

    # Expected agreement by chance
    p_e = sum((freq_a[label] / n) * (freq_b[label] / n) for label in all_labels)

    # Handle edge case: perfect chance agreement (all same label)
    if abs(1.0 - p_e) < 1e-10:
        # If p_o == p_e == 1.0, kappa is undefined; return 1.0 by convention
        return 1.0 if abs(p_o - 1.0) < 1e-10 else 0.0

    return (p_o - p_e) / (1.0 - p_e)


def _build_confusion_matrix(
    labels_a: List[str],
    labels_b: List[str],
) -> Dict[str, Dict[str, int]]:
    """Build confusion matrix from two label lists.

    Returns:
        {label_a: {label_b: count}} nested dict
    """
    all_labels = sorted(set(labels_a) | set(labels_b))
    matrix: Dict[str, Dict[str, int]] = {
        la: {lb: 0 for lb in all_labels} for la in all_labels
    }
    for a, b in zip(labels_a, labels_b):
        matrix[a][b] += 1
    return matrix


def _per_class_agreement(
    labels_a: List[str],
    labels_b: List[str],
) -> Dict[str, float]:
    """Compute per-class agreement rates.

    For each class label, compute what fraction of items annotated
    as that class by annotator A were also labeled the same by B.

    Returns:
        {label: agreement_rate}
    """
    class_total: Dict[str, int] = defaultdict(int)
    class_agree: Dict[str, int] = defaultdict(int)

    for a, b in zip(labels_a, labels_b):
        class_total[a] += 1
        if a == b:
            class_agree[a] += 1

    # Also count annotator B's labels to get complete picture
    for b in labels_b:
        if b not in class_total:
            class_total[b] += 0  # ensure key exists

    return {
        label: (class_agree.get(label, 0) / total if total > 0 else 0.0)
        for label, total in class_total.items()
    }


def compute_iaa(
    pairs: List[AnnotationPair],
) -> Dict[str, IAAReport]:
    """Compute IAA for each field represented in the annotation pairs.

    Groups pairs by field, then computes kappa, agreement, and
    confusion matrix for each field.

    Args:
        pairs: List of AnnotationPair objects

    Returns:
        {field_name: IAAReport}
    """
    if not pairs:
        logger.warning("No annotation pairs provided")
        return {}

    # Group by field
    by_field: Dict[str, List[AnnotationPair]] = defaultdict(list)
    for pair in pairs:
        by_field[pair.field].append(pair)

    reports: Dict[str, IAAReport] = {}
    for field_name, field_pairs in sorted(by_field.items()):
        labels_a = [p.label_a for p in field_pairs]
        labels_b = [p.label_b for p in field_pairs]

        kappa = compute_cohens_kappa(labels_a, labels_b)
        n = len(field_pairs)
        raw_agree = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
        confusion = _build_confusion_matrix(labels_a, labels_b)
        per_class = _per_class_agreement(labels_a, labels_b)

        report = IAAReport(
            field=field_name,
            n_pairs=n,
            raw_agreement=round(raw_agree, 4),
            cohens_kappa=round(kappa, 4),
            confusion_matrix=confusion,
            per_class_agreement={k: round(v, 4) for k, v in per_class.items()},
        )
        reports[field_name] = report
        logger.info(report.summary())

    return reports


def load_dual_annotations(
    path: str,
    fields: Optional[List[str]] = None,
) -> List[AnnotationPair]:
    """Load dual annotations from a gold_standard_v2 CSV.

    The v2 CSV has primary annotator columns (direction, model, endpoint)
    and secondary annotator columns (direction_b, model_b, endpoint_b).
    Only rows where both annotators have values are included.

    Args:
        path: Path to gold_standard_v2.csv
        fields: Which fields to extract pairs for. Default: ["direction", "model", "endpoint"]

    Returns:
        List of AnnotationPair objects
    """
    import csv
    from pathlib import Path as P

    if fields is None:
        fields = ["direction", "model", "endpoint"]

    p = P(path)
    if not p.exists():
        raise FileNotFoundError(f"Annotation file not found: {path}")

    pairs: List[AnnotationPair] = []

    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Empty CSV: {path}")

        for i, row in enumerate(reader, 1):
            pmid = (row.get("pmid") or "").strip()
            drug = (row.get("drug_name") or "").strip()
            annotator_a = (row.get("annotator") or "").strip()
            annotator_b = (row.get("annotator_b") or "").strip()

            if not pmid or not drug:
                continue

            record_id = f"{pmid}_{drug}"

            for fld in fields:
                label_a = (row.get(fld) or "").strip().lower()
                label_b = (row.get(f"{fld}_b") or "").strip().lower()

                # Only include if both annotators provided a value
                if label_a and label_b:
                    pairs.append(AnnotationPair(
                        record_id=record_id,
                        annotator_a=annotator_a or "annotator_a",
                        annotator_b=annotator_b or "annotator_b",
                        label_a=label_a,
                        label_b=label_b,
                        field=fld,
                    ))

    logger.info("Loaded %d annotation pairs from %s", len(pairs), path)
    return pairs
