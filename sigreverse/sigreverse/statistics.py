"""Statistical inference module for drug-level significance testing.

Provides:
    1. Permutation-based null distribution generation
    2. Empirical p-value computation
    3. Benjamini-Hochberg FDR correction
    4. Bootstrap confidence intervals for drug-level scores
    5. Effect size normalization (z-normalized scores)

References:
    - CMap Tau score: percentile rank against Touchstone reference distribution
    - Duan et al. 2021, Scientific Reports: CMap reproducibility study
    - Benjamini & Hochberg 1995: FDR procedure
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.statistics")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DrugSignificance:
    """Statistical significance result for a single drug."""
    drug: str
    observed_score: float
    perm_pvalue: float          # empirical p-value from permutation test
    fdr_bh: float               # Benjamini-Hochberg corrected p-value
    z_normalized: float         # (observed - mean_null) / std_null
    bootstrap_ci_lo: float      # 95% CI lower bound
    bootstrap_ci_hi: float      # 95% CI upper bound
    ci_excludes_zero: bool      # True if the entire CI is < 0 (significant reversal)
    n_permutations: int
    n_bootstrap: int


# ---------------------------------------------------------------------------
# 1. Permutation test — drug-level null distribution
# ---------------------------------------------------------------------------

def permutation_null_distribution(
    df_detail: pd.DataFrame,
    score_col: str = "sig_score",
    drug_col: str = "meta.pert_name",
    n_permutations: int = 1000,
    seed: int = 42,
    aggregation: str = "median",
) -> Dict[str, np.ndarray]:
    """Generate null distribution of drug-level scores by permuting signature scores.

    Strategy: for each permutation, shuffle the sig_score column across ALL
    signatures (breaking the drug-signature association), then re-aggregate
    to drug level. This preserves the marginal score distribution but destroys
    the drug-specific signal.

    This is the "label permutation" approach — equivalent to asking:
    "If this drug's signatures were drawn randomly from the pool of all
    signatures, what drug-level score would we expect?"

    Args:
        df_detail: Signature-level DataFrame with score and drug columns.
        score_col: Column name for signature-level scores.
        drug_col: Column name for drug identity.
        n_permutations: Number of permutations (1000 recommended).
        seed: Random seed for reproducibility.
        aggregation: 'median' or 'mean' for drug-level aggregation.

    Returns:
        Dict mapping drug name → array of n_permutations null scores.
    """
    rng = np.random.default_rng(seed)

    scores = df_detail[score_col].values.copy()
    drugs = df_detail[drug_col].values
    unique_drugs = pd.unique(drugs)

    # Pre-compute drug group indices for fast aggregation
    drug_indices: Dict[str, np.ndarray] = {}
    for drug in unique_drugs:
        drug_indices[drug] = np.where(drugs == drug)[0]

    null_distributions: Dict[str, List[float]] = {d: [] for d in unique_drugs}

    agg_fn = np.median if aggregation == "median" else np.mean

    logger.info(
        f"Running permutation test: {n_permutations} permutations, "
        f"{len(unique_drugs)} drugs, {len(scores)} signatures"
    )

    for i in range(n_permutations):
        shuffled = rng.permutation(scores)
        for drug in unique_drugs:
            idx = drug_indices[drug]
            null_score = float(agg_fn(shuffled[idx]))
            null_distributions[drug].append(null_score)

    return {d: np.array(v) for d, v in null_distributions.items()}


def compute_empirical_pvalue(
    observed: float,
    null_dist: np.ndarray,
) -> float:
    """Compute one-sided empirical p-value.

    For reversal scoring (more negative = better), p-value is the fraction
    of null scores that are <= the observed score.

    Adds pseudocount of 1 to avoid p=0:
        p = (count(null <= observed) + 1) / (N + 1)

    Args:
        observed: Observed drug-level score.
        null_dist: Array of null scores from permutation test.

    Returns:
        Empirical p-value in (0, 1].
    """
    n = len(null_dist)
    count_leq = np.sum(null_dist <= observed)
    return float(count_leq + 1) / float(n + 1)


# ---------------------------------------------------------------------------
# 2. Benjamini-Hochberg FDR correction
# ---------------------------------------------------------------------------

def benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg FDR correction.

    Args:
        pvalues: Array of raw p-values.

    Returns:
        Array of FDR-adjusted p-values (q-values), same order as input.
    """
    n = len(pvalues)
    if n == 0:
        return np.array([])

    # Sort p-values and track original indices
    sorted_idx = np.argsort(pvalues)
    sorted_pvals = pvalues[sorted_idx]

    # BH adjustment: q_i = p_i * n / rank_i, then enforce monotonicity
    ranks = np.arange(1, n + 1, dtype=float)
    adjusted = sorted_pvals * n / ranks

    # Enforce monotonicity from the end (cumulative minimum in reverse)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]

    # Clip to [0, 1]
    adjusted = np.clip(adjusted, 0.0, 1.0)

    # Restore original order
    result = np.empty(n)
    result[sorted_idx] = adjusted
    return result


# ---------------------------------------------------------------------------
# 3. Bootstrap confidence interval
# ---------------------------------------------------------------------------

