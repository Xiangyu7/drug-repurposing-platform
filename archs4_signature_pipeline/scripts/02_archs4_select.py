#!/usr/bin/env python3
"""
02_archs4_select.py - Select disease vs control samples from ARCHS4 H5 file

Reads the ARCHS4 human gene expression H5 file (~30GB), searches metadata
for disease-relevant samples, classifies them as case/control, groups by
series (GSE), and extracts raw count matrices for DESeq2.

Output per selected series:
  work/{disease}/archs4/{series_id}/counts.tsv   (gene x sample matrix)
  work/{disease}/archs4/{series_id}/coldata.tsv  (sample metadata with group column)
  work/{disease}/archs4/selected_series.json     (summary of selection)
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger("archs4.select")


class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def decode_h5_strings(dataset, indices=None):
    """Decode HDF5 byte strings to Python strings."""
    if indices is not None:
        raw = dataset[indices]
    else:
        raw = dataset[:]
    if raw.dtype.kind == 'O':
        # Already strings or object arrays
        return [s.decode("utf-8") if isinstance(s, bytes) else str(s) for s in raw]
    elif raw.dtype.kind == 'S':
        # Fixed-length byte strings
        return [s.decode("utf-8") for s in raw]
    else:
        return [str(s) for s in raw]


def search_samples(h5_file: h5py.File, case_keywords: list, control_keywords: list,
                   tissue_keywords: list = None) -> pd.DataFrame:
    """
    Search ARCHS4 metadata for disease-relevant samples.

    Returns DataFrame with columns:
      idx, gsm, series_id, title, source, characteristics, group (case/control/unknown)
    """
    logger.info("Loading ARCHS4 metadata...")

    # Load metadata arrays
    meta = h5_file["meta"]

    # ARCHS4 v2.4 structure: meta/samples/...
    samples_meta = meta["samples"]

    # Get all sample metadata
    n_samples = len(samples_meta["geo_accession"])
    logger.info("Total samples in ARCHS4: %d", n_samples)

    # Load text fields for keyword search
    # These are large arrays, load all at once
    geo_accession = decode_h5_strings(samples_meta["geo_accession"])
    series_id = decode_h5_strings(samples_meta["series_id"])
    title = decode_h5_strings(samples_meta["title"])
    source_name = decode_h5_strings(samples_meta["source_name_ch1"])
    characteristics = decode_h5_strings(samples_meta["characteristics_ch1"])

    # Combine text fields for search
    logger.info("Searching for disease-relevant samples...")
    case_pattern = "|".join(re.escape(kw.lower()) for kw in case_keywords)
    control_pattern = "|".join(re.escape(kw.lower()) for kw in control_keywords)

    results = []
    n_ambiguous = 0
    for i in range(n_samples):
        # Combine all text fields
        text = " | ".join([
            title[i] or "",
            source_name[i] or "",
            characteristics[i] or "",
        ]).lower()

        is_case = bool(re.search(case_pattern, text))
        is_control = bool(re.search(control_pattern, text))

        # Only keep samples that match case OR control
        if not (is_case or is_control):
            continue

        # Apply tissue filter if specified
        if tissue_keywords:
            tissue_pattern = "|".join(re.escape(kw.lower()) for kw in tissue_keywords)
            if not re.search(tissue_pattern, text):
                continue

        # Classify
        if is_case and not is_control:
            group = "case"
        elif is_control and not is_case:
            group = "control"
        else:
            # Both match - ambiguous, skip with logging
            n_ambiguous += 1
            continue

        results.append({
            "idx": i,
            "gsm": geo_accession[i],
            "series_id": series_id[i],
            "title": title[i],
            "source": source_name[i],
            "characteristics": characteristics[i],
            "group": group,
        })

    df = pd.DataFrame(results)
    if n_ambiguous > 0:
        logger.warning("Skipped %d ambiguous samples (matched both case AND control keywords)", n_ambiguous)
    if len(df) > 0:
        n_case = (df["group"] == "case").sum()
        n_control = (df["group"] == "control").sum()
        n_series = df["series_id"].nunique()
        logger.info("Found %d case + %d control samples across %d series",
                    n_case, n_control, n_series)
    else:
        logger.warning("No matching samples found!")

    return df


def select_best_series(
    sample_df: pd.DataFrame,
    min_per_group: int = 3,
    max_per_group: int = 50,
    max_series: int = 5,
) -> list[dict]:
    """
    Select top series (GSEs) with sufficient case + control samples.

    Selection criteria:
      1. At least min_per_group case AND min_per_group control
      2. Ranked by: min(n_case, n_control) then total samples
      3. Take top max_series
    """
    if len(sample_df) == 0:
        return []

    # Group by series
    series_stats = []
    for sid, grp in sample_df.groupby("series_id"):
        n_case = (grp["group"] == "case").sum()
        n_control = (grp["group"] == "control").sum()

        if n_case < min_per_group or n_control < min_per_group:
            continue

        # Cap per-group counts
        n_case_use = min(n_case, max_per_group)
        n_control_use = min(n_control, max_per_group)

        series_stats.append({
            "series_id": sid,
            "n_case": n_case,
            "n_control": n_control,
            "n_case_use": n_case_use,
            "n_control_use": n_control_use,
            "balance_score": min(n_case_use, n_control_use),
            "total": n_case_use + n_control_use,
        })

    if not series_stats:
        return []

    # Sort by balance_score (desc), then total (desc)
    series_stats.sort(key=lambda x: (x["balance_score"], x["total"]), reverse=True)

    # Take top N
    selected = series_stats[:max_series]
    logger.info("Selected %d series:", len(selected))
    for s in selected:
        logger.info("  %s: %d case + %d control (using %d + %d)",
                    s["series_id"], s["n_case"], s["n_control"],
                    s["n_case_use"], s["n_control_use"])

    return selected


def extract_counts(
    h5_file: h5py.File,
    sample_df: pd.DataFrame,
    series_info: dict,
    max_per_group: int = 50,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extract raw count matrix for a selected series.

    Returns:
      counts_df: gene_symbol x sample matrix (DataFrame)
      coldata_df: sample metadata with 'group' column
    """
    sid = series_info["series_id"]
    series_samples = sample_df[sample_df["series_id"] == sid].copy()

    # Balance groups by subsampling if needed
    cases = series_samples[series_samples["group"] == "case"]
    controls = series_samples[series_samples["group"] == "control"]

    if len(cases) > max_per_group:
        cases = cases.sample(n=max_per_group, random_state=42)
    if len(controls) > max_per_group:
        controls = controls.sample(n=max_per_group, random_state=42)

    selected = pd.concat([cases, controls])
    indices = selected["idx"].values

    # Get gene symbols
    gene_symbols = decode_h5_strings(h5_file["meta"]["genes"]["gene_symbol"])

    # Extract count matrix (genes x samples)
    logger.info("  Extracting counts for %s (%d samples)...", sid, len(indices))

    # Sort indices for efficient HDF5 access
    sorted_order = np.argsort(indices)
    sorted_indices = indices[sorted_order]

    # Read counts - ARCHS4 stores as (genes x samples) in data/expression
    expression = h5_file["data"]["expression"]

    # Read column by column (each column is a sample)
    counts = np.zeros((len(gene_symbols), len(indices)), dtype=np.int32)
    for out_idx, sample_idx in enumerate(sorted_indices):
        counts[:, out_idx] = expression[:, sample_idx]

    # Reorder back to original order
    inverse_order = np.argsort(sorted_order)
    counts = counts[:, inverse_order]

    # Create DataFrames
    gsm_ids = selected["gsm"].values
    counts_df = pd.DataFrame(counts, index=gene_symbols, columns=gsm_ids)
    counts_df.index.name = "feature_id"

    coldata_df = selected[["gsm", "group", "title", "source", "characteristics"]].copy()
    coldata_df = coldata_df.reset_index(drop=True)

    # Remove genes with all zeros
    nonzero_mask = counts_df.sum(axis=1) > 0
    n_zero = (~nonzero_mask).sum()
    counts_df = counts_df[nonzero_mask]
    logger.info("  %s: %d genes x %d samples (removed %d zero-count genes)",
                sid, counts_df.shape[0], counts_df.shape[1], n_zero)

    return counts_df, coldata_df


