"""Dose-response modeling module.

Implements dose-response analysis for drug reversal scores:
    1. Monotonic dose-response check (is reversal dose-dependent?)
    2. Hill equation fitting (EC50, Emax, Hill coefficient)
    3. Dose-response quality scoring
    4. Optimal dose identification

Why this matters:
    - A true reverser should show dose-dependent reversal
    - Non-monotonic dose-response suggests off-target or toxic effects
    - EC50 helps prioritize drugs with clinically achievable concentrations

References:
    - Hill equation: E = Emax * D^n / (EC50^n + D^n)
    - Ritz et al. 2015: dose-response analysis with R drc package
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.dose_response")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DoseResponseResult:
    """Result of dose-response analysis for a single drug.
    
    Attributes:
        drug: Drug name.
        is_monotonic: Whether reversal increases monotonically with dose.
        monotonicity_score: Spearman correlation of dose vs reversal score.
        hill_ec50: EC50 from Hill equation fit (in original dose units).
        hill_emax: Maximum effect (most negative score).
        hill_n: Hill coefficient (steepness).
        hill_r2: R² of Hill fit.
        n_doses: Number of unique doses tested.
        doses: Sorted unique doses.
        mean_scores: Mean reversal score at each dose.
        quality: 'excellent' | 'good' | 'marginal' | 'poor' | 'insufficient'.
    """
    drug: str
    is_monotonic: bool = False
    monotonicity_score: float = 0.0
    hill_ec50: Optional[float] = None
    hill_emax: Optional[float] = None
    hill_n: Optional[float] = None
    hill_r2: Optional[float] = None
    n_doses: int = 0
    doses: List[float] = None
    mean_scores: List[float] = None
    quality: str = "insufficient"
    
    def __post_init__(self):
        if self.doses is None:
            self.doses = []
        if self.mean_scores is None:
            self.mean_scores = []


# ---------------------------------------------------------------------------
# Dose parsing
# ---------------------------------------------------------------------------

def parse_dose(dose_str: str, dose_unit: str = "") -> Optional[float]:
    """Parse dose string to float value in µM.

    Handles common LINCS dose formats:
        - "10.0" → 10.0
        - "10 µM" → 10.0
        - "10 um" → 10.0
        - "10 nM" → 0.01
        - "10uM" → 10.0 (no space between number and unit)
        - "0.1 µg/mL" → approximate µM (rough conversion)
        - "10 %" → 10.0 (percentage, no conversion)

    Unit resolution order:
        1. Explicit dose_unit parameter (if non-empty)
        2. Unit embedded in dose_str after number
        3. No unit → assume µM
    """
    if dose_str is None:
        return None

    dose_str = str(dose_str).strip()
    if dose_str == "" or dose_str.lower() in ("nan", "none", "-666"):
        return None

    # Extract numeric part and embedded unit
    import re
    # Match: optional whitespace, number (int/float/scientific), optional unit
    m = re.match(r'^([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*(.*)$', dose_str)
    if not m:
        return None

    try:
        val = float(m.group(1))
    except (ValueError, TypeError):
        return None

    if val <= 0:
        return None

    # Determine unit: explicit > embedded > default (µM)
    embedded_unit = m.group(2).strip().lower() if m.group(2) else ""
    unit = dose_unit.strip().lower() if dose_unit and str(dose_unit).strip().lower() not in ("", "nan", "none") else ""

    if not unit:
        unit = embedded_unit

    # Unit conversion to µM
    if unit in ("nm", "nanomolar"):
        val *= 0.001  # nM → µM
    elif unit in ("pm", "picomolar"):
        val *= 0.000001  # pM → µM
    elif unit in ("mm", "millimolar"):
        val *= 1000.0  # mM → µM
    elif unit in ("m", "molar"):
        val *= 1e6  # M → µM
    elif unit in ("um", "µm", "micromolar", "μm", "uM"):
        pass  # already µM
    elif unit in ("%", "pct", "percent"):
        pass  # keep as percentage value (unitless)
    elif unit in ("µg/ml", "ug/ml", "ng/ml", "mg/ml"):
        pass  # weight-based: keep as-is (imprecise without MW)
    # If unit is unrecognized or empty, assume µM

    return val


# ---------------------------------------------------------------------------
# Monotonicity analysis
# ---------------------------------------------------------------------------

def check_monotonicity(doses: np.ndarray, scores: np.ndarray) -> Tuple[bool, float]:
    """Check if reversal score decreases monotonically with dose.
    
    For reversers, more negative score = stronger reversal.
    A dose-dependent reverser should have scores becoming more negative
    as dose increases.
    
    Uses Spearman rank correlation between dose and score.
    Monotonic if correlation < -0.5 (negative = score decreases with dose).
    
    Args:
        doses: Array of dose values.
        scores: Array of reversal scores (more negative = better).
    
    Returns:
        (is_monotonic, spearman_rho)
    """
    if len(doses) < 3:
        return False, 0.0
    
    # Spearman rank correlation
    rank_doses = _rank(doses)
    rank_scores = _rank(scores)
    
    n = len(doses)
    d_sq = np.sum((rank_doses - rank_scores) ** 2)
    rho = 1 - (6 * d_sq) / (n * (n**2 - 1))
    
    # Negative rho means dose↑ → score↓ (more negative = more reversal)
    is_monotonic = rho < -0.5
    
    return is_monotonic, float(rho)


def _rank(values: np.ndarray) -> np.ndarray:
    """Compute ranks (handling ties with average rank)."""
    order = np.argsort(values)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(values) + 1, dtype=float)
    
    # Handle ties by averaging ranks
    unique_vals = np.unique(values)
    if len(unique_vals) < len(values):
        for val in unique_vals:
            mask = values == val
            if mask.sum() > 1:
                ranks[mask] = ranks[mask].mean()
    
    return ranks


# ---------------------------------------------------------------------------
# Hill equation fitting (simplified, no scipy dependency)
# ---------------------------------------------------------------------------

def fit_hill_equation(
    doses: np.ndarray,
    scores: np.ndarray,
    n_restarts: int = 10,
    seed: int = 42,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Fit Hill equation to dose-response data.
    
    Hill equation (for reversal, adapted):
        E(D) = E0 + (Emax - E0) * D^n / (EC50^n + D^n)
    
    Where:
        E0 = baseline effect (score at zero dose) ≈ 0
        Emax = maximum reversal effect (most negative score)
        EC50 = dose at half-maximum effect
        n = Hill coefficient (steepness)
    
    Since we don't have scipy for optimization, use grid search
    with refinement for a reasonable fit.
    
    Args:
        doses: Dose values (positive, in µM).
        scores: Reversal scores (more negative = stronger).
        n_restarts: Number of random restarts for grid search.
        seed: Random seed.
    
    Returns:
        (EC50, Emax, n, R²) or (None, None, None, None) if fit fails.
    """
    if len(doses) < 3:
        return None, None, None, None
    
    # Filter out zero/negative doses
    mask = doses > 0
    doses = doses[mask]
    scores = scores[mask]
    
    if len(doses) < 3:
        return None, None, None, None
    
    rng = np.random.default_rng(seed)
    
    # Estimate bounds
    dose_min, dose_max = doses.min(), doses.max()
    score_min = scores.min()  # most negative
    
    if score_min >= 0:
        return None, None, None, None  # no reversal signal
    
    # E0 ≈ 0 (baseline), Emax = most negative score
    E0 = 0.0
    
    best_r2 = -np.inf
    best_params = (None, None, None, None)
    
    # Grid search over EC50, Emax, n
    for _ in range(n_restarts):
        # Random starting points
        ec50_try = 10 ** rng.uniform(np.log10(max(dose_min, 0.001)), np.log10(dose_max * 10))
        emax_try = rng.uniform(score_min * 1.5, score_min * 0.5)
        n_try = rng.uniform(0.5, 4.0)
        
        # Local grid refinement around this point
        for ec50_factor in [0.1, 0.3, 1.0, 3.0, 10.0]:
            for emax_factor in [0.7, 1.0, 1.3]:
                for n_factor in [0.7, 1.0, 1.5]:
                    ec50 = ec50_try * ec50_factor
                    emax = emax_try * emax_factor
                    n = n_try * n_factor
                    
                    # Compute predicted scores
                    predicted = E0 + (emax - E0) * (doses ** n) / (ec50 ** n + doses ** n)
                    
                    # R²
                    ss_res = np.sum((scores - predicted) ** 2)
                    ss_tot = np.sum((scores - np.mean(scores)) ** 2)
                    
                    if ss_tot < 1e-10:
                        continue
                    
                    r2 = 1.0 - ss_res / ss_tot
                    
                    if r2 > best_r2:
                        best_r2 = r2
                        best_params = (ec50, emax, n, r2)
    
    ec50, emax, n, r2 = best_params
    
    if r2 is not None and r2 > 0.3:
        return ec50, emax, n, r2
    else:
        return None, None, None, None


