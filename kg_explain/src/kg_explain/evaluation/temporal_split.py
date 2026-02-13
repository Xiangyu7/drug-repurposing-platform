"""Temporal split validation for drug repurposing.

Splits known drug-disease indications by time:
  - Train: approved/known before cutoff
  - Test: approved/known after cutoff

This tests whether the pipeline can "predict" recent approvals
using only historical data.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from .benchmark import run_benchmark

logger = logging.getLogger(__name__)


def split_by_year(
    gold_df: pd.DataFrame,
    cutoff_year: int = 2020,
    date_col: str = "approval_year",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split gold standard into train (pre-cutoff) and test (post-cutoff).

    Args:
        gold_df: Gold standard DataFrame (must have date_col and drug_normalized, diseaseId)
        cutoff_year: Year to split on (exclusive for test)
        date_col: Column containing the year

    Returns:
        (train_df, test_df) both with [drug_normalized, diseaseId]
    """
    if date_col not in gold_df.columns:
        logger.warning(
            "Column '%s' not found. Returning all as train, empty test.", date_col
        )
        return gold_df.copy(), pd.DataFrame(columns=gold_df.columns)

    gold_df = gold_df.copy()
    gold_df[date_col] = pd.to_numeric(gold_df[date_col], errors="coerce")

    train = gold_df[gold_df[date_col] < cutoff_year]
    test = gold_df[gold_df[date_col] >= cutoff_year]

    logger.info(
        "Temporal split at %d: train=%d pairs, test=%d pairs",
        cutoff_year, len(train), len(test),
    )
    return train, test


def cross_disease_holdout(
    gold_df: pd.DataFrame,
    holdout_diseases: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hold out specific diseases for validation.

    Tests cross-disease generalization: can the pipeline rank drugs
    for diseases it hasn't seen in training?

    Args:
        gold_df: Full gold standard
        holdout_diseases: EFO disease IDs to hold out

    Returns:
        (train_df, test_df)
    """
    holdout_set = set(holdout_diseases)
    test = gold_df[gold_df["diseaseId"].isin(holdout_set)]
    train = gold_df[~gold_df["diseaseId"].isin(holdout_set)]

    logger.info(
        "Cross-disease holdout: %d diseases held out. train=%d, test=%d",
        len(holdout_set), len(train), len(test),
    )
    return train, test


def run_temporal_validation(
    rank_csv: Path,
    gold_df: pd.DataFrame,
    cutoff_year: int = 2020,
    ks: list[int] | None = None,
) -> dict:
    """Run full temporal split validation.

    Evaluates ranking performance on pre-cutoff (known) and
    post-cutoff (future) drug-disease pairs.

    Args:
        rank_csv: Path to ranking output CSV
        gold_df: Gold standard with approval_year column
        cutoff_year: Year to split on
        ks: K values for Hit@K etc.

    Returns:
        {
            "cutoff_year": int,
            "train_metrics": dict,
            "test_metrics": dict,
            "gap_analysis": {metric: test_value - train_value}
        }
    """
    import tempfile

    if ks is None:
        ks = [5, 10, 20]

    train_gold, test_gold = split_by_year(gold_df, cutoff_year)

    # Write temp gold CSVs for run_benchmark
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        train_gold[["drug_normalized", "diseaseId"]].to_csv(f, index=False)
        train_path = Path(f.name)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        test_gold[["drug_normalized", "diseaseId"]].to_csv(f, index=False)
        test_path = Path(f.name)

    train_result = run_benchmark(rank_csv, train_path, ks=ks) if len(train_gold) > 0 else {}
    test_result = run_benchmark(rank_csv, test_path, ks=ks) if len(test_gold) > 0 else {}

    # Compute gap analysis
    gap: dict[str, float] = {}
    train_agg = train_result.get("aggregate", {})
    test_agg = test_result.get("aggregate", {})
    for metric in set(list(train_agg.keys()) + list(test_agg.keys())):
        train_val = train_agg.get(metric, 0.0)
        test_val = test_agg.get(metric, 0.0)
        gap[metric] = round(test_val - train_val, 6)

    # Leakage audit
    leakage_audit: dict = {}
    try:
        from .leakage_audit import generate_leakage_report
        leakage_audit = generate_leakage_report(train_gold, test_gold, f"temporal_{cutoff_year}")
    except Exception as e:
        logger.warning("Leakage audit failed: %s", e)

    # Clean up temp files
    try:
        train_path.unlink(missing_ok=True)
        test_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "cutoff_year": cutoff_year,
        "train_n_pairs": len(train_gold),
        "test_n_pairs": len(test_gold),
        "train_metrics": train_result,
        "test_metrics": test_result,
        "gap_analysis": gap,
        "leakage_audit": leakage_audit,
    }


def run_cross_disease_validation(
    rank_csv: Path,
    gold_df: pd.DataFrame,
    holdout_diseases: list[str],
    ks: list[int] | None = None,
) -> dict:
    """Run cross-disease holdout validation.

    Args:
        rank_csv: Ranking output CSV
        gold_df: Full gold standard
        holdout_diseases: EFO IDs to hold out
        ks: K values

    Returns:
        Same structure as run_temporal_validation but with disease holdout.
    """
    import tempfile

    if ks is None:
        ks = [5, 10, 20]

    train_gold, test_gold = cross_disease_holdout(gold_df, holdout_diseases)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        train_gold[["drug_normalized", "diseaseId"]].to_csv(f, index=False)
        train_path = Path(f.name)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        test_gold[["drug_normalized", "diseaseId"]].to_csv(f, index=False)
        test_path = Path(f.name)

    train_result = run_benchmark(rank_csv, train_path, ks=ks) if len(train_gold) > 0 else {}
    test_result = run_benchmark(rank_csv, test_path, ks=ks) if len(test_gold) > 0 else {}

    try:
        train_path.unlink(missing_ok=True)
        test_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "holdout_diseases": holdout_diseases,
        "train_n_pairs": len(train_gold),
        "test_n_pairs": len(test_gold),
        "train_metrics": train_result,
        "test_metrics": test_result,
    }
