#!/usr/bin/env python3
"""Compare A-route and B-route drug rankings for cross-validation.

A-route (Cross): ARCHS4 signature → sigreverse → KG → bridge_A
B-route (Origin): CT.gov → KG → bridge_B

Drugs found in BOTH routes = highest confidence candidates.

Usage:
    python ops/compare_ab_routes.py \
        --bridge-a kg_explain/output/atherosclerosis/bridge_repurpose_cross.csv \
        --bridge-b kg_explain/output/atherosclerosis/bridge_origin_reassess.csv \
        --out runtime/results/atherosclerosis/ab_comparison.csv
"""
import argparse
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def compare_routes(bridge_a_path: str, bridge_b_path: str) -> pd.DataFrame:
    """Compare two bridge CSVs and produce a merged ranking."""

    a_exists = Path(bridge_a_path).exists()
    b_exists = Path(bridge_b_path).exists()

    if not a_exists and not b_exists:
        logger.error("Neither bridge file exists")
        return pd.DataFrame()

    # Load available bridges
    if a_exists:
        a = pd.read_csv(bridge_a_path)
        a = a.rename(columns={
            "max_mechanism_score": "score_a",
            "final_score": "final_a",
            "confidence_tier": "tier_a",
            "n_evidence_paths": "n_paths_a",
        })
        a["drug_key"] = a["canonical_name"].str.lower().str.strip()
        a_drugs = set(a["drug_key"])
        logger.info("Route A (cross): %d drugs", len(a))
    else:
        a = pd.DataFrame()
        a_drugs = set()
        logger.warning("Route A bridge not found: %s", bridge_a_path)

    if b_exists:
        b = pd.read_csv(bridge_b_path)
        b = b.rename(columns={
            "max_mechanism_score": "score_b",
            "final_score": "final_b",
            "confidence_tier": "tier_b",
            "n_evidence_paths": "n_paths_b",
        })
        b["drug_key"] = b["canonical_name"].str.lower().str.strip()
        b_drugs = set(b["drug_key"])
        logger.info("Route B (origin): %d drugs", len(b))
    else:
        b = pd.DataFrame()
        b_drugs = set()
        logger.warning("Route B bridge not found: %s", bridge_b_path)

    # Find overlap
    overlap = a_drugs & b_drugs
    a_only = a_drugs - b_drugs
    b_only = b_drugs - a_drugs

    logger.info("Overlap (A ∩ B): %d drugs", len(overlap))
    logger.info("A only: %d drugs", len(a_only))
    logger.info("B only: %d drugs", len(b_only))

    # Build merged DataFrame
    rows = []

    # Drugs in both routes
    for drug_key in sorted(overlap):
        a_row = a[a["drug_key"] == drug_key].iloc[0] if len(a) > 0 else {}
        b_row = b[b["drug_key"] == drug_key].iloc[0] if len(b) > 0 else {}

        rows.append({
            "canonical_name": a_row.get("canonical_name", b_row.get("canonical_name", drug_key)),
            "chembl_pref_name": a_row.get("chembl_pref_name", b_row.get("chembl_pref_name", "")),
            "route": "A+B",
            "score_a": a_row.get("score_a", None),
            "score_b": b_row.get("score_b", None),
            "final_a": a_row.get("final_a", None),
            "final_b": b_row.get("final_b", None),
            "tier_a": a_row.get("tier_a", ""),
            "tier_b": b_row.get("tier_b", ""),
            "n_paths_a": a_row.get("n_paths_a", 0),
            "n_paths_b": b_row.get("n_paths_b", 0),
            "n_evidence_lines": 2,  # Found in both independent routes
            "source_a": a_row.get("source", ""),
            "source_b": b_row.get("source", ""),
        })

    # A-only drugs
    if len(a) > 0:
        for _, a_row in a[a["drug_key"].isin(a_only)].iterrows():
            rows.append({
                "canonical_name": a_row["canonical_name"],
                "chembl_pref_name": a_row.get("chembl_pref_name", ""),
                "route": "A_only",
                "score_a": a_row.get("score_a", None),
                "score_b": None,
                "final_a": a_row.get("final_a", None),
                "final_b": None,
                "tier_a": a_row.get("tier_a", ""),
                "tier_b": "",
                "n_paths_a": a_row.get("n_paths_a", 0),
                "n_paths_b": 0,
                "n_evidence_lines": 1,
                "source_a": a_row.get("source", ""),
                "source_b": "",
            })

    # B-only drugs
    if len(b) > 0:
        for _, b_row in b[b["drug_key"].isin(b_only)].iterrows():
            rows.append({
                "canonical_name": b_row["canonical_name"],
                "chembl_pref_name": b_row.get("chembl_pref_name", ""),
                "route": "B_only",
                "score_a": None,
                "score_b": b_row.get("score_b", None),
                "final_a": None,
                "final_b": b_row.get("final_b", None),
                "tier_a": "",
                "tier_b": b_row.get("tier_b", ""),
                "n_paths_a": 0,
                "n_paths_b": b_row.get("n_paths_b", 0),
                "n_evidence_lines": 1,
                "source_a": "",
                "source_b": b_row.get("source", ""),
            })

    df = pd.DataFrame(rows)

    # Sort: A+B first (highest confidence), then by max score
    if len(df) > 0:
        df["_sort_key"] = df["n_evidence_lines"] * 1000 + df[["score_a", "score_b"]].max(axis=1).fillna(0)
        df = df.sort_values("_sort_key", ascending=False).drop(columns=["_sort_key"]).reset_index(drop=True)

    return df


def main():
    ap = argparse.ArgumentParser(description="Compare A-route and B-route drug rankings")
    ap.add_argument("--bridge-a", required=True, help="A-route bridge CSV (cross/repurpose)")
    ap.add_argument("--bridge-b", required=True, help="B-route bridge CSV (origin/reassess)")
    ap.add_argument("--out", required=True, help="Output comparison CSV")
    args = ap.parse_args()

    df = compare_routes(args.bridge_a, args.bridge_b)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    logger.info("Comparison saved: %s (%d drugs)", out, len(df))

    # Summary
    if len(df) > 0:
        n_ab = len(df[df["route"] == "A+B"])
        n_a = len(df[df["route"] == "A_only"])
        n_b = len(df[df["route"] == "B_only"])
        print(f"\n{'='*60}")
        print(f"A+B Cross-Validation Summary: {len(df)} total drugs")
        print(f"  Both routes (HIGH confidence):  {n_ab}")
        print(f"  A-route only (discovery):       {n_a}")
        print(f"  B-route only (clinical):        {n_b}")
        print(f"  Output: {out}")
        print(f"{'='*60}")
        if n_ab > 0:
            print(f"\nTop cross-validated drugs (found in both A + B):")
            top = df[df["route"] == "A+B"].head(10)[
                ["canonical_name", "score_a", "score_b", "tier_a", "tier_b"]
            ]
            print(top.to_string(index=False))
    else:
        print("No drugs found in either route.")


if __name__ == "__main__":
    main()
