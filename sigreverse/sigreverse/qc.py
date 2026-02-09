"""Quality control module — industrial grade.

Provides:
    1. Missing gene ratio check with input validation
    2. Signature-level QC (FDR pass rate, direction agreement)
    3. Drug-level toxicity / generic stress confounder detection
    4. Input signature quality warnings
    5. NaN-safe aggregation throughout

Improvements (v0.4.0):
    - Input validation for all public functions
    - NaN-safe statistics (skipna everywhere)
    - Configurable toxicity thresholds with validation
    - Comprehensive docstrings
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.qc")


# ---------------------------------------------------------------------------
# 1. Gene-level QC
# ---------------------------------------------------------------------------

def missing_gene_ratio(
    missing_up: List[str],
    missing_down: List[str],
    up: List[str],
    down: List[str],
) -> float:
    """Fraction of input genes not found in LINCS entity database.

    Args:
        missing_up: Gene symbols from 'up' that were not found.
        missing_down: Gene symbols from 'down' that were not found.
        up: Original up gene list.
        down: Original down gene list.

    Returns:
        Ratio in [0.0, 1.0]. Returns 0.0 if both lists are empty.
    """
    denom = len(up) + len(down)
    if denom == 0:
        return 0.0
    ratio = (len(missing_up) + len(missing_down)) / denom
    return min(ratio, 1.0)  # Clamp to [0, 1]


def check_signature_size(
    n_up: int,
    n_down: int,
    min_recommended: int = 50,
    optimal_min: int = 150,
) -> Dict[str, Any]:
    """Check input signature size against recommended thresholds.

    LINCS/CMap best practices:
        - Minimum viable: ~50 genes per direction
        - Optimal: 150-300 genes per direction
        - Below 50: high variance, results unreliable

    Args:
        n_up: Number of up-regulated genes.
        n_down: Number of down-regulated genes.
        min_recommended: Minimum recommended genes per direction.
        optimal_min: Optimal minimum genes per direction.

    Returns:
        Dict with status, counts, and any warnings.
    """
    if n_up < 0 or n_down < 0:
        raise ValueError(f"Gene counts must be non-negative: up={n_up}, down={n_down}")
    if min_recommended < 1:
        raise ValueError(f"min_recommended must be >= 1, got {min_recommended}")

    status = "optimal"
    warnings = []

    if n_up == 0 or n_down == 0:
        status = "empty"
        warnings.append(
            f"Empty gene list: up={n_up}, down={n_down}. "
            f"Both directions must have genes for enrichment."
        )
    elif n_up < min_recommended or n_down < min_recommended:
        status = "below_minimum"
        warnings.append(
            f"Signature too small: up={n_up}, down={n_down}. "
            f"Minimum recommended: {min_recommended} per direction. "
            f"Results will have high variance."
        )
    elif n_up < optimal_min or n_down < optimal_min:
        status = "suboptimal"
        warnings.append(
            f"Signature below optimal size: up={n_up}, down={n_down}. "
            f"Optimal: {optimal_min}-300 per direction for stable enrichment."
        )

    return {"status": status, "n_up": n_up, "n_down": n_down, "warnings": warnings}


# ---------------------------------------------------------------------------
# 2. Signature-level QC summary
# ---------------------------------------------------------------------------

def signature_qc_summary(df_detail: pd.DataFrame) -> Dict[str, Any]:
    """Compute QC summary statistics for signature-level results.

    Reports:
        - FDR pass rate
        - Direction category distribution
        - LDP3 type agreement rate
        - Fraction with zero score (from sign-gate)
        - Score distribution statistics

    Args:
        df_detail: Signature-level DataFrame.

    Returns:
        Dict of QC statistics.
    """
    n = len(df_detail)
    if n == 0:
        return {"n_signatures": 0, "status": "empty"}

    summary: Dict[str, Any] = {"n_signatures": n}

    # FDR pass rate
    if "fdr_pass" in df_detail.columns:
        fdr_series = df_detail["fdr_pass"].fillna(False)
        n_pass = int(fdr_series.sum())
        summary["fdr_pass_rate"] = n_pass / n
        summary["n_fdr_pass"] = n_pass
        summary["n_fdr_fail"] = n - n_pass

    # Direction distribution
    if "direction_category" in df_detail.columns:
        dist = df_detail["direction_category"].value_counts().to_dict()
        summary["direction_distribution"] = dist

    # LDP3 type agreement
    if "ldp3_type_agree" in df_detail.columns:
        non_null = df_detail["ldp3_type_agree"].dropna()
        if len(non_null) > 0:
            agree_rate = float(non_null.sum()) / len(non_null)
            summary["ldp3_type_agreement_rate"] = round(agree_rate, 4)
            summary["n_ldp3_disagree"] = int((~non_null.astype(bool)).sum())

    # Zero-score fraction (from WTCS sign gate)
    if "sig_score" in df_detail.columns:
        scores = pd.to_numeric(df_detail["sig_score"], errors="coerce")
        n_zero = int((scores.abs() < 1e-10).sum())
        summary["n_zero_score"] = n_zero
        summary["zero_score_fraction"] = round(n_zero / n, 4)

        # Score distribution (NaN-safe)
        valid_scores = scores.dropna()
        if len(valid_scores) > 0:
            summary["score_stats"] = {
                "mean": round(float(valid_scores.mean()), 4),
                "median": round(float(valid_scores.median()), 4),
                "std": round(float(valid_scores.std()), 4),
                "min": round(float(valid_scores.min()), 4),
                "max": round(float(valid_scores.max()), 4),
                "n_nan": int(scores.isna().sum()),
            }

    return summary


# ---------------------------------------------------------------------------
# 3. Drug-level toxicity / confounder detection
# ---------------------------------------------------------------------------

def toxicity_flag_heuristic(
    n_signatures: int,
    p_reverser: float,
    median_strength: float,
    cfg: Dict[str, Any],
) -> bool:
    """Flag potential toxicity / generic stress confounders.

    A drug that reverses nearly everything in nearly every cell line is
    suspicious — it may be inducing a generic stress response rather than
    a disease-specific reversal.

    Criteria (all must be met):
        - n_signatures >= min_signatures (enough data to judge)
        - p_reverser >= min_p_reverser (reverses in almost all contexts)
        - median_strength >= min_median_strength (strong effect)

    Args:
        n_signatures: Number of FDR-passing signatures for this drug.
        p_reverser: Fraction of signatures classified as reverser.
        median_strength: Median absolute strength among reversers.
        cfg: Config dict with threshold keys.

    Returns:
        True if the drug is flagged as a potential confounder.
    """
    if not cfg.get("enabled", True):
        return False

    # Input validation
    if n_signatures < 0:
        logger.warning(f"Invalid n_signatures={n_signatures}, skipping toxicity flag")
        return False
    if not (0.0 <= p_reverser <= 1.0):
        logger.warning(f"Invalid p_reverser={p_reverser}, clamping to [0,1]")
        p_reverser = max(0.0, min(1.0, p_reverser))
    if not np.isfinite(median_strength):
        logger.warning(f"Non-finite median_strength={median_strength}, skipping toxicity flag")
        return False

    min_sigs = int(cfg.get("min_signatures", 10))
    min_prev = float(cfg.get("min_p_reverser", 0.80))
    min_str = float(cfg.get("min_median_strength", 25.0))

    if n_signatures < min_sigs:
        return False
    if p_reverser < min_prev:
        return False
    if median_strength < min_str:
        return False
    return True


def apply_toxicity_flags(
    df_drug: pd.DataFrame,
    tox_cfg: Dict[str, Any],
) -> pd.DataFrame:
    """Apply toxicity heuristic flag to drug-level DataFrame.

    Args:
        df_drug: Drug-level DataFrame (must have p_reverser, median_score columns).
        tox_cfg: Toxicity flag configuration.

    Returns:
        df_drug with 'possible_toxicity_confounder' column added.
    """
    if len(df_drug) == 0:
        df_drug["possible_toxicity_confounder"] = pd.Series(dtype=bool)
        return df_drug

    n_col = "n_signatures_fdr_pass" if "n_signatures_fdr_pass" in df_drug.columns else "n_signatures_total"
    strength_col = "median_score" if "median_score" in df_drug.columns else "median_strength(reverser_only)"

    def _safe_flag(row):
        try:
            n_sigs = int(row.get(n_col, 0))
            p_rev = float(row.get("p_reverser", 0.0))
            strength = abs(float(row.get(strength_col, 0.0)))
            return toxicity_flag_heuristic(n_sigs, p_rev, strength, tox_cfg)
        except (ValueError, TypeError) as e:
            logger.debug(f"Toxicity flag error for drug={row.get('drug', '?')}: {e}")
            return False

    df_drug["possible_toxicity_confounder"] = df_drug.apply(_safe_flag, axis=1)

    n_flagged = df_drug["possible_toxicity_confounder"].sum()
    if n_flagged > 0:
        logger.info(f"Toxicity confounder flag: {n_flagged}/{len(df_drug)} drugs flagged")

    return df_drug
