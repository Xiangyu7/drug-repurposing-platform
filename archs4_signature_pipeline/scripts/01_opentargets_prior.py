#!/usr/bin/env python3
"""
01_opentargets_prior.py - Fetch disease-associated genes from OpenTargets

Uses OpenTargets Platform GraphQL API (free, no registration, no rate limit).
Query: disease EFO ID -> associated target genes (with scores).

Output: work/{disease}/opentargets/gene_disease_associations.tsv
  Columns: ensembl_id, gene_symbol, disease_id, disease_name, ot_score
"""
import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests
import yaml

logger = logging.getLogger("archs4.opentargets")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

OT_API = "https://api.platform.opentargets.org/api/v4/graphql"

# GraphQL query: disease -> associated targets (genes)
DISEASE_TARGETS_QUERY = """
query($efoId: String!, $size: Int!, $index: Int!) {
  disease(efoId: $efoId) {
    id
    name
    associatedTargets(page: {size: $size, index: $index}) {
      count
      rows {
        score
        target {
          id
          approvedSymbol
          approvedName
        }
      }
    }
  }
}
"""


def fetch_disease_genes(
    efo_id: str,
    min_score: float = 0.1,
    page_size: int = 500,
    max_pages: int = 20,
    endpoint: str = OT_API,
) -> pd.DataFrame:
    """
    Fetch all genes associated with a disease from OpenTargets.

    Args:
        efo_id: Disease EFO ID (e.g. "EFO_0003914")
        min_score: Minimum association score to include
        page_size: Results per page
        max_pages: Maximum pages to fetch
        endpoint: GraphQL API endpoint

    Returns:
        DataFrame with columns: ensembl_id, gene_symbol, gene_name, disease_id, disease_name, ot_score
    """
    headers = {"Content-Type": "application/json"}
    all_rows = []
    disease_name = None

    for page_idx in range(max_pages):
        payload = {
            "query": DISEASE_TARGETS_QUERY,
            "variables": {
                "efoId": efo_id,
                "size": page_size,
                "index": page_idx,
            },
        }

        for attempt in range(3):
            try:
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning("OpenTargets API retry %d for page %d: %s", attempt + 1, page_idx, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("OpenTargets API failed after 3 retries for page %d: %s", page_idx, e)
                    raise

        disease_data = (data.get("data") or {}).get("disease")
        if not disease_data:
            if page_idx == 0:
                logger.error("Disease not found in OpenTargets: %s", efo_id)
                return pd.DataFrame(columns=["ensembl_id", "gene_symbol", "gene_name",
                                             "disease_id", "disease_name", "ot_score"])
            break

        if disease_name is None:
            disease_name = disease_data.get("name", efo_id)

        assoc = disease_data.get("associatedTargets") or {}
        rows = assoc.get("rows") or []
        total_count = assoc.get("count", 0)

        if not rows:
            break

        hit_threshold = False
        for row in rows:
            score = row.get("score", 0)
            if score < min_score:
                hit_threshold = True
                break  # Don't add this row, but we've collected all above-threshold rows on this page
            target = row.get("target") or {}
            all_rows.append({
                "ensembl_id": target.get("id", ""),
                "gene_symbol": target.get("approvedSymbol", ""),
                "gene_name": target.get("approvedName", ""),
                "disease_id": efo_id,
                "disease_name": disease_name,
                "ot_score": score,
            })

        if hit_threshold:
            logger.info("Reached score threshold %.2f at page %d, stopping.", min_score, page_idx)
            break

        # Check if we got all results on this page
        if len(rows) < page_size:
            break

        logger.info("  Page %d: %d genes fetched (total so far: %d, API total: %d)",
                    page_idx, len(rows), len(all_rows), total_count)

    df = pd.DataFrame(all_rows)
    if len(df) > 0:
        df = df.drop_duplicates(subset=["ensembl_id"]).reset_index(drop=True)
        # Filter out rows without valid ensembl_id
        df = df[df["ensembl_id"].str.startswith("ENSG", na=False)]

    logger.info("OpenTargets: %s (%s) -> %d associated genes (score >= %.2f)",
                efo_id, disease_name, len(df), min_score)

    return df


def main():
    ap = argparse.ArgumentParser(description="Fetch disease-gene associations from OpenTargets")
    ap.add_argument("--config", required=True, help="Config YAML path")
    ap.add_argument("--workdir", default="work", help="Work directory")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    disease_name = cfg["disease"]["name"]
    efo_id = cfg["disease"]["efo_id"]
    min_score = cfg["opentargets"].get("min_association_score", 0.1)

    workdir = Path(args.workdir)
    out_dir = workdir / "opentargets"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check cache
    out_file = out_dir / "gene_disease_associations.tsv"
    if out_file.exists():
        existing = pd.read_csv(out_file, sep="\t")
        if len(existing) > 0:
            logger.info("Using cached OpenTargets results: %d genes from %s", len(existing), out_file)
            return

    # Fetch from API
    logger.info("Querying OpenTargets for disease: %s (%s)", disease_name, efo_id)

    # Handle multiple EFO IDs (comma-separated)
    efo_ids = [eid.strip() for eid in efo_id.split(",")]
    all_dfs = []
    for eid in efo_ids:
        df = fetch_disease_genes(eid, min_score=min_score)
        all_dfs.append(df)

    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        # Deduplicate by gene, keep highest score
        combined = combined.sort_values("ot_score", ascending=False).drop_duplicates(
            subset=["ensembl_id"], keep="first"
        ).reset_index(drop=True)
    else:
        combined = pd.DataFrame(columns=["ensembl_id", "gene_symbol", "gene_name",
                                         "disease_id", "disease_name", "ot_score"])

    # Save
    combined.to_csv(out_file, sep="\t", index=False)
    logger.info("Saved %d gene-disease associations to %s", len(combined), out_file)

    # Summary stats
    if len(combined) > 0:
        logger.info("  Score range: %.3f - %.3f", combined["ot_score"].min(), combined["ot_score"].max())
        logger.info("  Median score: %.3f", combined["ot_score"].median())
        logger.info("  Genes with score >= 0.5: %d", (combined["ot_score"] >= 0.5).sum())
        logger.info("  Genes with score >= 0.3: %d", (combined["ot_score"] >= 0.3).sum())
    else:
        logger.warning("No genes found for %s. Check EFO ID.", efo_id)


if __name__ == "__main__":
    main()