def main():
    ap = argparse.ArgumentParser(description="Select disease vs control samples from ARCHS4")
    ap.add_argument("--config", required=True, help="Config YAML path")
    ap.add_argument("--workdir", default="work", help="Work directory")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    disease_name = cfg["disease"]["name"]
    h5_path = cfg["archs4"]["h5_path"]
    case_keywords = cfg["archs4"].get("case_keywords", [disease_name])
    control_keywords = cfg["archs4"].get("control_keywords", ["normal", "healthy", "control"])
    tissue_keywords = cfg["archs4"].get("tissue_keywords", None)
    min_per_group = cfg["archs4"].get("min_samples_per_group", 3)
    max_per_group = cfg["archs4"].get("max_samples_per_group", 50)
    max_series = cfg["archs4"].get("max_series", 5)

    workdir = Path(args.workdir)
    out_dir = workdir / "archs4"
    summary_file = out_dir / "selected_series.json"

    # Check cache
    if summary_file.exists():
        with open(summary_file) as f:
            existing = json.load(f)
        if existing.get("n_series", 0) > 0:
            logger.info("Using cached ARCHS4 selection: %d series from %s",
                        existing["n_series"], summary_file)
            return

    # Validate H5 file
    if not Path(h5_path).exists():
        logger.error("ARCHS4 H5 file not found: %s", h5_path)
        logger.error("Download from: https://archs4.org/download/file/human_gene_v2.4.h5")
        raise SystemExit(1)

    # Open H5 and search
    logger.info("Opening ARCHS4 H5 file: %s", h5_path)
    with h5py.File(h5_path, "r") as h5:
        # Search metadata
        sample_df = search_samples(h5, case_keywords, control_keywords, tissue_keywords)

        if len(sample_df) == 0:
            # Level 1 fallback: try without tissue filter
            if tissue_keywords:
                logger.warning("No samples with tissue filter. Retrying without tissue restriction...")
                sample_df = search_samples(h5, case_keywords, control_keywords, tissue_keywords=None)

        if len(sample_df) == 0:
            logger.error("No matching samples found in ARCHS4 for disease: %s", disease_name)
            logger.error("Keywords used - case: %s, control: %s", case_keywords, control_keywords)
            # Write empty summary
            out_dir.mkdir(parents=True, exist_ok=True)
            summary = {
                "disease": disease_name,
                "n_series": 0,
                "status": "no_samples_found",
                "case_keywords": case_keywords,
                "control_keywords": control_keywords,
            }
            summary_file.write_text(json.dumps(summary, indent=2, cls=NumpyEncoder))
            raise SystemExit(1)

        # Select best series
        selected = select_best_series(sample_df, min_per_group, max_per_group, max_series)

        if not selected:
            # Level 1 fallback: try with fewer min samples
            logger.warning("No series with >= %d per group. Trying with min=2...", min_per_group)
            selected = select_best_series(sample_df, min_per_group=2,
                                          max_per_group=max_per_group, max_series=max_series)

        if not selected:
            logger.error("No series with sufficient case+control samples for: %s", disease_name)
            out_dir.mkdir(parents=True, exist_ok=True)
            summary = {
                "disease": disease_name,
                "n_series": 0,
                "status": "insufficient_samples",
                "total_matching_samples": len(sample_df),
            }
            summary_file.write_text(json.dumps(summary, indent=2, cls=NumpyEncoder))
            raise SystemExit(1)

        # --- SAFETY: Cross-series sample deduplication ---
        # Prevent pseudo-replication: if the same GSM appears in multiple series,
        # keep it only in the series with the best balance_score.
        selected_sids = [s["series_id"] for s in selected]
        seen_gsms: set[str] = set()
        for series_info in selected:
            sid = series_info["series_id"]
            series_gsms = set(sample_df[sample_df["series_id"] == sid]["gsm"])
            overlap = series_gsms & seen_gsms
            if overlap:
                logger.warning("  [QC] %s: %d samples overlap with earlier series — removing them",
                               sid, len(overlap))
                # Remove overlapping samples from sample_df for this series
                sample_df = sample_df[~((sample_df["series_id"] == sid) &
                                        (sample_df["gsm"].isin(overlap)))]
                # Recount
                remaining = sample_df[sample_df["series_id"] == sid]
                n_case_remaining = (remaining["group"] == "case").sum()
                n_ctrl_remaining = (remaining["group"] == "control").sum()
                series_info["n_case"] = n_case_remaining
                series_info["n_control"] = n_ctrl_remaining
                series_info["n_case_use"] = min(n_case_remaining, max_per_group)
                series_info["n_control_use"] = min(n_ctrl_remaining, max_per_group)
                if n_case_remaining < 2 or n_ctrl_remaining < 2:
                    logger.warning("  [QC] %s: insufficient samples after dedup (%d case, %d ctrl) — SKIPPING",
                                   sid, n_case_remaining, n_ctrl_remaining)
                    series_info["_skip"] = True
            seen_gsms |= series_gsms

        # Remove skipped series
        selected = [s for s in selected if not s.get("_skip", False)]
        if not selected:
            logger.error("All series eliminated after sample deduplication")
            out_dir.mkdir(parents=True, exist_ok=True)
            summary = {"disease": disease_name, "n_series": 0,
                        "status": "all_series_deduplicated"}
            summary_file.write_text(json.dumps(summary, indent=2, cls=NumpyEncoder))
            raise SystemExit(1)

        logger.info("After dedup: %d series retained", len(selected))

        # Extract counts for each selected series
        for series_info in selected:
            sid = series_info["series_id"]
            series_dir = out_dir / sid
            series_dir.mkdir(parents=True, exist_ok=True)

            counts_df, coldata_df = extract_counts(h5, sample_df, series_info, max_per_group)

            # Save with feature_id as first column
            counts_out = series_dir / "counts.tsv"
            counts_df.to_csv(counts_out, sep="\t")

            coldata_out = series_dir / "coldata.tsv"
            coldata_df.to_csv(coldata_out, sep="\t", index=False)

            logger.info("  Saved: %s (%d genes x %d samples)", series_dir,
                        counts_df.shape[0], counts_df.shape[1])

    # Save selection summary
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "disease": disease_name,
        "n_series": len(selected),
        "status": "success",
        "case_keywords": case_keywords,
        "control_keywords": control_keywords,
        "tissue_keywords": tissue_keywords,
        "series": selected,
    }
    summary_file.write_text(json.dumps(summary, indent=2, cls=NumpyEncoder))
    logger.info("Selection complete: %d series for %s", len(selected), disease_name)


if __name__ == "__main__":
    main()