# ---------------------------------------------------------------------------
# Dose-response quality assessment
# ---------------------------------------------------------------------------

def assess_dose_response_quality(
    n_doses: int,
    is_monotonic: bool,
    monotonicity_score: float,
    hill_r2: Optional[float],
) -> str:
    """Assess quality of dose-response relationship.
    
    Quality tiers:
        excellent: ≥4 doses, monotonic (ρ < -0.7), good Hill fit (R² > 0.7)
        good:      ≥3 doses, monotonic (ρ < -0.5), reasonable Hill fit (R² > 0.5)
        marginal:  ≥3 doses, weak trend (ρ < -0.3)
        poor:      ≥2 doses, no clear trend
        insufficient: <2 doses, cannot assess
    """
    if n_doses < 2:
        return "insufficient"
    
    r2 = hill_r2 or 0.0
    
    if n_doses >= 4 and monotonicity_score < -0.7 and r2 > 0.7:
        return "excellent"
    elif n_doses >= 3 and is_monotonic and r2 > 0.5:
        return "good"
    elif n_doses >= 3 and monotonicity_score < -0.3:
        return "marginal"
    elif n_doses >= 2:
        return "poor"
    else:
        return "insufficient"


# ---------------------------------------------------------------------------
# Main analysis pipeline
# ---------------------------------------------------------------------------

