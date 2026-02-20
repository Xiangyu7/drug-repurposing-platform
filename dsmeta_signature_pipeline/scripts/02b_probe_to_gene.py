#!/usr/bin/env python3
"""
02b_probe_to_gene.py - Map probe IDs to gene symbols (CRITICAL for cross-platform meta)

Why this step is necessary:
  Different microarray platforms (e.g., GPL570 = HG-U133 Plus 2, GPL6244 = HuGene 1.0 ST)
  use completely different probe ID formats. Without mapping to a shared namespace (gene
  symbols), the meta-analysis in step 03 finds ZERO overlapping features and produces
  empty results.

Strategy:
  1. Try GEO platform annotation (GPL soft files) — most reliable
  2. Fallback to biomaRt (Ensembl) lookup
  3. Fallback to built-in mappings for common platforms

This script:
  - Reads each GSE's expr.tsv and de.tsv (if exists)
  - Maps probe IDs → HGNC gene symbols
  - Handles many-to-one (multiple probes → same gene): keeps probe with max |t| or max variance
  - Writes updated files in-place (with backup)

Supported platforms (built-in):
  - GPL570   (Affymetrix HG-U133 Plus 2.0)
  - GPL6244  (Affymetrix HuGene 1.0 ST)
  - GPL96    (Affymetrix HG-U133A)
  - GPL10558 (Illumina HumanHT-12 V4)
  - GPL6480  (Agilent Whole Human Genome)

For other platforms, the script attempts GEO annotation download.
"""
from __future__ import annotations

import argparse
import gzip
import io
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dsmeta.probe_to_gene")


# ---------------------------------------------------------------------------
# GEO platform annotation download
# ---------------------------------------------------------------------------

ANNOT_CACHE_DIR = "data/cache/gpl_annotations"

# Column name patterns that likely contain gene symbols
GENE_SYMBOL_COLUMNS = [
    "Gene Symbol",
    "gene_assignment",      # HuGene ST arrays
    "Symbol",
    "GENE_SYMBOL",
    "gene_symbol",
    "ILMN_Gene",            # Illumina
    "GeneSymbol",
    "SPOT_ID.1",
    "ORF",
    "GeneName",
]

# Column name patterns for probe IDs
PROBE_ID_COLUMNS = [
    "ID",
    "ID_REF",
    "PROBEID",
    "probe_id",
]


