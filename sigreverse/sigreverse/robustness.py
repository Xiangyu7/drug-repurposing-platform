"""Drug-level aggregation with robustness weighting.

Aggregates signature-level scores to drug-level with:
    1. FDR-filtered signature inclusion
    2. Confidence-weighted aggregation (Fisher logp weight)
    3. Cell-line relevance weighting (if weights provided)
    4. Time-window weighting (24h > 6h)
    5. Sample-size saturation (soft log penalty, not harsh sqrt)
    6. Cross-context consistency (p_reverser)
    7. Cell-line diversity bonus (multi-context evidence)
    8. Tiered confidence levels (high/medium/low/exploratory)
    9. Quantile-based summarization (CMap NCSct-inspired alternative available)

Aggregation formula (default weighted-median):
    For each drug, collect FDR-passing signatures, compute:
        effective_weight_i = confidence_weight_i * cell_line_weight_i * time_weight_i
        median_score = weighted_median(sig_scores, effective_weights)
        p_reverser = n_reverser / n_total
        n_factor = log(1 + n) / log(1 + n_cap)  # soft log saturation
        cl_diversity = (1 + 0.1 * (n_unique_cell_lines - 1))  # multi-context bonus
        final_score = median_score * p_reverser * n_factor * cl_diversity

    v0.3.1 change: replaced sqrt(n/n_cap) with log(1+n)/log(1+n_cap).
        Old: single-sig drug gets √(1/20) = 0.224 (78% penalty)
        New: single-sig drug gets log(2)/log(21) = 0.228  → BUT n_cap lowered to 8
             so log(2)/log(9) = 0.316 (68% penalty, much gentler)
        Also: n_cap default lowered from 20 to 8 (more realistic for LDP3 sparse data)

Alternative quantile-max mode (CMap NCSct-inspired):
    Q_hi  = 67th percentile of sig_scores across cell lines
    Q_low = 33rd percentile
    final_score = Q_hi if |Q_hi| >= |Q_low| else Q_low
    (Preserves signal from minority cell lines where drug is active)

Confidence tiers:
    high:         ≥5 FDR-pass sigs, ≥2 cell lines, p_reverser ≥ 0.6
    medium:       ≥3 FDR-pass sigs, p_reverser ≥ 0.5
    low:          ≥2 FDR-pass sigs, p_reverser > 0
    exploratory:  1 FDR-pass sig (single-evidence, treat with caution)
"""
from __future__ import annotations

import math
import logging
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.robustness")


# ---------------------------------------------------------------------------
# Cell-line and time weighting defaults
# ---------------------------------------------------------------------------

DEFAULT_TIME_WEIGHTS = {
    "24 h": 1.0,
    "6 h": 0.6,
    "48 h": 0.9,
    "72 h": 0.8,
    "3 h": 0.4,
}


