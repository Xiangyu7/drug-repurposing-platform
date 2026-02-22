"""Bridge script: Fuse SigReverse + KG_Explain results.

Reads:
    - SigReverse drug_reversal_rank.csv (per disease)
    - KG_Explain V5 drug_disease_rank.csv (all diseases)

Produces:
    - fusion_rank.csv: Combined ranking with both evidence streams
    - drug_name_mapping.csv: How drug names were matched between projects

Drug name matching strategy:
    1. Exact match (case-insensitive)
    2. Component match: combo drugs (e.g. "aspirin + ticagrelor" → match "aspirin", "ticagrelor")
    3. Salt/formulation stripping: "fasudil hydrochloride" → "fasudil"
    4. Brand→generic: "jardiance" → "empagliflozin" (manual table)

Usage:
    python scripts/run_fusion_with_kg.py \
        --sig-output data/output_v41_atherosclerosis \
        --kg-output /path/to/kg_explain/output/drug_disease_rank.csv \
        --disease atherosclerosis \
        --out data/output_v41_atherosclerosis/fusion
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sigreverse.fusion import (
    FusionRanker, SignatureEvidence, KGExplainEvidence, SafetyEvidence,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sigreverse.fusion_bridge")


# ---------------------------------------------------------------------------
# Drug name normalization & matching
# ---------------------------------------------------------------------------

# Known brand → generic mappings for atherosclerosis drugs
BRAND_TO_GENERIC = {
    "jardiance": "empagliflozin",
    "repatha": "evolocumab",
    "nexlizet": "bempedoic acid",
    "trilipix": "fenofibric acid",
    "remodulin": "treprostinil",
    "cellgram-cli": "mesenchymal stem cells",  # biological, no LINCS match expected
}

# Salt/formulation suffixes to strip
SALT_SUFFIXES = [
    " hydrochloride", " hcl", " sulfate", " sodium", " potassium",
    " mesylate", " maleate", " fumarate", " tartrate", " acetate",
    " phosphate", " citrate", " liposome", " medoxomil",
]


def normalize_drug_name(name: str) -> str:
    """Normalize drug name for matching."""
    name = name.strip().lower()
    # Brand → generic
    if name in BRAND_TO_GENERIC:
        name = BRAND_TO_GENERIC[name]
    # Strip salt/formulation suffixes
    for suffix in SALT_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    # Strip parenthetical codes: "canakinumab (acz885)" → "canakinumab"
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name)
    return name.strip()


def split_combo_drug(name: str) -> List[str]:
    """Split combo drugs on '+' and '/'."""
    # "aspirin + ticagrelor" → ["aspirin", "ticagrelor"]
    # "niacin/laropiprant" → ["niacin", "laropiprant"]
    parts = re.split(r'\s*[+/]\s*', name)
    return [p.strip() for p in parts if p.strip()]


def build_drug_name_map(
    kg_drugs: List[str],
    sig_drugs: Set[str],
) -> Dict[str, str]:
    """Build KG drug name → SigReverse drug name mapping.

    Returns dict: {kg_drug_normalized: sig_drug_original}
    """
    sig_drugs_lower = {d.lower(): d for d in sig_drugs}
    # Also build hyphen↔space variants for matching
    sig_drugs_variants = dict(sig_drugs_lower)
    for key, val in list(sig_drugs_lower.items()):
        sig_drugs_variants[key.replace("-", " ")] = val
        sig_drugs_variants[key.replace(" ", "-")] = val

    mapping = {}

    def _try_match(name: str) -> Optional[str]:
        """Try matching a name against SigReverse drug set."""
        name = name.lower().strip()
        if name in sig_drugs_variants:
            return sig_drugs_variants[name]
        return None

    for kg_drug in kg_drugs:
        # 1. Exact match (with hyphen/space equivalence)
        m = _try_match(kg_drug)
        if m:
            mapping[kg_drug] = m
            continue

        # 2. Normalized match (strip salts, brand→generic)
        kg_norm = normalize_drug_name(kg_drug)
        m = _try_match(kg_norm)
        if m:
            mapping[kg_drug] = m
            continue

        # 3. Component match for combo drugs
        components = split_combo_drug(kg_drug.lower())
        if len(components) > 1:
            for comp in components:
                comp_norm = normalize_drug_name(comp)
                m = _try_match(comp_norm) or _try_match(comp)
                if m:
                    mapping[kg_drug] = m
                    break
            if kg_drug in mapping:
                continue

        # 4. Single-component normalized
        if len(components) == 1:
            comp_norm = normalize_drug_name(components[0])
            m = _try_match(comp_norm)
            if m:
                mapping[kg_drug] = m
                continue

        # No match found
        logger.debug(f"No SigReverse match for KG drug: {kg_drug}")

    return mapping


# ---------------------------------------------------------------------------
# Disease name matching
# ---------------------------------------------------------------------------

DISEASE_KEYWORDS = {
    "atherosclerosis": [
        "atherosclerosis", "coronary artery disease",
        "cardiovascular disease", "coronary atherosclerosis",
        "cerebral atherosclerosis",
    ],
    "breast_cancer_er": [
        "breast cancer", "breast carcinoma", "breast neoplasm",
        "er-positive breast", "estrogen receptor",
    ],
    "type2_diabetes": [
        "type 2 diabetes", "diabetes mellitus", "type ii diabetes",
        "non-insulin-dependent diabetes",
    ],
    "ulcerative_colitis": [
        "ulcerative colitis", "inflammatory bowel disease", "colitis",
    ],
}


def filter_kg_for_disease(df_kg: pd.DataFrame, disease: str) -> pd.DataFrame:
    """Filter KG results to disease-relevant rows."""
    keywords = DISEASE_KEYWORDS.get(disease, [disease])
    pattern = "|".join(re.escape(k) for k in keywords)
    mask = df_kg["diseaseName"].str.lower().str.contains(pattern, na=False)
    filtered = df_kg[mask].copy()
    logger.info(f"KG disease filter '{disease}': {len(filtered)}/{len(df_kg)} rows, "
                f"{filtered['drug_normalized'].nunique()} drugs")
    return filtered


# ---------------------------------------------------------------------------
# Main fusion
# ---------------------------------------------------------------------------

def run_fusion(
    sig_output_dir: str,
    kg_csv_path: str,
    disease: str,
    out_dir: str,
    weights: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """Run fusion ranking between SigReverse and KG_Explain.

    Args:
        sig_output_dir: Path to SigReverse output directory (must contain drug_reversal_rank.csv)
        kg_csv_path: Path to KG_Explain V5 output CSV
        disease: Disease identifier (for filtering KG results)
        out_dir: Output directory for fusion results
        weights: Optional fusion weight overrides
    """
    # --- Load SigReverse results ---
    sig_rank_path = os.path.join(sig_output_dir, "drug_reversal_rank.csv")
    if not os.path.exists(sig_rank_path):
        raise FileNotFoundError(f"SigReverse output not found: {sig_rank_path}")

    df_sig_all = pd.read_csv(sig_rank_path)
    # Filter to ok drugs for primary evidence
    if "status" in df_sig_all.columns:
        df_sig = df_sig_all[df_sig_all["status"] == "ok"].copy()
    else:
        df_sig = df_sig_all.copy()
    logger.info(f"SigReverse: {len(df_sig)} ok drugs (out of {len(df_sig_all)} total)")

    # --- Load KG_Explain results ---
    if not os.path.exists(kg_csv_path):
        raise FileNotFoundError(f"KG_Explain output not found: {kg_csv_path}")

    df_kg_all = pd.read_csv(kg_csv_path)
    df_kg = filter_kg_for_disease(df_kg_all, disease)

    if df_kg.empty:
        logger.warning(f"No KG results for disease '{disease}'. Running SigReverse-only ranking.")

    # Aggregate KG to drug level: max final_score per drug
    df_kg_drug = (
        df_kg.groupby("drug_normalized", as_index=False)
        .agg(
            final_score=("final_score", "max"),
            mechanism_score=("mechanism_score", "max"),
            best_disease=("diseaseName", "first"),
            n_kg_diseases=("diseaseName", "nunique"),
        )
        .rename(columns={"drug_normalized": "drug"})
    )
    logger.info(f"KG_Explain: {len(df_kg_drug)} drugs (disease-level aggregated)")

    # --- Drug name matching ---
    # Match against ALL SigReverse drugs (including no_reverser) for completeness
    all_sig_drug_set = set(df_sig_all["drug"].values)
    ok_sig_drug_set = set(df_sig["drug"].values)
    kg_drug_list = df_kg_drug["drug"].tolist()
    name_map = build_drug_name_map(kg_drug_list, all_sig_drug_set)

    n_ok_match = sum(1 for v in name_map.values() if v.lower() in {d.lower() for d in ok_sig_drug_set})
    n_norev_match = len(name_map) - n_ok_match
    logger.info(
        f"Drug name matching: {len(name_map)}/{len(kg_drug_list)} KG drugs matched "
        f"({n_ok_match} ok + {n_norev_match} no_reverser)"
    )

    # Map KG drug names to SigReverse names
    df_kg_drug["sig_drug_name"] = df_kg_drug["drug"].map(name_map)
    df_kg_matched = df_kg_drug.dropna(subset=["sig_drug_name"]).copy()

    # Build the KG evidence DataFrame with matched names
    df_kg_for_fusion = df_kg_matched[["sig_drug_name", "final_score"]].rename(
        columns={"sig_drug_name": "drug"}
    )

    # --- Load dose-response data if available ---
    df_dr = None
    dr_cols = ["drug", "dr_quality", "dr_hill_ec50", "dr_hill_r2", "dr_is_monotonic"]
    if "dr_quality" in df_sig.columns:
        available_dr_cols = [c for c in dr_cols if c in df_sig.columns]
        df_dr = df_sig[available_dr_cols].copy()

    # --- Run Fusion ---
    fusion_weights = weights or {
        "signature": 0.50,
        "kg": 0.30,
        "safety": 0.10,
        "dose_response": 0.05,
        "literature": 0.05,
    }

    ranker = FusionRanker(weights=fusion_weights, normalization="rank")

    # Add SigReverse evidence
    sig_evidence = SignatureEvidence(df_sig, score_col="final_reversal_score")
    ranker.add_evidence(sig_evidence)

    # Add KG evidence
    kg_evidence = KGExplainEvidence(df_kg_for_fusion, score_col="final_score")
    ranker.add_evidence(kg_evidence)

    # Add dose-response bonus
    if df_dr is not None:
        ranker.set_dose_response(df_dr)

    # Execute fusion
    results = ranker.fuse()
    df_fusion = ranker.to_dataframe()

    if df_fusion.empty:
        logger.warning("Fusion produced no results!")
        return df_fusion

    # --- Enrich with source data ---
    # Merge SigReverse columns
    sig_merge_cols = ["drug", "final_reversal_score", "p_reverser", "n_signatures_fdr_pass",
                      "confidence_tier", "tau", "ci_excludes_zero", "dr_quality"]
    sig_merge_cols = [c for c in sig_merge_cols if c in df_sig.columns]
    df_fusion = df_fusion.merge(
        df_sig[sig_merge_cols], on="drug", how="left", suffixes=("", "_sig")
    )

    # Merge KG columns (via name mapping)
    reverse_map = {v: k for k, v in name_map.items()}
    df_fusion["kg_drug_name"] = df_fusion["drug"].map(reverse_map)
    kg_merge_cols = ["drug", "final_score", "mechanism_score", "best_disease", "n_kg_diseases"]
    df_fusion = df_fusion.merge(
        df_kg_drug[kg_merge_cols].rename(columns={"drug": "kg_drug_name", "final_score": "kg_final_score"}),
        on="kg_drug_name", how="left",
    )

    # Sort by fusion score
    df_fusion = df_fusion.sort_values("fusion_score").reset_index(drop=True)
    df_fusion["rank"] = range(1, len(df_fusion) + 1)

    # --- Save outputs ---
    os.makedirs(out_dir, exist_ok=True)

    # Main fusion ranking
    out_fusion = os.path.join(out_dir, "fusion_rank.csv")
    df_fusion.to_csv(out_fusion, index=False)
    logger.info(f"Wrote: {out_fusion}")

    # Drug name mapping
    mapping_rows = []
    for kg_name, sig_name in sorted(name_map.items()):
        mapping_rows.append({
            "kg_drug": kg_name,
            "sig_drug": sig_name,
            "match_type": "exact" if kg_name.lower() == sig_name.lower()
                          else "normalized",
        })
    for kg_name in kg_drug_list:
        if kg_name not in name_map:
            mapping_rows.append({
                "kg_drug": kg_name,
                "sig_drug": "",
                "match_type": "no_match",
            })
    df_mapping = pd.DataFrame(mapping_rows)
    out_mapping = os.path.join(out_dir, "drug_name_mapping.csv")
    df_mapping.to_csv(out_mapping, index=False)
    logger.info(f"Wrote: {out_mapping}")

    # --- Summary ---
    n_both = (df_fusion["evidence_sources"] >= 2).sum()
    n_sig_only = ((df_fusion["evidence_sources"] == 1) & df_fusion["kg_drug_name"].isna()).sum()
    n_total = len(df_fusion)

    print(f"\n{'='*70}")
    print(f"Fusion Ranking — {disease}")
    print(f"{'='*70}")
    print(f"Total drugs ranked:          {n_total}")
    print(f"  Both SigReverse + KG:      {n_both}")
    print(f"  SigReverse only:           {n_sig_only}")
    print(f"  High confidence:           {(df_fusion['confidence'] == 'high').sum()}")
    print(f"  Medium confidence:         {(df_fusion['confidence'] == 'medium').sum()}")
    print(f"")
    print(f"Top 20 fused drugs:")
    top20 = df_fusion.head(20)[["rank", "drug", "fusion_score", "evidence_sources",
                                 "confidence", "final_reversal_score", "kg_final_score"]].copy()
    top20["final_reversal_score"] = top20["final_reversal_score"].round(4)
    top20["kg_final_score"] = top20["kg_final_score"].round(4)
    top20["fusion_score"] = top20["fusion_score"].round(4)
    print(top20.to_string(index=False))
    print(f"{'='*70}")
    print(f"Output: {out_dir}")
    print(f"{'='*70}\n")

    return df_fusion


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fuse SigReverse + KG_Explain results")
    parser.add_argument("--sig-output", required=True,
                        help="SigReverse output directory (contains drug_reversal_rank.csv)")
    parser.add_argument("--kg-output", required=True,
                        help="KG_Explain V5 CSV (drug_disease_rank.csv)")
    parser.add_argument("--disease", required=True,
                        help="Disease name for KG filtering (atherosclerosis, breast_cancer_er, etc)")
    parser.add_argument("--out", required=True,
                        help="Output directory for fusion results")
    parser.add_argument("--w-sig", type=float, default=0.50, help="Weight for SigReverse")
    parser.add_argument("--w-kg", type=float, default=0.30, help="Weight for KG_Explain")
    parser.add_argument("--w-safety", type=float, default=0.10, help="Weight for FAERS safety")
    parser.add_argument("--w-dr", type=float, default=0.05, help="Weight for dose-response")
    parser.add_argument("--w-lit", type=float, default=0.05, help="Weight for literature")

    args = parser.parse_args()

    weights = {
        "signature": args.w_sig,
        "kg": args.w_kg,
        "safety": args.w_safety,
        "dose_response": args.w_dr,
        "literature": args.w_lit,
    }

    run_fusion(
        sig_output_dir=args.sig_output,
        kg_csv_path=args.kg_output,
        disease=args.disease,
        out_dir=args.out,
        weights=weights,
    )


if __name__ == "__main__":
    main()
