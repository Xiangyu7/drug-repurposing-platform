"""SIDER (Side Effect Resource) data integration.

SIDER provides drug side effects mined from FDA-approved drug labels.
Complements FAERS (voluntary reporting) with structured, label-based safety data.

Data source: http://sideeffects.embl.de/
Files needed (TSV, downloadable):
    - meddra_all_se.tsv.gz: Drug → side effect (MedDRA terms)
    - drug_names.tsv: CID → drug name mapping

Integration with KG pipeline:
    - Merged with FAERS safety signals to create a more comprehensive safety profile
    - SIDER covers known/labeled AEs; FAERS covers post-market/emerging signals
    - Combined safety score = max(FAERS_signal, SIDER_signal) per drug

Usage:
    sider_df = load_sider_safety(data_dir, drug_list=["aspirin", "metformin"])
    merged = merge_faers_sider(faers_df, sider_df)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd

from ..utils import safe_str

logger = logging.getLogger(__name__)

# MedDRA preferred terms indicating serious adverse events
_SERIOUS_SE_PREFIXES = {
    "death", "cardiac arrest", "hepatic failure", "renal failure",
    "anaphylactic", "stevens-johnson", "toxic epidermal",
    "agranulocytosis", "aplastic anaemia", "pulmonary embolism",
    "cerebrovascular accident", "myocardial infarction",
    "ventricular fibrillation", "pancreatitis",
    "rhabdomyolysis", "neuroleptic malignant",
}


def load_sider_safety(
    data_dir: Path,
    drug_list: Optional[List[str]] = None,
    se_file: str = "sider_meddra_all_se.tsv",
    names_file: str = "sider_drug_names.tsv",
) -> pd.DataFrame:
    """Load SIDER side effect data and normalize to pipeline drug names.

    Expects pre-downloaded SIDER TSV files in data_dir.
    If files don't exist, returns empty DataFrame (graceful fallback).

    Args:
        data_dir: Directory containing SIDER data files
        drug_list: Optional list of drug names to filter (lowercase)
        se_file: Side effects TSV filename
        names_file: Drug names TSV filename

    Returns:
        DataFrame with columns:
            drug_normalized, side_effect, meddra_concept_id,
            is_serious, side_effect_source
    """
    se_path = data_dir / se_file
    names_path = data_dir / names_file

    if not se_path.exists():
        logger.info("SIDER data not found at %s — skipping SIDER integration", se_path)
        return pd.DataFrame(columns=[
            "drug_normalized", "side_effect", "meddra_concept_id",
            "is_serious", "side_effect_source",
        ])

    # Load side effects
    try:
        # SIDER TSV format: CID, CID_flat, MedDRA_concept_type, UMLS_CID,
        #                    MedDRA_concept_id, side_effect_name
        se_df = pd.read_csv(se_path, sep="\t", header=None, dtype=str)
        se_df.columns = ["cid", "cid_flat", "concept_type", "umls_id",
                         "meddra_id", "side_effect"][:len(se_df.columns)]
    except Exception as e:
        logger.warning("Failed to load SIDER SE data: %s", e)
        return pd.DataFrame(columns=[
            "drug_normalized", "side_effect", "meddra_concept_id",
            "is_serious", "side_effect_source",
        ])

    # Load drug name mapping
    cid_to_name = {}
    if names_path.exists():
        try:
            names_df = pd.read_csv(names_path, sep="\t", header=None, dtype=str)
            for _, r in names_df.iterrows():
                cid = safe_str(r.iloc[0])
                name = safe_str(r.iloc[1]).lower().strip()
                if cid and name:
                    cid_to_name[cid] = name
        except Exception as e:
            logger.warning("Failed to load SIDER drug names: %s", e)

    # Map CID → drug name
    se_df["drug_normalized"] = se_df["cid"].map(cid_to_name)
    se_df = se_df.dropna(subset=["drug_normalized", "side_effect"])

    # Filter to pipeline drugs if provided
    if drug_list is not None:
        drug_set = {d.lower().strip() for d in drug_list}
        se_df = se_df[se_df["drug_normalized"].isin(drug_set)]

    # Classify serious vs non-serious
    def _is_serious_se(se_name: str) -> bool:
        se_lower = se_name.lower()
        return any(kw in se_lower for kw in _SERIOUS_SE_PREFIXES)

    se_df["is_serious"] = se_df["side_effect"].apply(_is_serious_se)
    se_df["side_effect_source"] = "SIDER"

    result = se_df[["drug_normalized", "side_effect", "meddra_id",
                     "is_serious", "side_effect_source"]].copy()
    result = result.rename(columns={"meddra_id": "meddra_concept_id"})
    result = result.drop_duplicates()

    n_drugs = result["drug_normalized"].nunique()
    n_serious = result["is_serious"].sum()
    logger.info("SIDER loaded: %d side effects for %d drugs (%d serious)",
                len(result), n_drugs, n_serious)

    return result


def merge_faers_sider(
    faers_df: pd.DataFrame,
    sider_df: pd.DataFrame,
) -> pd.DataFrame:
    """Merge FAERS and SIDER safety data for comprehensive safety profiling.

    Strategy:
    - Union all AE terms from both sources
    - For overlapping terms: keep max(PRR) from FAERS, flag as "both_sources"
    - SIDER-only terms: assign moderate PRR equivalent (2.0) since they're
      FDA-label confirmed but without population-level disproportionality

    Args:
        faers_df: FAERS DataFrame (drug_normalized, ae_term, report_count, prr)
        sider_df: SIDER DataFrame (drug_normalized, side_effect, is_serious)

    Returns:
        Merged DataFrame with unified format
    """
    if sider_df.empty:
        return faers_df

    # Standardize SIDER to FAERS-like format
    sider_ae = sider_df.rename(columns={"side_effect": "ae_term"}).copy()
    sider_ae["ae_term"] = sider_ae["ae_term"].str.lower().str.strip()
    # SIDER label-confirmed AEs: assign a moderate PRR equivalent
    # and a synthetic report_count to indicate label-confirmed status
    sider_ae["prr"] = 2.0     # Conservative: confirmed side effect
    sider_ae["report_count"] = 100  # High confidence (FDA label)
    sider_ae["source"] = "SIDER"

    if faers_df.empty:
        return sider_ae[["drug_normalized", "ae_term", "report_count",
                          "prr", "is_serious", "source"]].drop_duplicates()

    faers_ae = faers_df.copy()
    faers_ae["ae_term"] = faers_ae["ae_term"].astype(str).str.lower().str.strip()
    faers_ae["source"] = "FAERS"
    if "is_serious" not in faers_ae.columns:
        faers_ae["is_serious"] = False

    # Union with priority: FAERS PRR when available (empirical > label)
    combined = pd.concat([
        faers_ae[["drug_normalized", "ae_term", "report_count", "prr", "is_serious", "source"]],
        sider_ae[["drug_normalized", "ae_term", "report_count", "prr", "is_serious", "source"]],
    ], ignore_index=True)

    # Deduplicate: keep max PRR per (drug, ae_term), flag source
    def _agg_source(sources):
        s = set(sources)
        if len(s) > 1:
            return "FAERS+SIDER"
        return s.pop()

    merged = combined.groupby(["drug_normalized", "ae_term"], as_index=False).agg(
        report_count=("report_count", "max"),
        prr=("prr", "max"),
        is_serious=("is_serious", "max"),
        source=("source", _agg_source),
    )

    n_faers_only = (merged["source"] == "FAERS").sum()
    n_sider_only = (merged["source"] == "SIDER").sum()
    n_both = (merged["source"] == "FAERS+SIDER").sum()
    logger.info("FAERS+SIDER merged: %d total AEs (%d FAERS-only, %d SIDER-only, %d both)",
                len(merged), n_faers_only, n_sider_only, n_both)

    return merged