def load_cell_line_weights(path: Optional[str] = None) -> Dict[str, float]:
    """Load cell line relevance weights from CSV.

    Expected CSV format: cell_line, tissue, relevance_weight
    If no file provided, returns empty dict (all cell lines weight=1.0).
    """
    if path is None:
        return {}
    try:
        df = pd.read_csv(path)
        return dict(zip(df["cell_line"], df["relevance_weight"]))
    except Exception as e:
        logger.warning(f"Failed to load cell line weights from {path}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Weighted median helper
# ---------------------------------------------------------------------------

def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Compute weighted median.

    Args:
        values: Array of values.
        weights: Array of positive weights (same length as values).

    Returns:
        Weighted median value.
    """
    if len(values) == 0:
        return 0.0
    if len(values) == 1:
        return float(values[0])

    # Sort by value
    sorted_idx = np.argsort(values)
    sorted_vals = values[sorted_idx]
    sorted_weights = weights[sorted_idx]

    # Cumulative weight
    cum_weights = np.cumsum(sorted_weights)
    total = cum_weights[-1]

    if total <= 0:
        return float(np.median(values))

    # Find first index where cumulative weight >= total/2
    median_idx = np.searchsorted(cum_weights, total / 2.0)
    return float(sorted_vals[min(median_idx, len(sorted_vals) - 1)])


# ---------------------------------------------------------------------------
# Main aggregation function
# ---------------------------------------------------------------------------

def _compute_confidence_tier(
    n_fdr_pass: int,
    n_cell_lines: int,
    p_rev: float,
) -> str:
    """Determine confidence tier based on evidence strength.

    Tiers:
        high:         ≥5 FDR-pass sigs, ≥2 cell lines, p_reverser ≥ 0.6
        medium:       ≥3 FDR-pass sigs, p_reverser ≥ 0.5
        low:          ≥2 FDR-pass sigs, p_reverser > 0
        exploratory:  1 FDR-pass sig (single-evidence, treat with caution)
    """
    if n_fdr_pass >= 5 and n_cell_lines >= 2 and p_rev >= 0.6:
        return "high"
    elif n_fdr_pass >= 3 and p_rev >= 0.5:
        return "medium"
    elif n_fdr_pass >= 2 and p_rev > 0:
        return "low"
    else:
        return "exploratory"


def _compute_n_factor(n: int, n_cap: int, mode: str = "log") -> float:
    """Compute sample-size adjustment factor.

    Modes:
        "log":  log(1+n) / log(1+n_cap) — soft logarithmic saturation.
                n=1 → log(2)/log(1+n_cap), n=n_cap → 1.0
        "sqrt": sqrt(min(n, n_cap) / n_cap) — original, harsher penalty.

    With n_cap=8 (default for LDP3 sparse data):
        log mode:  n=1 → 0.334, n=2 → 0.530, n=3 → 0.668, n=5 → 0.864, n=8 → 1.0
        sqrt mode: n=1 → 0.354, n=2 → 0.500, n=3 → 0.612, n=5 → 0.791, n=8 → 1.0
    """
    if n <= 0:
        return 0.0
    n = min(n, n_cap)
    if mode == "sqrt":
        return math.sqrt(n / n_cap)
    else:
        return math.log(1 + n) / math.log(1 + n_cap)


def aggregate_to_drug(
    df_detail: pd.DataFrame,
    n_cap: int = 8,
    min_signatures: int = 1,
    min_reverser: int = 1,
    filter_fdr: bool = True,
    cell_line_weights: Optional[Dict[str, float]] = None,
    time_weights: Optional[Dict[str, float]] = None,
    aggregation_mode: str = "weighted_median",  # or "quantile_max"
    n_factor_mode: str = "log",  # "log" (soft) or "sqrt" (harsh)
    cl_diversity_bonus: float = 0.1,  # bonus per extra cell line
) -> pd.DataFrame:
    """Aggregate signature-level results to drug-level with robustness weighting.

    Expected columns in df_detail:
        - meta.pert_name: drug identifier
        - sig_score: continuous signature score (more negative = stronger reversal)
        - sig_strength: absolute strength of directional signal
        - is_reverser: boolean reverser classification
        - fdr_pass: boolean FDR significance flag
        - confidence_weight: Fisher logp-derived weight
        - direction_category: 'reverser'|'mimicker'|'partial'|'orthogonal'
        Optional:
        - meta.cell_line: cell line identifier
        - meta.pert_time: perturbation time

    Args:
        df_detail: Signature-level DataFrame.
        n_cap: Sample size saturation cap (default 8, tuned for LDP3 sparse data).
        min_signatures: Minimum total signatures for drug scoring (default 1).
        min_reverser: Minimum reverser signatures required (default 1).
        filter_fdr: If True, exclude FDR-failing signatures from aggregation.
        cell_line_weights: Dict mapping cell_line → relevance weight.
        time_weights: Dict mapping pert_time → temporal weight.
        aggregation_mode: 'weighted_median' or 'quantile_max'.
        n_factor_mode: 'log' (gentle) or 'sqrt' (harsh) sample-size penalty.
        cl_diversity_bonus: Bonus multiplier per additional cell line (0 = disabled).

    Returns:
        Drug-level DataFrame sorted by final_reversal_score (ascending).
    """
    if cell_line_weights is None:
        cell_line_weights = {}
    if time_weights is None:
        time_weights = DEFAULT_TIME_WEIGHTS

    rows = []
    required_col = "meta.pert_name"
    if required_col not in df_detail.columns:
        raise ValueError(f"df_detail must contain column: {required_col}")

    # Ensure score columns exist
    score_col = "sig_score" if "sig_score" in df_detail.columns else "sig_strength"

    for drug, g in df_detail.groupby(required_col, dropna=True):
        n_total = len(g)

        # --- Filter by FDR if enabled ---
        if filter_fdr and "fdr_pass" in g.columns:
            g_filtered = g[g["fdr_pass"] == True].copy()
        else:
            g_filtered = g.copy()

        n_after_fdr = len(g_filtered)
        n_fdr_removed = n_total - n_after_fdr

        # --- Count reversers ---
        if "is_reverser" in g_filtered.columns:
            n_rev = int(g_filtered["is_reverser"].sum())
        else:
            n_rev = 0
        p_rev = (n_rev / n_after_fdr) if n_after_fdr > 0 else 0.0

        # --- Cell-line diversity ---
        n_cell_lines = 1
        if "meta.cell_line" in g_filtered.columns:
            n_cell_lines = max(1, g_filtered["meta.cell_line"].nunique())

        # --- Direction category distribution ---
        cat_dist = {}
        if "direction_category" in g_filtered.columns:
            cat_dist = g_filtered["direction_category"].value_counts().to_dict()

        # --- Confidence tier ---
        confidence_tier = _compute_confidence_tier(n_after_fdr, n_cell_lines, p_rev)

        # --- Check minimum thresholds ---
        if n_after_fdr < min_signatures:
            rows.append(_make_drug_row(
                drug, 0.0, p_rev, n_total, n_after_fdr, n_rev, n_fdr_removed,
                0.0, 0.0, cat_dist, "too_few_signatures",
                confidence_tier=confidence_tier, n_cell_lines=n_cell_lines,
            ))
            continue

        if n_rev < min_reverser:
            med_s = float(g_filtered[score_col].median()) if len(g_filtered) > 0 else 0.0
            rows.append(_make_drug_row(
                drug, 0.0, p_rev, n_total, n_after_fdr, n_rev, n_fdr_removed,
                med_s, 0.0, cat_dist, "no_reverser_context",
                confidence_tier=confidence_tier, n_cell_lines=n_cell_lines,
            ))
            continue

        # --- Compute effective weights per signature ---
        effective_weights = _compute_effective_weights(
            g_filtered, cell_line_weights, time_weights
        )

        # --- Aggregate to drug-level score ---
        scores = g_filtered[score_col].values.astype(float)
        weights = effective_weights

        # --- Detect cell-line direction conflict ---
        has_cl_conflict = False
        if n_cell_lines >= 2 and "meta.cell_line" in g_filtered.columns and "direction_category" in g_filtered.columns:
            cl_directions = g_filtered.groupby("meta.cell_line")["direction_category"].agg(
                lambda x: x.mode().iloc[0] if len(x) > 0 else "unknown"
            )
            unique_dirs = set(cl_directions.values) - {"partial", "orthogonal"}
            has_cl_conflict = len(unique_dirs) >= 2  # e.g., both reverser AND mimicker

        # --- Aggregation: use quantile_max when cell-line conflict detected ---
        if aggregation_mode == "quantile_max" or has_cl_conflict:
            agg_score = _quantile_max_aggregate(scores)
            if has_cl_conflict:
                logger.debug(f"  {drug}: cell-line conflict detected, using quantile_max")
        else:
            agg_score = weighted_median(scores, weights)

        # --- Robustness weighting (v0.3.1: soft log + cell-line diversity) ---
        n_factor = _compute_n_factor(n_after_fdr, n_cap, mode=n_factor_mode)

        # Cell-line diversity bonus: reward drugs tested in multiple contexts
        # BUT apply a penalty for conflicting cell lines
        if has_cl_conflict:
            cl_bonus = 1.0  # no bonus for conflicting evidence
        else:
            cl_bonus = 1.0 + cl_diversity_bonus * max(0, n_cell_lines - 1)

        final_score = agg_score * p_rev * n_factor * cl_bonus

        # --- Stability metrics ---
        rev_scores = g_filtered.loc[
            g_filtered.get("is_reverser", pd.Series(dtype=bool)) == True,
            score_col
        ].values if "is_reverser" in g_filtered.columns else np.array([])

        if len(rev_scores) >= 2:
            iqr = float(np.percentile(rev_scores, 75) - np.percentile(rev_scores, 25))
        elif len(rev_scores) == 1:
            iqr = 0.0
        else:
            iqr = 0.0

        median_score = float(np.median(scores))

        rows.append(_make_drug_row(
            drug, float(final_score), float(p_rev), n_total, n_after_fdr,
            n_rev, n_fdr_removed, median_score, iqr, cat_dist, "ok",
            confidence_tier=confidence_tier, n_cell_lines=n_cell_lines,
            has_cl_conflict=has_cl_conflict,
        ))

    df_drug = pd.DataFrame(rows)
    if len(df_drug) > 0:
        df_drug = df_drug.sort_values("final_reversal_score", ascending=True)

    logger.info(
        f"Aggregated {len(df_drug)} drugs: "
        f"{(df_drug['status'] == 'ok').sum()} ok, "
        f"{(df_drug['status'] == 'too_few_signatures').sum()} too_few, "
        f"{(df_drug['status'] == 'no_reverser_context').sum()} no_reverser"
    )
    return df_drug


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_effective_weights(
    g: pd.DataFrame,
    cell_line_weights: Dict[str, float],
    time_weights: Dict[str, float],
) -> np.ndarray:
    """Compute per-signature effective weights combining all factors."""
    n = len(g)
    weights = np.ones(n, dtype=float)

    # Confidence weight from Fisher logp
    if "confidence_weight" in g.columns:
        weights *= g["confidence_weight"].values.astype(float)

    # Cell line relevance
    if cell_line_weights and "meta.cell_line" in g.columns:
        cl_w = g["meta.cell_line"].map(
            lambda x: cell_line_weights.get(x, 0.5)  # unknown cell lines get 0.5
        ).values.astype(float)
        weights *= cl_w

    # Time window weighting
    if time_weights and "meta.pert_time" in g.columns:
        t_w = g["meta.pert_time"].map(
            lambda x: time_weights.get(str(x).strip(), 0.7)  # unknown times get 0.7
        ).values.astype(float)
        weights *= t_w

    # Ensure no zero weights
    weights = np.maximum(weights, 1e-6)
    return weights


def _quantile_max_aggregate(scores: np.ndarray) -> float:
    """CMap NCSct-inspired quantile-max aggregation.

    Takes 67th and 33rd percentile of scores across all contexts.
    Returns whichever has larger absolute value.
    This preserves signal from minority cell lines where the drug is active.
    """
    if len(scores) == 0:
        return 0.0
    if len(scores) == 1:
        return float(scores[0])

    q_hi = float(np.percentile(scores, 67))
    q_lo = float(np.percentile(scores, 33))
    return q_hi if abs(q_hi) >= abs(q_lo) else q_lo


def _make_drug_row(
    drug: str,
    final_score: float,
    p_rev: float,
    n_total: int,
    n_after_fdr: int,
    n_rev: int,
    n_fdr_removed: int,
    median_score: float,
    iqr_score: float,
    cat_dist: dict,
    status: str,
    confidence_tier: str = "exploratory",
    n_cell_lines: int = 1,
    has_cl_conflict: bool = False,
) -> dict:
    """Create a standardized drug-level result row."""
    return {
        "drug": drug,
        "final_reversal_score": final_score,
        "p_reverser": p_rev,
        "n_signatures_total": n_total,
        "n_signatures_fdr_pass": n_after_fdr,
        "n_reverser": n_rev,
        "n_fdr_removed": n_fdr_removed,
        "median_score": median_score,
        "iqr_score": iqr_score,
        "n_mimicker": cat_dist.get("mimicker", 0),
        "n_partial": cat_dist.get("partial", 0),
        "n_orthogonal": cat_dist.get("orthogonal", 0),
        "n_cell_lines": n_cell_lines,
        "confidence_tier": confidence_tier,
        "has_cl_conflict": has_cl_conflict,
        "status": status,
    }