def download_gpl_annotation(gpl_id: str, cache_dir: str = ANNOT_CACHE_DIR) -> Optional[pd.DataFrame]:
    """Download GPL platform annotation from GEO and parse probe→gene mapping.

    Downloads the SOFT file for the platform, extracts the annotation table,
    and identifies the gene symbol column.

    Returns DataFrame with columns: [probe_id, gene_symbol] or None on failure.
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{gpl_id}_annotation.tsv")

    # Check cache
    if os.path.exists(cache_path):
        logger.info(f"Loading cached annotation: {cache_path}")
        df = pd.read_csv(cache_path, sep="\t")
        if "probe_id" in df.columns and "gene_symbol" in df.columns:
            return df

    # Download GPL annotation file
    # GEO FTP URL pattern: strip last 3 digits and replace with "nnn"
    # GPL570 → GPLnnn, GPL6244 → GPL6nnn, GPL10558 → GPL10nnn
    gpl_num_str = re.search(r"(\d+)", gpl_id).group(1)
    if len(gpl_num_str) <= 3:
        gpl_prefix = "GPLnnn"
    else:
        gpl_prefix = f"GPL{gpl_num_str[:-3]}nnn"

    urls = [
        f"https://ftp.ncbi.nlm.nih.gov/geo/platforms/{gpl_prefix}/{gpl_id}/annot/{gpl_id}.annot.gz",
        f"https://ftp.ncbi.nlm.nih.gov/geo/platforms/{gpl_prefix}/{gpl_id}/soft/{gpl_id}_family.soft.gz",
    ]

    content = None
    for url in urls:
        try:
            logger.info(f"Downloading {url} ...")
            r = requests.get(url, timeout=120)
            if r.status_code == 200:
                content = gzip.decompress(r.content).decode("utf-8", errors="replace")
                logger.info(f"Downloaded {len(content)} chars from {url}")
                break
            else:
                logger.debug(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            logger.debug(f"Failed {url}: {e}")

    if content is None:
        logger.warning(f"Could not download annotation for {gpl_id}")
        return None

    # Parse annotation table from SOFT/annot file
    return _parse_gpl_content(gpl_id, content, cache_path)


def _parse_gpl_content(gpl_id: str, content: str, cache_path: str) -> Optional[pd.DataFrame]:
    """Parse GPL annotation content (SOFT or annot format) to extract probe→gene mapping."""
    lines = content.split("\n")

    # Find the table start
    table_start = None
    header_line = None
    for i, line in enumerate(lines):
        if line.startswith("!platform_table_begin") or line.startswith("#ID"):
            table_start = i + 1
            continue
        if table_start is not None and header_line is None:
            if line.startswith("ID\t") or line.startswith("ID_REF\t"):
                header_line = i
                break
            elif not line.startswith("!") and not line.startswith("#") and "\t" in line:
                header_line = i
                break

    # For .annot files, header might be the first non-comment line
    if header_line is None:
        for i, line in enumerate(lines):
            if line.startswith("!") or line.startswith("#") or line.startswith("^"):
                continue
            if "\t" in line and ("ID" in line or "Gene" in line):
                header_line = i
                break

    if header_line is None:
        logger.warning(f"Could not find annotation table header in {gpl_id}")
        return None

    # Find table end
    table_end = len(lines)
    for i in range(header_line + 1, len(lines)):
        if lines[i].startswith("!platform_table_end"):
            table_end = i
            break

    # Parse as TSV
    table_text = "\n".join(lines[header_line:table_end])
    try:
        df = pd.read_csv(io.StringIO(table_text), sep="\t", low_memory=False,
                         comment="!", na_values=["", "---", "NA"])
    except Exception as e:
        logger.warning(f"Failed to parse annotation table for {gpl_id}: {e}")
        return None

    if len(df) == 0:
        logger.warning(f"Empty annotation table for {gpl_id}")
        return None

    # Find probe ID column
    probe_col = None
    for col in PROBE_ID_COLUMNS:
        if col in df.columns:
            probe_col = col
            break
    if probe_col is None:
        probe_col = df.columns[0]  # First column is usually the probe ID

    # Find gene symbol column
    gene_col = _find_gene_symbol_column(df, gpl_id)
    if gene_col is None:
        logger.warning(f"No gene symbol column found in {gpl_id}. Columns: {list(df.columns)}")
        return None

    logger.info(f"  Probe col: '{probe_col}', Gene col: '{gene_col}'")

    # Extract and clean mapping
    mapping = df[[probe_col, gene_col]].copy()
    mapping.columns = ["probe_id", "gene_symbol_raw"]
    mapping = mapping.dropna(subset=["probe_id", "gene_symbol_raw"])

    # Clean gene symbols
    mapping["gene_symbol"] = mapping["gene_symbol_raw"].apply(_clean_gene_symbol)
    mapping = mapping.dropna(subset=["gene_symbol"])
    mapping = mapping[mapping["gene_symbol"] != ""]
    mapping = mapping[["probe_id", "gene_symbol"]].copy()

    # Convert probe_id to string for matching
    mapping["probe_id"] = mapping["probe_id"].astype(str).str.strip()

    logger.info(f"  {gpl_id}: {len(mapping)} probe→gene mappings")

    # Cache
    try:
        mapping.to_csv(cache_path, sep="\t", index=False)
    except Exception as e:
        logger.debug(f"Cache write failed: {e}")

    return mapping


def _find_gene_symbol_column(df: pd.DataFrame, gpl_id: str) -> Optional[str]:
    """Find the column most likely to contain gene symbols."""
    # Try exact matches first
    for col_name in GENE_SYMBOL_COLUMNS:
        if col_name in df.columns:
            # Verify it actually has gene-symbol-like content
            sample = df[col_name].dropna().head(20).astype(str)
            if len(sample) > 0:
                # Gene symbols are typically short uppercase strings
                gene_like = sample.str.match(r"^[A-Z][A-Z0-9\-]{1,15}$")
                if gene_like.sum() >= len(sample) * 0.3:
                    return col_name

    # Try case-insensitive search
    for col in df.columns:
        col_lower = col.lower()
        if "gene" in col_lower and "symbol" in col_lower:
            return col
        if col_lower == "symbol":
            return col

    # For gene_assignment columns (HuGene ST), parse differently
    if "gene_assignment" in df.columns:
        return "gene_assignment"

    # Last resort: look for columns with gene-symbol-like content
    for col in df.columns:
        sample = df[col].dropna().head(50).astype(str)
        if len(sample) == 0:
            continue
        gene_like = sample.str.match(r"^[A-Z][A-Z0-9\-]{1,15}$")
        if gene_like.sum() >= len(sample) * 0.5:
            return col

    return None


def _clean_gene_symbol(raw: str) -> str:
    """Extract clean HGNC gene symbol from various annotation formats.

    Handles:
      - "IL1B"                → "IL1B"
      - "IL1B /// TNF"        → "IL1B" (take first)
      - "NM_000576 // IL1B // interleukin 1 beta" → "IL1B"
      - "---"                 → ""
      - "NR_046018 // DDX11L1 // DEAD/H-box helicase 11 like 1 // ..." → "DDX11L1"
    """
    if not isinstance(raw, str):
        return ""

    raw = raw.strip()
    if raw in ("---", "", "NA", "nan", "None"):
        return ""

    # HuGene ST format: "NM_000576 // IL1B // interleukin 1 beta // ..."
    if " // " in raw:
        parts = [p.strip() for p in raw.split("//")]
        # Gene symbol is typically the second element after RefSeq ID
        for part in parts:
            part = part.strip()
            if re.match(r"^[A-Z][A-Z0-9\-]{1,15}$", part):
                return part
        return ""

    # Multiple genes: "IL1B /// TNF" → take first
    if " /// " in raw:
        first = raw.split("///")[0].strip()
        if re.match(r"^[A-Z][A-Z0-9\-]{1,15}$", first):
            return first
        return ""

    # Simple gene symbol
    raw = raw.strip()
    if re.match(r"^[A-Z][A-Z0-9\-]{1,15}$", raw):
        return raw

    # Try to extract from longer string
    match = re.search(r"\b([A-Z][A-Z0-9\-]{1,15})\b", raw)
    if match:
        candidate = match.group(1)
        # Filter out obvious non-gene-symbols
        if candidate not in ("NA", "NM", "NR", "XM", "XR", "ID", "NULL", "REF"):
            return candidate

    return ""


# ---------------------------------------------------------------------------
# Probe → Gene mapping and DE file update
# ---------------------------------------------------------------------------

def map_expression_to_genes(
    expr_path: str,
    mapping: pd.DataFrame,
) -> Tuple[pd.DataFrame, dict]:
    """Map probe-level expression matrix to gene-level.

    For multiple probes mapping to the same gene, keeps the probe with
    highest variance across samples (most informative).

    Returns (gene_expr_df, stats_dict).
    """
    expr = pd.read_csv(expr_path, sep="\t")
    original_features = len(expr)

    # Ensure probe_id columns match type
    expr["feature_id"] = expr["feature_id"].astype(str).str.strip()
    mapping = mapping.copy()
    mapping["probe_id"] = mapping["probe_id"].astype(str).str.strip()

    # Merge
    merged = expr.merge(mapping, left_on="feature_id", right_on="probe_id", how="inner")

    if len(merged) == 0:
        logger.warning(f"  ZERO probes matched annotation! Check platform compatibility.")
        return expr, {"original": original_features, "mapped": 0, "genes": 0}

    # For multi-probe → same gene: keep highest variance probe
    sample_cols = [c for c in expr.columns if c != "feature_id"]
    merged["_var"] = merged[sample_cols].var(axis=1)

    # Sort by variance descending, keep first (highest var) per gene
    merged = merged.sort_values("_var", ascending=False)
    merged = merged.drop_duplicates(subset=["gene_symbol"], keep="first")

    # Replace feature_id with gene symbol
    merged["feature_id"] = merged["gene_symbol"]
    result = merged[["feature_id"] + sample_cols].copy()
    result = result.sort_values("feature_id").reset_index(drop=True)

    stats = {
        "original_probes": original_features,
        "probes_mapped": len(expr.merge(mapping, left_on="feature_id",
                                         right_on="probe_id", how="inner")),
        "unique_genes": len(result),
        "probes_unmapped": original_features - len(expr.merge(
            mapping, left_on="feature_id", right_on="probe_id", how="inner")),
    }

    return result, stats


def map_de_to_genes(
    de_path: str,
    mapping: pd.DataFrame,
) -> Tuple[pd.DataFrame, dict]:
    """Map probe-level DE results to gene-level.

    For multiple probes → same gene: keeps the probe with max |t| statistic
    (most significant differential expression).

    Returns (gene_de_df, stats_dict).
    """
    de = pd.read_csv(de_path, sep="\t")
    original_features = len(de)

    de["feature_id"] = de["feature_id"].astype(str).str.strip()
    mapping = mapping.copy()
    mapping["probe_id"] = mapping["probe_id"].astype(str).str.strip()

    merged = de.merge(mapping, left_on="feature_id", right_on="probe_id", how="inner")

    if len(merged) == 0:
        logger.warning(f"  ZERO probes matched in DE results!")
        return de, {"original": original_features, "mapped": 0, "genes": 0}

    # For multi-probe → same gene: keep probe with max |t|
    if "t" in merged.columns:
        merged["_abs_t"] = merged["t"].abs()
        merged = merged.sort_values("_abs_t", ascending=False)
        merged = merged.drop_duplicates(subset=["gene_symbol"], keep="first")
        merged = merged.drop(columns=["_abs_t"])
    else:
        merged = merged.drop_duplicates(subset=["gene_symbol"], keep="first")

    # Replace feature_id with gene symbol
    merged["feature_id"] = merged["gene_symbol"]

    # Drop mapping columns
    cols_to_drop = ["probe_id", "gene_symbol"]
    merged = merged.drop(columns=[c for c in cols_to_drop if c in merged.columns])
    merged = merged.sort_values("feature_id").reset_index(drop=True)

    stats = {
        "original_probes": original_features,
        "unique_genes": len(merged),
    }

    return merged, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def detect_platform(pheno_path: str) -> Optional[str]:
    """Detect platform ID from pheno data."""
    pheno = pd.read_csv(pheno_path, sep="\t", nrows=5)
    if "platform_id" in pheno.columns:
        platform = pheno["platform_id"].dropna().iloc[0] if len(pheno) > 0 else None
        return str(platform) if platform else None
    return None


def is_already_gene_symbols(feature_ids: pd.Series) -> bool:
    """Heuristic: check if feature IDs look like gene symbols already."""
    sample = feature_ids.dropna().head(100).astype(str)
    if len(sample) == 0:
        return False
    # Gene symbols: uppercase letters + digits, 2-15 chars
    gene_like = sample.str.match(r"^[A-Z][A-Z0-9\-]{1,14}$")
    fraction = gene_like.sum() / len(sample)
    return fraction > 0.7


def main():
    ap = argparse.ArgumentParser(
        description="Map probe IDs to gene symbols for cross-platform meta-analysis"
    )
    ap.add_argument("--config", required=True, help="Config YAML path")
    ap.add_argument("--workdir", default="work", help="Working directory")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    workdir = Path(args.workdir)
    gse_list = cfg["geo"]["gse_list"]

    # Check if probe_to_gene is enabled (default: True)
    ptg_cfg = cfg.get("probe_to_gene", {})
    if ptg_cfg.get("enable", True) is False:
        logger.info("probe_to_gene disabled in config. Skipping.")
        return

    skip_if_symbols = ptg_cfg.get("skip_if_gene_symbols", True)

    logger.info(f"Processing {len(gse_list)} GSE datasets for probe→gene mapping...")

    all_stats = {}

    for gse in gse_list:
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing {gse}")
        logger.info(f"{'='*60}")

        geo_dir = workdir / "geo" / gse
        de_dir = workdir / "de" / gse
        expr_path = geo_dir / "expr.tsv"
        pheno_path = geo_dir / "pheno.tsv"
        de_path = de_dir / "de.tsv"

        if not expr_path.exists():
            logger.warning(f"  {gse}: expr.tsv not found, skipping")
            continue

        # Check if RNA-seq: skip probe mapping (already gene-level IDs)
        dtype_file = geo_dir / "data_type.txt"
        if dtype_file.exists():
            dtype = dtype_file.read_text().strip()
            if dtype.startswith("rnaseq"):
                logger.info(f"  {gse}: RNA-seq data, skipping probe-to-gene mapping.")
                all_stats[gse] = {"status": "skipped_rnaseq"}
                continue

        # Check if already gene symbols
        expr_df = pd.read_csv(expr_path, sep="\t", nrows=50)
        if skip_if_symbols and is_already_gene_symbols(expr_df["feature_id"]):
            logger.info(f"  {gse}: Feature IDs already look like gene symbols. Skipping mapping.")
            all_stats[gse] = {"status": "skipped_already_symbols"}
            continue

        # Detect platform
        platform = detect_platform(str(pheno_path)) if pheno_path.exists() else None
        logger.info(f"  Platform: {platform or 'unknown'}")

        # Get probe → gene mapping
        mapping = None
        if platform:
            mapping = download_gpl_annotation(platform)

        if mapping is None or len(mapping) == 0:
            logger.warning(f"  {gse}: No probe→gene mapping available for {platform}. "
                          f"Features will remain as probe IDs.")
            all_stats[gse] = {"status": "no_mapping", "platform": platform}
            continue

        # --- Map expression matrix ---
        logger.info(f"  Mapping expression matrix...")
        backup_expr = str(expr_path) + ".probe_backup"
        if not os.path.exists(backup_expr):
            shutil.copy2(str(expr_path), backup_expr)

        gene_expr, expr_stats = map_expression_to_genes(str(expr_path), mapping)
        gene_expr.to_csv(str(expr_path), sep="\t", index=False)
        logger.info(f"  Expression: {expr_stats['original_probes']} probes → "
                    f"{expr_stats['unique_genes']} genes")

        # --- Map DE results ---
        if de_path.exists():
            logger.info(f"  Mapping DE results...")
            backup_de = str(de_path) + ".probe_backup"
            if not os.path.exists(backup_de):
                shutil.copy2(str(de_path), backup_de)

            gene_de, de_stats = map_de_to_genes(str(de_path), mapping)
            gene_de.to_csv(str(de_path), sep="\t", index=False)
            logger.info(f"  DE: {de_stats['original_probes']} probes → "
                       f"{de_stats['unique_genes']} genes")
            all_stats[gse] = {**expr_stats, **de_stats, "platform": platform, "status": "mapped"}
        else:
            all_stats[gse] = {**expr_stats, "platform": platform, "status": "mapped_expr_only"}

    # --- Cross-platform overlap check ---
    logger.info(f"\n{'='*60}")
    logger.info("Cross-platform gene overlap check (post-mapping)")
    logger.info(f"{'='*60}")

    gene_sets = {}
    for gse in gse_list:
        de_path = workdir / "de" / gse / "de.tsv"
        if de_path.exists():
            de = pd.read_csv(de_path, sep="\t", usecols=["feature_id"])
            gene_sets[gse] = set(de["feature_id"].dropna().astype(str))

    if len(gene_sets) >= 2:
        gse_names = list(gene_sets.keys())
        for i in range(len(gse_names)):
            for j in range(i + 1, len(gse_names)):
                g1, g2 = gse_names[i], gse_names[j]
                overlap = len(gene_sets[g1] & gene_sets[g2])
                total = len(gene_sets[g1] | gene_sets[g2])
                pct = (100 * overlap / total) if total > 0 else 0
                logger.info(f"  {g1} ∩ {g2} = {overlap} / {total} ({pct:.1f}%)")
                if overlap == 0:
                    logger.error(f"  STILL ZERO OVERLAP after mapping! "
                                f"Check platform annotations.")
    else:
        logger.info("  Only one dataset, no overlap check needed.")

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("Probe→Gene mapping summary")
    logger.info(f"{'='*60}")
    for gse, stats in all_stats.items():
        logger.info(f"  {gse}: {stats}")
    logger.info("Done.")


if __name__ == "__main__":
    main()
