"""Bootstrap confidence intervals for drug-disease ranking scores.

Each drug-disease pair has N evidence paths with individual path_score values.
v3: Uses BLOCK BOOTSTRAP — resamples by target group instead of individual
paths, since paths through the same target are correlated (not independent).

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


def block_bootstrap_ci(
    grouped_scores: Dict[str, List[float]],
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
    agg_fn: str = "mean",
) -> Dict[str, float]:
    """Compute BLOCK bootstrap CI — resample by target group.

    v3: Paths through the same target are correlated (e.g., LDLR → pathway_A
    and LDLR → pathway_B are not independent).  Block bootstrap resamples
    entire target groups instead of individual paths, producing wider and
    more honest confidence intervals.

    Args:
        grouped_scores: target_id → list of path scores for that target
        n_bootstrap: Number of bootstrap resamples
        ci: Confidence level
        seed: Random seed
        agg_fn: "mean" or "median"

    Returns:
        {"mean": float, "ci_lower": float, "ci_upper": float,
         "ci_width": float, "n_paths": int, "n_groups": int}
    """
    all_scores = []
    for scores in grouped_scores.values():
        all_scores.extend(scores)
    n_total = len(all_scores)

    if n_total == 0:
        return {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0,
                "ci_width": 0.0, "n_paths": 0, "n_groups": 0}

    all_arr = np.array(all_scores, dtype=float)
    point_est = float(np.mean(all_arr)) if agg_fn == "mean" else float(np.median(all_arr))

    n_groups = len(grouped_scores)
    if n_groups <= 1:
        # Only one target → fallback to standard bootstrap on paths
        return {
            **bootstrap_ci(all_scores, n_bootstrap, ci, seed, agg_fn),
            "n_groups": n_groups,
        }

    # Block bootstrap: resample target groups with replacement
    group_keys = list(grouped_scores.keys())
    group_arrays = [np.array(grouped_scores[k], dtype=float) for k in group_keys]

    rng = np.random.RandomState(seed)
    boot_stats = np.empty(n_bootstrap, dtype=float)
    agg = np.mean if agg_fn == "mean" else np.median

    for i in range(n_bootstrap):
        # Resample n_groups groups with replacement
        chosen = rng.choice(n_groups, size=n_groups, replace=True)
        resampled = np.concatenate([group_arrays[j] for j in chosen])
        boot_stats[i] = agg(resampled)

    alpha = 1.0 - ci
    lower = float(np.percentile(boot_stats, 100 * alpha / 2))
    upper = float(np.percentile(boot_stats, 100 * (1 - alpha / 2)))

    return {
        "mean": round(point_est, 6),
        "ci_lower": round(lower, 6),
        "ci_upper": round(upper, 6),
        "ci_width": round(upper - lower, 6),
        "n_paths": n_total,
        "n_groups": n_groups,
    }


def assign_confidence_tier(
    ci_width: float,
    n_paths: int = 0,
    mean_score: float = 0.0,
    n_groups: int = 0,
) -> str:
    """Assign a confidence tier based on CI width, path count, AND score level.

    v3: Added n_groups (unique targets) — HIGH confidence requires multiple
    independent target groups, not just many correlated paths through one target.

    Args:
        ci_width: Width of the confidence interval
        n_paths: Number of evidence paths for this pair
        mean_score: Mean path score (to prevent high confidence on low scores)
        n_groups: Number of unique target groups (0 = unknown/legacy)

    Returns:
        "HIGH", "MEDIUM", or "LOW"
    """
    # Too few paths → LOW regardless of CI width
    if n_paths <= 1:
        return "LOW"
    if n_paths <= 2:
        if ci_width < 0.25:
            return "MEDIUM"
        return "LOW"

    # v2: Uniformly low scores → cap at MEDIUM even with narrow CI
    if mean_score < 0.15:
        if ci_width < 0.15:
            return "MEDIUM"
        return "LOW"

    # v3: HIGH requires multiple independent targets (groups)
    # If n_groups is available (>0), use it; otherwise fallback to n_paths
    effective_independence = n_groups if n_groups > 0 else n_paths

    # 3+ paths with reasonable scores: use CI width thresholds
    if ci_width < 0.10 and effective_independence >= 3:
        return "HIGH"
    elif ci_width < 0.10:
        return "MEDIUM"  # Few independent groups → cap at MEDIUM
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

    v3: Uses block bootstrap grouped by target_chembl_id when target info
    is available in evidence paths.  Falls back to standard bootstrap when
    target grouping is unavailable.

    Args:
        rank_df: Ranking DataFrame (must have drug_normalized, diseaseId, final_score)
        evidence_paths: List of path dicts, each with "drug", "diseaseId", "path_score"
                        and optionally "nodes" (containing target info)
        n_bootstrap: Number of bootstrap resamples
        ci: Confidence level
        seed: Random seed

    Returns:
        rank_df with added columns: ci_lower, ci_upper, ci_width, confidence_tier, n_evidence_paths
    """
    # Group path scores by (drug, disease) AND by target within each pair
    # Structure: (drug, disease) → {target_id → [scores]}
    pair_target_scores: Dict[tuple, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    n_dropped = 0

    for path in evidence_paths:
        drug = str(path.get("drug", "")).lower().strip()
        disease = str(path.get("diseaseId", "")).strip()
        score = path.get("path_score")
        if score is None:
            n_dropped += 1
            continue
        try:
            score_f = float(score)
        except (ValueError, TypeError):
            n_dropped += 1
            continue

        # Extract target from nodes if available
        target_id = "unknown"
        nodes = path.get("nodes", [])
        if isinstance(nodes, list) and len(nodes) >= 2:
            target_node = nodes[1] if isinstance(nodes[1], dict) else {}
            target_id = str(target_node.get("id", "unknown"))

        pair_target_scores[(drug, disease)][target_id].append(score_f)

    if n_dropped > 0:
        logger.warning("Uncertainty: dropped %d paths with invalid/missing scores", n_dropped)

    # Compute CI for each pair using block bootstrap
    ci_records = []
    for (drug, disease), target_groups in pair_target_scores.items():
        has_target_info = not (len(target_groups) == 1 and "unknown" in target_groups)

        if has_target_info and len(target_groups) > 1:
            # Block bootstrap: resample by target group
            result = block_bootstrap_ci(
                target_groups, n_bootstrap=n_bootstrap, ci=ci, seed=seed,
            )
        else:
            # Standard bootstrap (no target grouping or single target)
            all_scores = []
            for scores in target_groups.values():
                all_scores.extend(scores)
            result = bootstrap_ci(
                all_scores, n_bootstrap=n_bootstrap, ci=ci, seed=seed,
            )
            result["n_groups"] = len(target_groups)

        ci_records.append({
            "drug_normalized": drug,
            "diseaseId": disease,
            "ci_lower": result["ci_lower"],
            "ci_upper": result["ci_upper"],
            "ci_width": result["ci_width"],
            "confidence_tier": assign_confidence_tier(
                result["ci_width"],
                n_paths=result["n_paths"],
                mean_score=result["mean"],
                n_groups=result.get("n_groups", 0),
            ),
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
        "Uncertainty (block bootstrap): %d HIGH, %d MEDIUM, %d LOW confidence pairs (mean CI width: %.4f)",
        n_high, n_med, n_low,
        result_df["ci_width"].mean() if not result_df.empty else 0.0,
    )

    return result_df
