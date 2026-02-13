"""Evaluation metrics for evidence extraction quality assessment

Computes accuracy, precision, recall, F1, and confusion matrices
for comparing extraction results against gold-standard annotations.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

from .gold_standard import GoldStandardRecord
from ..logger import get_logger

logger = get_logger(__name__)


@dataclass
class FieldMetrics:
    """Metrics for a single extraction field (e.g., direction)."""
    accuracy: float = 0.0
    total: int = 0
    correct: int = 0
    class_metrics: Dict[str, Dict[str, float]] = field(default_factory=dict)
    confusion: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accuracy": round(self.accuracy, 4),
            "total": self.total,
            "correct": self.correct,
            "class_metrics": self.class_metrics,
            "confusion_matrix": self.confusion,
        }


@dataclass
class ExtractionMetrics:
    """Complete evaluation metrics for an extraction method."""
    overall_accuracy: float = 0.0
    total_samples: int = 0
    matched_samples: int = 0
    unmatched_predictions: int = 0
    unmatched_gold: int = 0
    field_metrics: Dict[str, FieldMetrics] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_accuracy": round(self.overall_accuracy, 4),
            "total_samples": self.total_samples,
            "matched_samples": self.matched_samples,
            "unmatched_predictions": self.unmatched_predictions,
            "unmatched_gold": self.unmatched_gold,
            "field_metrics": {
                k: v.to_dict() for k, v in self.field_metrics.items()
            },
        }

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"=== Extraction Evaluation ===",
            f"Samples: {self.total_samples} gold, {self.matched_samples} matched",
            f"Overall accuracy: {self.overall_accuracy:.1%}",
            f"Unmatched: {self.unmatched_predictions} predictions, {self.unmatched_gold} gold",
        ]
        for fname, fm in self.field_metrics.items():
            lines.append(f"\n  {fname}: accuracy={fm.accuracy:.1%} ({fm.correct}/{fm.total})")
            for cls, m in sorted(fm.class_metrics.items()):
                p = m.get("precision", 0)
                r = m.get("recall", 0)
                f1 = m.get("f1", 0)
                lines.append(f"    {cls}: P={p:.2f} R={r:.2f} F1={f1:.2f}")
        return "\n".join(lines)


@dataclass
class ComparisonReport:
    """Report comparing two extraction methods."""
    method_a_name: str
    method_b_name: str
    metrics_a: ExtractionMetrics
    metrics_b: ExtractionMetrics
    field_deltas: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"=== Comparison: {self.method_a_name} vs {self.method_b_name} ===",
            f"Overall accuracy: {self.metrics_a.overall_accuracy:.1%} vs {self.metrics_b.overall_accuracy:.1%}",
        ]
        for fname, delta in self.field_deltas.items():
            direction = "+" if delta > 0 else ""
            lines.append(f"  {fname}: {direction}{delta:.1%}")
        return "\n".join(lines)


def _compute_class_metrics(
    confusion: Dict[str, Dict[str, int]]
) -> Dict[str, Dict[str, float]]:
    """Compute per-class precision, recall, F1 from confusion matrix."""
    all_classes = set()
    for pred_cls, golds in confusion.items():
        all_classes.add(pred_cls)
        all_classes.update(golds.keys())

    metrics = {}
    for cls in sorted(all_classes):
        # TP: predicted cls and gold was cls
        tp = confusion.get(cls, {}).get(cls, 0)
        # FP: predicted cls but gold was something else
        fp = sum(
            count for gold_cls, count in confusion.get(cls, {}).items()
            if gold_cls != cls
        )
        # FN: gold was cls but predicted something else
        fn = sum(
            confusion.get(pred_cls, {}).get(cls, 0)
            for pred_cls in all_classes
            if pred_cls != cls
        )

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) > 0 else 0.0)

        metrics[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }

    return metrics


def evaluate_field(
    predictions: List[str],
    golds: List[str],
) -> FieldMetrics:
    """Evaluate a single field (direction, model, etc.) across all samples.

    Args:
        predictions: Predicted values
        golds: Gold-standard values (must be same length as predictions)

    Returns:
        FieldMetrics with accuracy, confusion matrix, per-class P/R/F1
    """
    if len(predictions) != len(golds):
        raise ValueError(
            f"Length mismatch: {len(predictions)} predictions vs {len(golds)} golds"
        )

    total = len(predictions)
    correct = sum(1 for p, g in zip(predictions, golds) if p == g)

    # Build confusion matrix: confusion[predicted][gold] = count
    confusion: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p, g in zip(predictions, golds):
        confusion[p][g] += 1

    # Convert defaultdict to regular dict for serialization
    confusion_dict = {k: dict(v) for k, v in confusion.items()}

    class_metrics = _compute_class_metrics(confusion_dict)

    return FieldMetrics(
        accuracy=correct / total if total > 0 else 0.0,
        total=total,
        correct=correct,
        class_metrics=class_metrics,
        confusion=confusion_dict,
    )


def evaluate_extraction(
    predictions: List[Dict[str, Any]],
    gold: List[GoldStandardRecord],
    fields: Optional[List[str]] = None,
) -> ExtractionMetrics:
    """Evaluate extraction results against gold standard.

    Matches predictions to gold records by (pmid, drug_name) key.

    Args:
        predictions: List of dicts with at least {pmid, drug_name, direction, model, endpoint}
        gold: List of GoldStandardRecord objects
        fields: Which fields to evaluate (default: direction, model, endpoint)

    Returns:
        ExtractionMetrics with overall and per-field metrics
    """
    if fields is None:
        fields = ["direction", "model"]

    # Index gold by (pmid, drug_name)
    gold_index: Dict[Tuple[str, str], GoldStandardRecord] = {}
    for g in gold:
        key = (g.pmid.strip(), g.drug_name.strip().lower())
        gold_index[key] = g

    # Index predictions by (pmid, drug_name)
    pred_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in predictions:
        key = (str(p.get("pmid", "")).strip(), str(p.get("drug_name", "")).strip().lower())
        pred_index[key] = p

    # Find matches
    matched_keys = set(gold_index.keys()) & set(pred_index.keys())
    unmatched_gold = len(gold_index) - len(matched_keys)
    unmatched_pred = len(pred_index) - len(matched_keys)

    if not matched_keys:
        logger.warning("No matching (pmid, drug_name) pairs found")
        return ExtractionMetrics(
            total_samples=len(gold),
            unmatched_gold=unmatched_gold,
            unmatched_predictions=unmatched_pred,
        )

    # Evaluate per field
    field_metrics = {}
    all_correct = 0
    all_total = 0

    for fname in fields:
        preds_list = []
        golds_list = []

        for key in sorted(matched_keys):
            g = gold_index[key]
            p = pred_index[key]

            gold_val = getattr(g, fname, "").lower().strip()
            pred_val = str(p.get(fname, "")).lower().strip()

            preds_list.append(pred_val)
            golds_list.append(gold_val)

        fm = evaluate_field(preds_list, golds_list)
        field_metrics[fname] = fm
        all_correct += fm.correct
        all_total += fm.total

    overall = all_correct / all_total if all_total > 0 else 0.0

    return ExtractionMetrics(
        overall_accuracy=overall,
        total_samples=len(gold),
        matched_samples=len(matched_keys),
        unmatched_predictions=unmatched_pred,
        unmatched_gold=unmatched_gold,
        field_metrics=field_metrics,
    )


def evaluate_by_stratum(
    predictions: List[Dict[str, Any]],
    gold: List[GoldStandardRecord],
    stratum_field: str = "direction",
    eval_fields: Optional[List[str]] = None,
) -> Dict[str, ExtractionMetrics]:
    """Evaluate extraction metrics broken down by a stratum field.

    Groups gold-standard records by the specified field value and
    runs evaluation independently on each group.

    Args:
        predictions: Predicted extractions
        gold: Gold-standard records
        stratum_field: Field to stratify by (e.g., "direction", "model")
        eval_fields: Fields to evaluate within each stratum

    Returns:
        {stratum_value: ExtractionMetrics}
    """
    if eval_fields is None:
        eval_fields = ["direction", "model"]

    # Group gold by stratum value
    strata: Dict[str, List[GoldStandardRecord]] = defaultdict(list)
    for g in gold:
        val = getattr(g, stratum_field, "unknown").lower().strip()
        strata[val].append(g)

    results: Dict[str, ExtractionMetrics] = {}
    for stratum_val, stratum_gold in sorted(strata.items()):
        metrics = evaluate_extraction(predictions, stratum_gold, fields=eval_fields)
        results[stratum_val] = metrics
        logger.info(
            "Stratum %s=%s: n=%d, accuracy=%.3f",
            stratum_field, stratum_val, metrics.matched_samples, metrics.overall_accuracy,
        )

    return results


def evaluate_by_difficulty(
    predictions: List[Dict[str, Any]],
    gold_v2: list,
    eval_fields: Optional[List[str]] = None,
) -> Dict[str, ExtractionMetrics]:
    """Evaluate extraction metrics broken down by difficulty tier.

    Requires V2 gold-standard records with difficulty_tier field.

    Args:
        predictions: Predicted extractions
        gold_v2: List of GoldStandardRecordV2 objects (must have difficulty_tier)
        eval_fields: Fields to evaluate

    Returns:
        {difficulty_tier: ExtractionMetrics}
    """
    if eval_fields is None:
        eval_fields = ["direction", "model"]

    # Group by difficulty
    by_difficulty: Dict[str, list] = defaultdict(list)
    for g in gold_v2:
        tier = getattr(g, "difficulty_tier", "medium")
        # Convert V2 to V1 for evaluate_extraction compatibility
        v1 = g.to_v1() if hasattr(g, "to_v1") else g
        by_difficulty[tier].append(v1)

    results: Dict[str, ExtractionMetrics] = {}
    for tier, tier_gold in sorted(by_difficulty.items()):
        metrics = evaluate_extraction(predictions, tier_gold, fields=eval_fields)
        results[tier] = metrics
        logger.info(
            "Difficulty %s: n=%d, accuracy=%.3f",
            tier, metrics.matched_samples, metrics.overall_accuracy,
        )

    return results


def compare_methods(
    results_a: List[Dict[str, Any]],
    results_b: List[Dict[str, Any]],
    gold: List[GoldStandardRecord],
    method_a_name: str = "method_a",
    method_b_name: str = "method_b",
    fields: Optional[List[str]] = None,
) -> ComparisonReport:
    """Compare two extraction methods against the same gold standard.

    Args:
        results_a: Predictions from method A
        results_b: Predictions from method B
        gold: Gold-standard records
        method_a_name: Name for method A
        method_b_name: Name for method B
        fields: Fields to compare

    Returns:
        ComparisonReport with deltas per field
    """
    metrics_a = evaluate_extraction(results_a, gold, fields=fields)
    metrics_b = evaluate_extraction(results_b, gold, fields=fields)

    deltas = {}
    all_fields = set(metrics_a.field_metrics.keys()) | set(metrics_b.field_metrics.keys())
    for fname in sorted(all_fields):
        acc_a = metrics_a.field_metrics.get(fname, FieldMetrics()).accuracy
        acc_b = metrics_b.field_metrics.get(fname, FieldMetrics()).accuracy
        deltas[fname] = acc_a - acc_b

    return ComparisonReport(
        method_a_name=method_a_name,
        method_b_name=method_b_name,
        metrics_a=metrics_a,
        metrics_b=metrics_b,
        field_deltas=deltas,
    )