def bootstrap_confidence_interval(
    values: np.ndarray,
    n_bootstrap: int = 2000,
    confidence: float = 0.95,
    statistic: str = "median",
    seed: int = 42,
) -> Tuple[float, float]:
    """Compute bootstrap confidence interval for a summary statistic.

    Uses the percentile method (simplest, most robust for non-normal data).

    Args:
        values: Array of observations (e.g., reverser sig_scores for one drug).
        n_bootstrap: Number of bootstrap resamples.
        confidence: Confidence level (default 0.95 for 95% CI).
        statistic: 'median' or 'mean'.
        seed: Random seed.

    Returns:
        (ci_lower, ci_upper)
    """
    if len(values) < 2:
        v = float(values[0]) if len(values) == 1 else 0.0
        return v, v

    rng = np.random.default_rng(seed)
    stat_fn = np.median if statistic == "median" else np.mean

    boot_stats = np.empty(n_bootstrap)
    n = len(values)
    for i in range(n_bootstrap):
        sample = rng.choice(values, size=n, replace=True)
        boot_stats[i] = stat_fn(sample)

    alpha = 1.0 - confidence
    lo = float(np.percentile(boot_stats, 100 * alpha / 2))
    hi = float(np.percentile(boot_stats, 100 * (1 - alpha / 2)))
    return lo, hi


# ---------------------------------------------------------------------------
# 4. Effect size normalization
# ---------------------------------------------------------------------------

def normalize_effect_size(
    observed: float,
    null_dist: np.ndarray,
) -> float:
    """Compute z-normalized effect size.

    z = (observed - mean_null) / std_null

    For reversal scoring, a large negative z indicates that the observed
    reversal score is many standard deviations below the null expectation.

    Args:
        observed: Observed drug-level score.
        null_dist: Array of null scores from permutation test.

    Returns:
        z-normalized score. Returns 0.0 if null std is zero.
    """
    mu = float(np.mean(null_dist))
    sigma = float(np.std(null_dist, ddof=1))
    if sigma < 1e-12:
        return 0.0
    return (observed - mu) / sigma


# ---------------------------------------------------------------------------
# 5. Integrated drug-level significance pipeline
# ---------------------------------------------------------------------------

def compute_drug_significance(
    df_detail: pd.DataFrame,
    df_drug: pd.DataFrame,
    score_col: str = "sig_score",
    drug_col: str = "meta.pert_name",
    drug_score_col: str = "final_reversal_score",
    n_permutations: int = 1000,
    n_bootstrap: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> pd.DataFrame:
    """Full significance pipeline: permutation + FDR + bootstrap + effect size.

    Args:
        df_detail: Signature-level DataFrame.
        df_drug: Drug-level DataFrame with aggregated scores.
        score_col: Signature-level score column.
        drug_col: Drug identity column in df_detail.
        drug_score_col: Drug-level score column in df_drug.
        n_permutations: Number of permutations.
        n_bootstrap: Number of bootstrap resamples.
        confidence: Confidence level for CI.
        seed: Random seed.

    Returns:
        DataFrame with drug-level significance results, aligned with df_drug.
    """
    # Step 1: Permutation null distributions
    null_dists = permutation_null_distribution(
        df_detail, score_col=score_col, drug_col=drug_col,
        n_permutations=n_permutations, seed=seed,
    )

    # Step 2: Empirical p-values for each drug
    drug_pvalues = {}
    drug_z_scores = {}
    for _, row in df_drug.iterrows():
        drug = row["drug"]
        observed = float(row[drug_score_col])
        if drug in null_dists:
            null_dist = null_dists[drug]
            drug_pvalues[drug] = compute_empirical_pvalue(observed, null_dist)
            drug_z_scores[drug] = normalize_effect_size(observed, null_dist)
        else:
            drug_pvalues[drug] = 1.0
            drug_z_scores[drug] = 0.0

    # Step 3: BH-FDR correction
    drugs_ordered = list(drug_pvalues.keys())
    raw_pvals = np.array([drug_pvalues[d] for d in drugs_ordered])
    fdr_vals = benjamini_hochberg(raw_pvals)
    drug_fdr = dict(zip(drugs_ordered, fdr_vals))

    # Step 4: Bootstrap CI per drug
    drug_ci = {}
    for drug in drugs_ordered:
        mask = df_detail[drug_col] == drug
        drug_scores = df_detail.loc[mask, score_col].values
        if len(drug_scores) >= 2:
            ci_lo, ci_hi = bootstrap_confidence_interval(
                drug_scores, n_bootstrap=n_bootstrap,
                confidence=confidence, seed=seed,
            )
        elif len(drug_scores) == 1:
            ci_lo = ci_hi = float(drug_scores[0])
        else:
            ci_lo = ci_hi = 0.0
        drug_ci[drug] = (ci_lo, ci_hi)

    # Step 5: Assemble results
    rows = []
    for drug in drugs_ordered:
        ci_lo, ci_hi = drug_ci[drug]
        rows.append({
            "drug": drug,
            "perm_pvalue": drug_pvalues[drug],
            "fdr_bh": float(drug_fdr[drug]),
            "z_normalized": drug_z_scores[drug],
            "bootstrap_ci_lo": ci_lo,
            "bootstrap_ci_hi": ci_hi,
            "ci_excludes_zero": ci_hi < 0.0,
            "n_permutations": n_permutations,
            "n_bootstrap": n_bootstrap,
        })

    df_sig = pd.DataFrame(rows)
    logger.info(
        f"Significance: {(df_sig['fdr_bh'] < 0.05).sum()}/{len(df_sig)} drugs "
        f"pass FDR<0.05; {df_sig['ci_excludes_zero'].sum()} have CI excluding zero"
    )
    return df_sig
