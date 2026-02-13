"""Data leakage audit for train/test validation splits.

Reports drug overlap, disease overlap, and exact pair overlap between
train and test sets. Essential for due diligence documentation.

Usage:
    report = generate_leakage_report(train_df, test_df, "temporal_2020")
    if not report["passed"]:
        print("LEAKAGE DETECTED:", report["pair_overlap"]["overlap_count"])
    save_leakage_report(report, Path("output/leakage_audit.json"))
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

logger = logging.getLogger(__name__)


def audit_drug_overlap(train_df: pd.DataFrame, test_df: pd.DataFrame) -> Dict[str, Any]:
    """Check drug overlap between train and test sets.

    Returns:
        {
            "train_count": int,
            "test_count": int,
            "overlap_count": int,
            "overlap_ratio": float (overlap / test),
            "overlap_drugs": List[str],
            "test_only_drugs": List[str],
        }
    """
    train_drugs = set(train_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())
    test_drugs = set(test_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())
    overlap = train_drugs & test_drugs
    test_only = test_drugs - train_drugs
    overlap_ratio = len(overlap) / len(test_drugs) if test_drugs else 0.0

    return {
        "train_count": len(train_drugs),
        "test_count": len(test_drugs),
        "overlap_count": len(overlap),
        "overlap_ratio": round(overlap_ratio, 4),
        "overlap_drugs": sorted(overlap),
        "test_only_drugs": sorted(test_only),
    }


def audit_disease_overlap(train_df: pd.DataFrame, test_df: pd.DataFrame) -> Dict[str, Any]:
    """Check disease overlap between train and test sets."""
    train_diseases = set(train_df["diseaseId"].dropna().astype(str).str.strip())
    test_diseases = set(test_df["diseaseId"].dropna().astype(str).str.strip())
    overlap = train_diseases & test_diseases
    test_only = test_diseases - train_diseases
    overlap_ratio = len(overlap) / len(test_diseases) if test_diseases else 0.0

    return {
        "train_count": len(train_diseases),
        "test_count": len(test_diseases),
        "overlap_count": len(overlap),
        "overlap_ratio": round(overlap_ratio, 4),
        "overlap_diseases": sorted(overlap),
        "test_only_diseases": sorted(test_only),
    }


def audit_pair_overlap(train_df: pd.DataFrame, test_df: pd.DataFrame) -> Dict[str, Any]:
    """Check exact (drug, disease) pair overlap — this is true leakage."""
    train_pairs = set(
        zip(
            train_df["drug_normalized"].dropna().astype(str).str.lower().str.strip(),
            train_df["diseaseId"].dropna().astype(str).str.strip(),
        )
    )
    test_pairs = set(
        zip(
            test_df["drug_normalized"].dropna().astype(str).str.lower().str.strip(),
            test_df["diseaseId"].dropna().astype(str).str.strip(),
        )
    )
    overlap = train_pairs & test_pairs

    return {
        "train_count": len(train_pairs),
        "test_count": len(test_pairs),
        "overlap_count": len(overlap),
        "overlap_pairs": [{"drug": d, "disease": dis} for d, dis in sorted(overlap)],
        "clean": len(overlap) == 0,
    }


def generate_leakage_report(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    split_name: str = "temporal",
) -> Dict[str, Any]:
    """Generate a comprehensive leakage audit report.

    Args:
        train_df: Training set (must have drug_normalized, diseaseId)
        test_df: Test set (must have drug_normalized, diseaseId)
        split_name: Name of the split (for report metadata)

    Returns:
        Structured report dict with passed/failed status and all overlaps.
    """
    drug_audit = audit_drug_overlap(train_df, test_df)
    disease_audit = audit_disease_overlap(train_df, test_df)
    pair_audit = audit_pair_overlap(train_df, test_df)

    # Passed = no exact pair leakage (drug/disease overlap is expected and OK)
    passed = pair_audit["clean"]

    # Compute seen_drug_test_fraction (for transductive vs inductive analysis)
    seen_drug_test_fraction = drug_audit["overlap_ratio"]

    recommendations = []
    if not passed:
        recommendations.append(
            f"CRITICAL: {pair_audit['overlap_count']} exact (drug, disease) pairs "
            "appear in both train and test. Remove duplicates from test set."
        )
    if seen_drug_test_fraction > 0.8:
        recommendations.append(
            f"WARNING: {seen_drug_test_fraction:.0%} of test drugs were seen in training. "
            "Consider reporting seen/unseen drug performance separately."
        )
    if disease_audit["overlap_ratio"] > 0.9:
        recommendations.append(
            "INFO: High disease overlap. Consider cross-disease holdout validation."
        )
    if not recommendations:
        recommendations.append("No leakage issues detected. Split is clean.")

    report = {
        "split_name": split_name,
        "passed": passed,
        "drug_overlap": drug_audit,
        "disease_overlap": disease_audit,
        "pair_overlap": pair_audit,
        "seen_drug_test_fraction": round(seen_drug_test_fraction, 4),
        "recommendations": recommendations,
    }

    logger.info(
        "Leakage audit [%s]: %s (pair_overlap=%d, drug_overlap=%.0f%%)",
        split_name,
        "PASSED" if passed else "FAILED",
        pair_audit["overlap_count"],
        seen_drug_test_fraction * 100,
    )

    return report


def save_leakage_report(report: Dict[str, Any], output_path: Path) -> None:
    """Save leakage report as JSON for DD documentation."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info("Leakage report saved: %s", output_path)
