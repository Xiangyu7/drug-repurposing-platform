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


def audit_target_overlap(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    edge_drug_target: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Check transductive leakage via shared drug targets.

    v3: A test drug sharing targets with train drugs can leak pathway/mechanism
    information.  This is transductive leakage — the KG structure itself creates
    information flow from train to test.

    Args:
        train_df: Training set (must have drug_normalized)
        test_df: Test set (must have drug_normalized)
        edge_drug_target: Drug-target edge DataFrame (drug_normalized, target_chembl_id)

    Returns:
        Dict with overlap statistics and flagged test drugs
    """
    if edge_drug_target is None or edge_drug_target.empty:
        return {
            "available": False,
            "note": "edge_drug_target not provided, skipping target overlap audit",
        }

    dt = edge_drug_target.copy()
    dt["drug_normalized"] = dt["drug_normalized"].astype(str).str.lower().str.strip()
    dt["target_chembl_id"] = dt["target_chembl_id"].astype(str).str.strip()

    train_drugs = set(train_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())
    test_drugs = set(test_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())

    # Targets used by train drugs
    train_targets = set(
        dt[dt["drug_normalized"].isin(train_drugs)]["target_chembl_id"].dropna()
    )
    # Targets used by test drugs
    test_targets = set(
        dt[dt["drug_normalized"].isin(test_drugs)]["target_chembl_id"].dropna()
    )

    shared_targets = train_targets & test_targets
    target_overlap_ratio = len(shared_targets) / len(test_targets) if test_targets else 0.0

    # Flag test drugs whose ALL targets appear in train (high leakage risk)
    flagged_test_drugs = []
    for drug in test_drugs:
        drug_targets = set(dt[dt["drug_normalized"] == drug]["target_chembl_id"].dropna())
        if drug_targets and drug_targets.issubset(train_targets):
            flagged_test_drugs.append(drug)

    return {
        "available": True,
        "train_targets": len(train_targets),
        "test_targets": len(test_targets),
        "shared_targets": len(shared_targets),
        "target_overlap_ratio": round(target_overlap_ratio, 4),
        "flagged_test_drugs": sorted(flagged_test_drugs),
        "flagged_count": len(flagged_test_drugs),
        "flagged_ratio": round(
            len(flagged_test_drugs) / len(test_drugs), 4
        ) if test_drugs else 0.0,
    }


def audit_pathway_overlap(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    edge_drug_target: Optional[pd.DataFrame] = None,
    edge_target_pathway: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Check transductive leakage via shared pathways.

    Even stricter than target overlap: test drugs whose mechanism pathways
    are entirely covered by train drug pathways receive pathway-disease
    scores derived from training data.

    Args:
        train_df, test_df: Train/test DataFrames
        edge_drug_target: drug_normalized → target_chembl_id
        edge_target_pathway: target_chembl_id → reactome_stid
    """
    if (edge_drug_target is None or edge_drug_target.empty
            or edge_target_pathway is None or edge_target_pathway.empty):
        return {
            "available": False,
            "note": "edge data not provided, skipping pathway overlap audit",
        }

    dt = edge_drug_target.copy()
    dt["drug_normalized"] = dt["drug_normalized"].astype(str).str.lower().str.strip()
    tp = edge_target_pathway.copy()

    # Drug → pathways (via targets)
    drug_pathways = dt.merge(tp, on="target_chembl_id", how="inner")

    train_drugs = set(train_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())
    test_drugs = set(test_df["drug_normalized"].dropna().astype(str).str.lower().str.strip())

    train_pathways = set(
        drug_pathways[drug_pathways["drug_normalized"].isin(train_drugs)]["reactome_stid"].dropna()
    )
    test_pathways = set(
        drug_pathways[drug_pathways["drug_normalized"].isin(test_drugs)]["reactome_stid"].dropna()
    )

    shared = train_pathways & test_pathways
    overlap_ratio = len(shared) / len(test_pathways) if test_pathways else 0.0

    return {
        "available": True,
        "train_pathways": len(train_pathways),
        "test_pathways": len(test_pathways),
        "shared_pathways": len(shared),
        "pathway_overlap_ratio": round(overlap_ratio, 4),
    }


def generate_leakage_report(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    split_name: str = "temporal",
    edge_drug_target: Optional[pd.DataFrame] = None,
    edge_target_pathway: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Generate a comprehensive leakage audit report.

    v3: Added transductive leakage audits (target and pathway overlap).

    Args:
        train_df: Training set (must have drug_normalized, diseaseId)
        test_df: Test set (must have drug_normalized, diseaseId)
        split_name: Name of the split (for report metadata)
        edge_drug_target: Optional drug-target DataFrame for transductive audit
        edge_target_pathway: Optional target-pathway DataFrame for transductive audit

    Returns:
        Structured report dict with passed/failed status and all overlaps.
    """
    drug_audit = audit_drug_overlap(train_df, test_df)
    disease_audit = audit_disease_overlap(train_df, test_df)
    pair_audit = audit_pair_overlap(train_df, test_df)
    target_audit = audit_target_overlap(train_df, test_df, edge_drug_target)
    pathway_audit = audit_pathway_overlap(
        train_df, test_df, edge_drug_target, edge_target_pathway,
    )

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
    # v3: Transductive leakage warnings
    if target_audit.get("available") and target_audit.get("flagged_ratio", 0) > 0.5:
        recommendations.append(
            f"WARNING: {target_audit['flagged_ratio']:.0%} of test drugs share ALL targets "
            "with training drugs (transductive leakage). Report seen-target vs unseen-target "
            "performance separately."
        )
    if pathway_audit.get("available") and pathway_audit.get("pathway_overlap_ratio", 0) > 0.9:
        recommendations.append(
            f"WARNING: {pathway_audit['pathway_overlap_ratio']:.0%} pathway overlap between "
            "train and test. KG pathway-disease scores may leak training information."
        )
    if not recommendations:
        recommendations.append("No leakage issues detected. Split is clean.")

    report = {
        "split_name": split_name,
        "passed": passed,
        "drug_overlap": drug_audit,
        "disease_overlap": disease_audit,
        "pair_overlap": pair_audit,
        "target_overlap": target_audit,
        "pathway_overlap": pathway_audit,
        "seen_drug_test_fraction": round(seen_drug_test_fraction, 4),
        "recommendations": recommendations,
    }

    logger.info(
        "Leakage audit [%s]: %s (pair_overlap=%d, drug_overlap=%.0f%%, target_flagged=%d)",
        split_name,
        "PASSED" if passed else "FAILED",
        pair_audit["overlap_count"],
        seen_drug_test_fraction * 100,
        target_audit.get("flagged_count", 0),
    )

    return report


def save_leakage_report(report: Dict[str, Any], output_path: Path) -> None:
    """Save leakage report as JSON for DD documentation."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info("Leakage report saved: %s", output_path)
