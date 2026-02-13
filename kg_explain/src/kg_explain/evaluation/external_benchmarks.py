"""External benchmark datasets for drug repurposing validation.

Downloads, parses, and caches Hetionet Compound-treats-Disease
ground truth for standardized comparison against KG ranking output.

Hetionet: https://github.com/hetio/hetionet
CtD edges: Known drug-disease treatment relationships from DrugBank + other sources.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

HETIONET_CTD_URL = (
    "https://github.com/hetio/hetionet/raw/main/hetnet/tsv/edges/CtD.tsv.gz"
)


def download_hetionet_ctd(
    cache_dir: Path,
    force: bool = False,
) -> pd.DataFrame:
    """Download and parse Hetionet Compound-treats-Disease edges.

    Args:
        cache_dir: Directory to cache the downloaded file
        force: If True, re-download even if cached

    Returns:
        DataFrame with columns: [compound_id, compound_name, disease_id, disease_name]
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_path = cache_dir / "hetionet_CtD.tsv.gz"

    if not cached_path.exists() or force:
        logger.info("Downloading Hetionet CtD from %s", HETIONET_CTD_URL)
        try:
            resp = requests.get(HETIONET_CTD_URL, timeout=60)
            resp.raise_for_status()
            with open(cached_path, "wb") as f:
                f.write(resp.content)
            logger.info("Downloaded Hetionet CtD: %d bytes", len(resp.content))
        except requests.RequestException as e:
            logger.error("Failed to download Hetionet: %s", e)
            if cached_path.exists():
                logger.info("Using cached version")
            else:
                raise
    else:
        logger.info("Using cached Hetionet CtD: %s", cached_path)

    df = pd.read_csv(cached_path, sep="\t", compression="gzip", dtype=str)

    # Hetionet TSV has columns: source, metaedge, target
    # Source = "Compound::DB00945" (DrugBank ID + name)
    # Target = "Disease::DOID:14330" (DOID + name)
    result_rows = []
    for _, row in df.iterrows():
        source = str(row.get("source", ""))
        target = str(row.get("target", ""))

        # Parse compound
        if "::" in source:
            compound_parts = source.split("::", 1)
            compound_id = compound_parts[0].strip()
            compound_name = compound_parts[1].strip() if len(compound_parts) > 1 else ""
        else:
            compound_id = source
            compound_name = ""

        # Parse disease
        if "::" in target:
            disease_parts = target.split("::", 1)
            disease_id = disease_parts[0].strip()
            disease_name = disease_parts[1].strip() if len(disease_parts) > 1 else ""
        else:
            disease_id = target
            disease_name = ""

        result_rows.append({
            "compound_id": compound_id,
            "compound_name": compound_name.lower(),
            "disease_id": disease_id,
            "disease_name": disease_name,
        })

    result = pd.DataFrame(result_rows)
    logger.info("Parsed %d Compound-treats-Disease edges from Hetionet", len(result))
    return result


def map_hetionet_to_internal(
    hetionet_df: pd.DataFrame,
    mapping_path: Path,
) -> pd.DataFrame:
    """Map Hetionet IDs to internal pipeline IDs.

    Uses a DOID→EFO mapping file to convert Hetionet disease IDs
    to the EFO IDs used by the kg_explain pipeline.

    Args:
        hetionet_df: Output of download_hetionet_ctd()
        mapping_path: Path to disease_id_mapping.csv (columns: doid, efo_id, disease_name)

    Returns:
        DataFrame with columns: [drug_normalized, diseaseId, source, mapping_confidence]
    """
    if not mapping_path.exists():
        logger.warning("Disease ID mapping file not found: %s", mapping_path)
        return pd.DataFrame(columns=["drug_normalized", "diseaseId"])

    mapping = pd.read_csv(mapping_path, dtype=str)
    if "doid" not in mapping.columns or "efo_id" not in mapping.columns:
        raise ValueError("Mapping file must have 'doid' and 'efo_id' columns")

    doid_to_efo = dict(zip(mapping["doid"], mapping["efo_id"]))

    result_rows = []
    mapped_count = 0
    unmapped_count = 0

    for _, row in hetionet_df.iterrows():
        disease_id = str(row.get("disease_id", ""))
        drug_name = str(row.get("compound_name", "")).lower().strip()

        if not drug_name:
            continue

        efo_id = doid_to_efo.get(disease_id)
        if efo_id:
            result_rows.append({
                "drug_normalized": drug_name,
                "diseaseId": efo_id,
                "source": "hetionet",
                "mapping_confidence": "exact",
            })
            mapped_count += 1
        else:
            unmapped_count += 1

    logger.info(
        "Mapped %d/%d Hetionet edges to EFO IDs (%d unmapped)",
        mapped_count, mapped_count + unmapped_count, unmapped_count,
    )
    return pd.DataFrame(result_rows)


def build_external_gold(
    cache_dir: Path,
    mapping_path: Optional[Path] = None,
    disease_filter: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Build a gold-standard CSV from Hetionet for use with run_benchmark().

    Args:
        cache_dir: Where to cache downloaded data
        mapping_path: Path to DOID→EFO mapping. If None, uses default location.
        disease_filter: If provided, only include these EFO disease IDs

    Returns:
        DataFrame with [drug_normalized, diseaseId] ready for run_benchmark()
    """
    if mapping_path is None:
        mapping_path = Path(__file__).parent.parent.parent.parent / "data" / "external" / "disease_id_mapping.csv"

    hetionet_df = download_hetionet_ctd(cache_dir)
    mapped_df = map_hetionet_to_internal(hetionet_df, mapping_path)

    if disease_filter:
        mapped_df = mapped_df[mapped_df["diseaseId"].isin(disease_filter)]

    # Deduplicate
    gold = mapped_df[["drug_normalized", "diseaseId"]].drop_duplicates()
    logger.info("Built external gold standard: %d drug-disease pairs", len(gold))
    return gold
