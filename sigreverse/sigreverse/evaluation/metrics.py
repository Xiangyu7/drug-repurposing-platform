"""Ranking evaluation metrics for drug repurposing benchmarks.

Provides standard information retrieval metrics for evaluating
drug ranking quality against known drug-disease associations.

Metrics:
    1. Hit@K: Is there at least one known positive in top-K?
    2. Precision@K: Fraction of known positives in top-K
    3. Reciprocal Rank (MRR): 1/rank of first known positive
    4. Average Precision (MAP): Mean precision at each positive rank
    5. NDCG@K: Normalized discounted cumulative gain
    6. AUROC: Area under ROC curve
    7. AUPRC: Area under precision-recall curve

Usage:
    from sigreverse.evaluation.metrics import evaluate_ranking

    results = evaluate_ranking(
        ranked_drugs=["drugA", "drugB", "drugC", ...],
        known_positives={"drugA", "drugC"},
        ks=[5, 10, 20],
    )
"""
from __future__ import annotations

import math
from typing import Dict, List, Set, Any

import numpy as np


def hit_at_k(ranked: List[str], positives: Set[str], k: int) -> float:
    """Returns 1.0 if at least one positive is in top-k, else 0.0."""
    return 1.0 if any(d in positives for d in ranked[:k]) else 0.0


def precision_at_k(ranked: List[str], positives: Set[str], k: int) -> float:
    """Fraction of top-k drugs that are known positives."""
    topk = ranked[:k]
    if len(topk) == 0:
        return 0.0
    return sum(1 for d in topk if d in positives) / len(topk)


def reciprocal_rank(ranked: List[str], positives: Set[str]) -> float:
    """1 / rank of the first known positive drug. Returns 0 if none found."""
    for i, d in enumerate(ranked):
        if d in positives:
            return 1.0 / (i + 1)
    return 0.0


def average_precision(ranked: List[str], positives: Set[str]) -> float:
    """Average Precision: mean of precision values at each positive rank.

    AP = (1/|P|) * SUM_{k: ranked[k] in P} precision@(k+1)
    """
    if not positives:
        return 0.0

    n_pos_found = 0
    sum_prec = 0.0
    for i, d in enumerate(ranked):
        if d in positives:
            n_pos_found += 1
            sum_prec += n_pos_found / (i + 1)

    return sum_prec / len(positives) if len(positives) > 0 else 0.0


def ndcg_at_k(ranked: List[str], positives: Set[str], k: int) -> float:
    """Normalized Discounted Cumulative Gain at K.

    DCG@K = SUM_{i=1}^{K} rel_i / log2(i + 1)
    IDCG@K = DCG of the ideal ranking (all positives first)
    NDCG@K = DCG@K / IDCG@K
    """
    topk = ranked[:k]

    # DCG
    dcg = 0.0
    for i, d in enumerate(topk):
        rel = 1.0 if d in positives else 0.0
        dcg += rel / math.log2(i + 2)  # i+2 because log2(1)=0

    # IDCG: ideal ranking puts all positives first
    n_pos_in_k = min(len(positives), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n_pos_in_k))

    return dcg / idcg if idcg > 0 else 0.0


def auroc(ranked: List[str], positives: Set[str]) -> float:
    """Area Under ROC Curve (Wilcoxon-Mann-Whitney statistic).

    AUROC = P(random positive ranked before random negative)
    """
    if not positives or len(positives) >= len(ranked):
        return 0.5

    n_pos = len(positives)
    n_neg = len(ranked) - n_pos

    if n_neg == 0:
        return 0.5

    # Sum of ranks of positives (1-indexed)
    rank_sum = 0
    for i, d in enumerate(ranked):
        if d in positives:
            rank_sum += (i + 1)

    # AUROC = (rank_sum - n_pos*(n_pos+1)/2) / (n_pos * n_neg)
    # But since lower rank = better (more negative score), we need to invert
    auc = (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    # In our case, positives should have LOWER ranks (sorted ascending by score)
    # so a good ranking gives low rank_sum → low AUC → we want 1 - AUC
    return 1.0 - auc


def auprc(ranked: List[str], positives: Set[str]) -> float:
    """Area Under Precision-Recall Curve.

    More informative than AUROC for highly imbalanced datasets.
    Approximated by trapezoidal rule over precision-recall pairs.
    """
    if not positives:
        return 0.0

    precisions = []
    recalls = []
    n_pos_found = 0

    for i, d in enumerate(ranked):
        if d in positives:
            n_pos_found += 1
        prec = n_pos_found / (i + 1)
        rec = n_pos_found / len(positives)
        precisions.append(prec)
        recalls.append(rec)

    # Trapezoidal approximation
    area = 0.0
    for i in range(1, len(recalls)):
        area += (recalls[i] - recalls[i - 1]) * (precisions[i] + precisions[i - 1]) / 2

    return area


def evaluate_ranking(
    ranked_drugs: List[str],
    known_positives: Set[str],
    ks: List[int] = None,
) -> Dict[str, Any]:
    """Comprehensive evaluation of a drug ranking against known positives.

    Args:
        ranked_drugs: List of drug names, sorted by score (best first).
        known_positives: Set of known effective drugs for the target disease.
        ks: List of K values for Hit@K, P@K, NDCG@K metrics.

    Returns:
        Dict with all metric values.
    """
    if ks is None:
        ks = [5, 10, 20, 50]

    # Filter to drugs that are actually in the ranking
    valid_positives = known_positives & set(ranked_drugs)

    results: Dict[str, Any] = {
        "n_ranked": len(ranked_drugs),
        "n_known_positives": len(known_positives),
        "n_positives_in_ranking": len(valid_positives),
        "mrr": reciprocal_rank(ranked_drugs, valid_positives),
        "map": average_precision(ranked_drugs, valid_positives),
        "auroc": auroc(ranked_drugs, valid_positives),
        "auprc": auprc(ranked_drugs, valid_positives),
    }

    for k in ks:
        results[f"hit@{k}"] = hit_at_k(ranked_drugs, valid_positives, k)
        results[f"precision@{k}"] = precision_at_k(ranked_drugs, valid_positives, k)
        results[f"ndcg@{k}"] = ndcg_at_k(ranked_drugs, valid_positives, k)

    return results