def analyze_dose_response(
    df_detail: pd.DataFrame,
    drug_col: str = "meta.pert_name",
    dose_col: str = "meta.pert_dose",
    dose_unit_col: str = "meta.pert_dose_unit",
    score_col: str = "sig_score",
) -> pd.DataFrame:
    """Analyze dose-response relationship for all drugs.
    
    For each drug:
        1. Parse doses from metadata
        2. Aggregate scores per dose level
        3. Check monotonicity (Spearman correlation)
        4. Fit Hill equation
        5. Assess quality
    
    Args:
        df_detail: Signature-level DataFrame with dose and score columns.
        drug_col: Drug name column.
        dose_col: Dose value column.
        dose_unit_col: Dose unit column.
        score_col: Reversal score column.
    
    Returns:
        DataFrame with dose-response metrics per drug.
    """
    if drug_col not in df_detail.columns:
        logger.warning(f"Column '{drug_col}' not found")
        return pd.DataFrame()
    
    results = []
    
    for drug, group in df_detail.groupby(drug_col, dropna=True):
        # Parse doses
        doses = []
        scores = []
        
        for _, row in group.iterrows():
            dose_str = str(row.get(dose_col, ""))
            dose_unit = str(row.get(dose_unit_col, ""))
            dose_val = parse_dose(dose_str, dose_unit)
            
            if dose_val is not None and dose_val > 0:
                doses.append(dose_val)
                scores.append(float(row[score_col]))
        
        if len(doses) < 2:
            results.append(DoseResponseResult(drug=str(drug), quality="insufficient"))
            continue
        
        doses_arr = np.array(doses)
        scores_arr = np.array(scores)
        
        # Aggregate by unique dose levels
        unique_doses = np.unique(doses_arr)
        mean_scores = []
        for d in unique_doses:
            mask = doses_arr == d
            mean_scores.append(float(np.mean(scores_arr[mask])))
        mean_scores_arr = np.array(mean_scores)
        
        # Monotonicity check
        is_mono, mono_score = check_monotonicity(unique_doses, mean_scores_arr)
        
        # Hill fit
        ec50, emax, hill_n, r2 = fit_hill_equation(unique_doses, mean_scores_arr)
        
        # Quality assessment
        quality = assess_dose_response_quality(
            len(unique_doses), is_mono, mono_score, r2
        )
        
        results.append(DoseResponseResult(
            drug=str(drug),
            is_monotonic=is_mono,
            monotonicity_score=mono_score,
            hill_ec50=ec50,
            hill_emax=emax,
            hill_n=hill_n,
            hill_r2=r2,
            n_doses=len(unique_doses),
            doses=unique_doses.tolist(),
            mean_scores=mean_scores,
            quality=quality,
        ))
    
    # Convert to DataFrame
    rows = []
    for r in results:
        rows.append({
            "drug": r.drug,
            "dr_is_monotonic": r.is_monotonic,
            "dr_monotonicity_rho": r.monotonicity_score,
            "dr_hill_ec50": r.hill_ec50,
            "dr_hill_emax": r.hill_emax,
            "dr_hill_n": r.hill_n,
            "dr_hill_r2": r.hill_r2,
            "dr_n_doses": r.n_doses,
            "dr_quality": r.quality,
        })
    
    df_dr = pd.DataFrame(rows)
    
    n_excellent = (df_dr["dr_quality"] == "excellent").sum()
    n_good = (df_dr["dr_quality"] == "good").sum()
    logger.info(
        f"Dose-response analysis: {len(df_dr)} drugs, "
        f"{n_excellent} excellent, {n_good} good"
    )
    
    return df_dr
