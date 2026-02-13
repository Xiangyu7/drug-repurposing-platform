"""Bootstrap confidence intervals for drug-disease ranking scores.

Each drug-disease pair has N evidence paths with individual path_score values.
Bootstrap resample these N paths B times → compute score distribution → 95% CI.

Usage:
    ci = bootstrap_ci([0.8, 0.5, 0.3, 0.9], n_bootstrap=1000)
    print(f"Score: {ci['mean']:.3f} [{ci['ci_lower']:.3f}, {ci['ci_upper']:.3f}]")
    print(f"Confidence: {assign_confidence_tier(ci['ci_width'])}")
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def bootstrap_ci(
    path_scores: List[float],
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
    agg_fn: str = "mean",
) -> Dict[str, float]:
    """Compute bootstrap confidence interval for a set of path scores.

    Args:
        path_scores: Individual path scores for one drug-disease pair
        n_bootstrap: Number of bootstrap resamples
        ci: Confidence level (default 0.95 → 95% CI)
        seed: Random seed for reproducibility
        agg_fn: Aggregation function ("mean" or "median")

    Returns:
        {"mean": float, "ci_lower": float, "ci_upper": float,
         "ci_width": float, "n_paths": int}
    """
    n = len(path_scores)
    if n == 0:
        return {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0, "ci_width": 0.0, "n_paths": 0}

    scores = np.array(path_scores, dtype=float)
    point_est = float(np.mean(scores)) if agg_fn == "mean" else float(np.median(scores))

    if n == 1:
        return {"mean": point_est, "ci_lower": point_est, "ci_upper": point_est, "ci_width": 0.0, "n_paths": 1}

    rng = np.random.RandomState(seed)
    boot_stats = np.empty(n_bootstrap, dtype=float)
    for i in range(n_bootstrap):
        sample = rng.choice(scores, size=n, replace=True)
        boot_stats[i] = np.mean(sample) if agg_fn == "mean" else np.median(sample)

    alpha = 1.0 - ci
    lower = float(np.percentile(boot_stats, 100 * alpha / 2))
    upper = float(np.percentile(boot_stats, 100 * (1 - alpha / 2)))

    return {
        "mean": round(point_est, 6),
        "ci_lower": round(lower, 6),
        "ci_upper": round(upper, 6),
        "ci_width": round(upper - lower, 6),
        "n_paths": n,
    }


def assign_confidence_tier(ci_width: float) -> str:
    """Assign a confidence tier based on CI width.

    Args:
        ci_width: Width of the confidence interval

    Returns:
        "HIGH" (ci_width < 0.10), "MEDIUM" (< 0.25), or "LOW"
    """
    if ci_width < 0.10:
        return "HIGH"
    elif ci_width < 0.25:
        return "MEDIUM"
    else:
        return "LOW"


def add_uncertainty_to_ranking(
    rank_df: pd.DataFrame,
    evidence_paths: List[dict],
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> pd.DataFrame:
    """Add bootstrap CI columns to a ranking DataFrame.

    Groups evidence paths by (drug_normalized, diseaseId), computes bootstrap CI
    for each pair, and joins the results back to the ranking DataFrame.

    Args:
        rank_df: Ranking DataFrame (must have drug_normalized, diseaseId, final_score)
        evidence_paths: List of path dicts, each with "drug", "diseaseId", "path_score"
        n_bootstrap: Number of bootstrap resamples
        ci: Confidence level
        seed: Random seed

    Returns:
        rank_df with added columns: ci_lower, ci_upper, ci_width, confidence_tier, n_evidence_paths
    """
    # Group path scores by (drug, disease)
    pair_scores: Dict[tuple, List[float]] = defaultdict(list)
    for path in evidence_paths:
        drug = str(path.get("drug", "")).lower().strip()
        disease = str(path.get("diseaseId", "")).strip()
        score = path.get("path_score")
        if score is not None:
            try:
                pair_scores[(drug, disease)].append(float(score))
            except (ValueError, TypeError):
                continue

    # Compute CI for each pair
    ci_records = []
    for (drug, disease), scores in pair_scores.items():
        result = bootstrap_ci(scores, n_bootstrap=n_bootstrap, ci=ci, seed=seed)
        ci_records.append({
            "drug_normalized": drug,
            "diseaseId": disease,
            "ci_lower": result["ci_lower"],
            "ci_upper": result["ci_upper"],
            "ci_width": result["ci_width"],
            "confidence_tier": assign_confidence_tier(result["ci_width"]),
            "n_evidence_paths": result["n_paths"],
        })

    if not ci_records:
        rank_df = rank_df.copy()
        rank_df["ci_lower"] = 0.0
        rank_df["ci_upper"] = 0.0
        rank_df["ci_width"] = 0.0
        rank_df["confidence_tier"] = "LOW"
        rank_df["n_evidence_paths"] = 0
        return rank_df

    ci_df = pd.DataFrame(ci_records)
    result_df = rank_df.merge(ci_df, on=["drug_normalized", "diseaseId"], how="left")

    # Fill missing CI data for pairs with no evidence paths
    result_df["ci_lower"] = result_df["ci_lower"].fillna(0.0)
    result_df["ci_upper"] = result_df["ci_upper"].fillna(0.0)
    result_df["ci_width"] = result_df["ci_width"].fillna(0.0)
    result_df["confidence_tier"] = result_df["confidence_tier"].fillna("LOW")
    result_df["n_evidence_paths"] = result_df["n_evidence_paths"].fillna(0).astype(int)

    n_high = len(result_df[result_df["confidence_tier"] == "HIGH"])
    n_med = len(result_df[result_df["confidence_tier"] == "MEDIUM"])
    n_low = len(result_df[result_df["confidence_tier"] == "LOW"])
    logger.info(
        "Uncertainty: %d HIGH, %d MEDIUM, %d LOW confidence pairs (mean CI width: %.4f)",
        n_high, n_med, n_low,
        result_df["ci_width"].mean() if not result_df.empty else 0.0,
    )

    return result_df
