#!/usr/bin/env python3
"""
04_assemble_signature.py - Assemble final disease signature

Combines OpenTargets prior genes with DE meta-analysis results:
  1. Load DE meta results (gene_meta.tsv)
  2. Load OpenTargets gene-disease associations
  3. SOFT PRIOR: keep ALL DE genes; OT genes get score boost, non-OT genes
     get a reduced but non-zero weight (p5 of OT scores) so purely
     data-driven discoveries can still enter the signature.
  4. Weight = |meta_z| * ot_weight (OT score for OT genes, p5 fallback for non-OT)
  5. Select top 300 up + top 300 down
  6. Output in dsmeta-compatible format for sigreverse/KG
  7. Write gene_audit.json tracking gene counts through pipeline

Output (identical format to dsmeta_signature_pipeline):
  - disease_signature_meta.json (schema v2.0)
  - sigreverse_input.json ({name, up, down})
  - up_genes.txt, down_genes.txt
  - gene_audit.json (gene count tracking through every stage)
"""
import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger("archs4.assemble")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SCHEMA_VERSION = "2.0"


def _collect_upstream_audit(workdir: Path, outdir: Path) -> dict:
    """Collect gene counts from upstream pipeline steps for the audit trail."""
    audit = {"stages": []}

    # Stage 1: OpenTargets prior
    ot_path = workdir / "opentargets" / "gene_disease_associations.tsv"
    if ot_path.exists():
        ot_df = pd.read_csv(ot_path, sep="\t")
        audit["stages"].append({
            "step": 1,
            "name": "opentargets_prior",
            "genes_out": len(ot_df),
            "detail": f"EFO query → {len(ot_df)} disease-associated genes",
        })

    # Stage 2: ARCHS4 sample selection (count unique genes across all series)
    archs4_dir = workdir / "archs4"
    summary_file = archs4_dir / "selected_series.json"
    if summary_file.exists():
        with open(summary_file) as f:
            summary = json.load(f)
        n_series = summary.get("n_series", 0)
        # Count genes from first series counts file as representative
        series_gene_counts = []
        for sdir in sorted(archs4_dir.iterdir()):
            counts_file = sdir / "counts.tsv"
            if sdir.is_dir() and counts_file.exists():
                # Read only first column (gene symbols)
                genes = pd.read_csv(counts_file, sep="\t", usecols=[0], nrows=0)
                n_genes = sum(1 for _ in open(counts_file)) - 1  # minus header
                series_gene_counts.append({"series": sdir.name, "genes": n_genes})
        audit["stages"].append({
            "step": 2,
            "name": "archs4_select",
            "n_series": n_series,
            "per_series_genes": series_gene_counts,
            "detail": f"{n_series} series selected",
        })

    # Stage 3: DE analysis (count genes per series DE output)
    de_dir = workdir / "de"
    if de_dir.exists():
        de_gene_counts = []
        for sdir in sorted(de_dir.iterdir()):
            de_file = sdir / "de.tsv"
            if sdir.is_dir() and de_file.exists():
                de_dt = pd.read_csv(de_file, sep="\t")
                de_gene_counts.append({
                    "series": sdir.name,
                    "genes_tested": len(de_dt),
                    "sig_005": int((de_dt["adj.P.Val"] < 0.05).sum()) if "adj.P.Val" in de_dt.columns else None,
                })
        audit["stages"].append({
            "step": 3,
            "name": "de_analysis",
            "per_series": de_gene_counts,
            "detail": f"DESeq2 on {len(de_gene_counts)} series",
        })

    # Stage 4: Meta-analysis
    meta_path = outdir / "signature" / "gene_meta.tsv"
    if meta_path.exists():
        meta_df = pd.read_csv(meta_path, sep="\t")
        n_valid = meta_df.dropna(subset=["meta_logFC", "meta_z"]).shape[0]
        n_sig = int((meta_df["fdr"] < 0.05).sum()) if "fdr" in meta_df.columns else None
        audit["stages"].append({
            "step": 4,
            "name": "meta_analysis",
            "genes_total": len(meta_df),
            "genes_valid": n_valid,
            "genes_fdr005": n_sig,
            "detail": f"Meta: {len(meta_df)} total, {n_valid} valid, {n_sig} FDR<0.05",
        })

    return audit


def _lincs_coverage_check(up_genes: list, down_genes: list) -> dict:
    """Check what fraction of signature genes are findable in LINCS LDP3.

    This is a lightweight pre-check: it queries the LINCS entity API to see
    how many of our signature genes can be mapped. Low coverage (<50%)
    means sigreverse will have limited statistical power.

    Returns dict with coverage stats (does NOT fail the pipeline).
    """
    all_genes = list(set(up_genes + down_genes))
    if not all_genes:
        return {"checked": False, "reason": "no_genes"}

    # Try importing the LDP3 client
    try:
        import sys
        sigreverse_path = str(Path(__file__).resolve().parents[2] / "sigreverse")
        if sigreverse_path not in sys.path:
            sys.path.insert(0, sigreverse_path)
        from sigreverse.ldp3_client import LDP3Client
    except ImportError:
        logger.warning("[LINCS check] Cannot import LDP3Client — skipping coverage check")
        return {"checked": False, "reason": "import_failed"}

    try:
        client = LDP3Client()
        entities = client.entities_find_by_symbols(all_genes)

        # Extract found symbols
        found_symbols = set()
        for ent in entities:
            meta = ent.get("meta", {})
            sym = meta.get("symbol", "") if isinstance(meta, dict) else ""
            if sym:
                found_symbols.add(sym.upper())

        # Compute coverage
        query_upper = {g.upper() for g in all_genes}
        matched = query_upper & found_symbols
        coverage = len(matched) / len(query_upper) if query_upper else 0

        up_matched = sum(1 for g in up_genes if g.upper() in found_symbols)
        down_matched = sum(1 for g in down_genes if g.upper() in found_symbols)

        missing = sorted(query_upper - found_symbols)

        result = {
            "checked": True,
            "total_queried": len(query_upper),
            "total_found": len(matched),
            "coverage_pct": round(coverage * 100, 1),
            "up_queried": len(up_genes),
            "up_found": up_matched,
            "down_queried": len(down_genes),
            "down_found": down_matched,
            "missing_genes_sample": missing[:20],  # First 20 for debugging
        }

        if coverage < 0.5:
            logger.warning("[LINCS check] LOW coverage: %.1f%% (%d/%d genes found in LINCS). "
                          "sigreverse may have weak statistical power.",
                          coverage * 100, len(matched), len(query_upper))
        else:
            logger.info("[LINCS check] Coverage: %.1f%% (%d/%d genes found in LINCS)",
                       coverage * 100, len(matched), len(query_upper))

        return result

    except Exception as e:
        logger.warning("[LINCS check] API call failed: %s — skipping", e)
        return {"checked": False, "reason": f"api_error: {e}"}


def main():
    ap = argparse.ArgumentParser(description="Assemble disease signature from OT + DE")
    ap.add_argument("--config", required=True, help="Config YAML path")
    ap.add_argument("--workdir", default="work", help="Work directory")
    ap.add_argument("--skip-lincs-check", action="store_true",
                    help="Skip LINCS gene coverage pre-check")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    outdir = Path(cfg["project"]["outdir"])
    workdir = Path(args.workdir)
    disease_name = cfg["disease"]["name"]
    top_n = cfg["signature"].get("top_n", 300)
    min_de_fdr = cfg["signature"].get("min_de_fdr", 0.5)

    sig_dir = outdir / "signature"
    sig_dir.mkdir(parents=True, exist_ok=True)

    # --- Gene audit: collect upstream counts ---
    audit = _collect_upstream_audit(workdir, outdir)

    # --- Load DE meta results ---
    meta_path = sig_dir / "gene_meta.tsv"
    if not meta_path.exists():
        logger.error("gene_meta.tsv not found at %s", meta_path)
        _write_empty(sig_dir, disease_name, top_n)
        raise SystemExit(1)

    de_df = pd.read_csv(meta_path, sep="\t")
    de_df = de_df.dropna(subset=["meta_logFC", "meta_z"]).copy()
    n_de_valid = len(de_df)
    logger.info("DE meta results: %d genes", n_de_valid)

    if len(de_df) == 0:
        logger.error("No valid genes in DE meta results")
        _write_empty(sig_dir, disease_name, top_n)
        return

    # --- Load OpenTargets prior ---
    ot_path = workdir / "opentargets" / "gene_disease_associations.tsv"
    if not ot_path.exists():
        logger.warning("OpenTargets associations not found at %s", ot_path)
        logger.warning("Proceeding without OT filter (using all DE genes)")
        ot_df = None
    else:
        ot_df = pd.read_csv(ot_path, sep="\t")
        logger.info("OpenTargets prior: %d genes", len(ot_df))

    # --- Soft prior: OT genes boosted, non-OT genes kept with reduced weight ---
    # RATIONALE: Hard intersection (old code) dropped all genes not in OpenTargets,
    # preventing discovery of novel biology.  Soft prior keeps ALL DE genes but
    # gives OT-associated genes a score boost via their OT association score.
    # Non-OT genes receive the 5th percentile of OT scores as weight, so they
    # can still enter the signature if their DE signal is strong enough.
    total_de = len(de_df)
    if ot_df is not None and len(ot_df) > 0:
        # OT has gene_symbol, DE has feature_id (also gene symbols from ARCHS4)
        ot_genes = set(ot_df["gene_symbol"].dropna().str.upper())
        de_df["feature_id_upper"] = de_df["feature_id"].astype(str).str.upper()

        # Merge OT score (left join — keep ALL DE genes)
        ot_lookup = ot_df.drop_duplicates(subset=["gene_symbol"]).set_index(
            ot_df["gene_symbol"].str.upper()
        )["ot_score"]
        de_df["ot_score"] = de_df["feature_id_upper"].map(ot_lookup)

        # Compute fallback weight for non-OT genes
        valid_scores = de_df["ot_score"].dropna()
        if len(valid_scores) > 0:
            fallback_score = float(np.percentile(valid_scores, 5))
        else:
            fallback_score = 0.1
        # Ensure fallback is at least 0.05 so non-OT genes are not zeroed out
        fallback_score = max(fallback_score, 0.05)

        n_in_ot = int(de_df["ot_score"].notna().sum())
        n_not_in_ot = int(de_df["ot_score"].isna().sum())
        logger.info("  OT soft prior: %d genes in OT, %d genes NOT in OT (fallback weight: %.4f)",
                    n_in_ot, n_not_in_ot, fallback_score)
        de_df["ot_score"] = de_df["ot_score"].fillna(fallback_score)
        de_df["in_opentargets"] = de_df["feature_id_upper"].isin(ot_genes)

        de_df = de_df.drop(columns=["feature_id_upper"])

        n_after_ot = len(de_df)  # Now equals total_de (no genes dropped)
        logger.info("After OT soft prior: %d / %d genes retained (100%%, soft mode)",
                    n_after_ot, total_de)
    else:
        de_df["ot_score"] = 1.0  # No OT data, uniform weight
        de_df["in_opentargets"] = False
        n_after_ot = len(de_df)

    if len(de_df) == 0:
        logger.error("Zero genes after OT intersection. Check gene symbol compatibility.")
        _write_empty(sig_dir, disease_name, top_n)
        return

    # --- Apply FDR filter ---
    n_before_fdr = len(de_df)
    if "fdr" in de_df.columns:
        de_df = de_df[de_df["fdr"] <= min_de_fdr].copy()
        logger.info("FDR filter (<= %.2f): %d / %d genes", min_de_fdr, len(de_df), n_before_fdr)

    n_after_fdr = len(de_df)

    if len(de_df) == 0:
        logger.warning("No genes pass FDR filter (<=%.2f). Relaxing to FDR <= 1.0...", min_de_fdr)
        de_df = pd.read_csv(meta_path, sep="\t")
        de_df = de_df.dropna(subset=["meta_logFC", "meta_z"]).copy()
        if ot_df is not None and len(ot_df) > 0:
            ot_genes = set(ot_df["gene_symbol"].dropna().str.upper())
            de_df["feature_id_upper"] = de_df["feature_id"].astype(str).str.upper()
            # Soft prior (same as above — keep all genes)
            ot_lookup = ot_df.drop_duplicates(subset=["gene_symbol"]).set_index(
                ot_df["gene_symbol"].str.upper()
            )["ot_score"]
            de_df["ot_score"] = de_df["feature_id_upper"].map(ot_lookup)
            valid_scores = de_df["ot_score"].dropna()
            fallback_score = max(float(np.percentile(valid_scores, 5)) if len(valid_scores) > 0 else 0.1, 0.05)
            de_df["ot_score"] = de_df["ot_score"].fillna(fallback_score)
            de_df["in_opentargets"] = de_df["feature_id_upper"].isin(ot_genes)
            de_df = de_df.drop(columns=["feature_id_upper"])
        else:
            de_df["ot_score"] = 1.0
            de_df["in_opentargets"] = False
        n_after_fdr = len(de_df)

    # --- Compute weighted score ---
    # Weight = |meta_z| * ot_score
    de_df["weight"] = np.abs(de_df["meta_z"]) * de_df["ot_score"]

    # --- Split up / down by logFC direction ---
    up_df = de_df[de_df["meta_logFC"] > 0].sort_values("weight", ascending=False)
    down_df = de_df[de_df["meta_logFC"] < 0].sort_values("weight", ascending=False)

    actual_top_n = min(top_n, len(up_df), len(down_df))
    if actual_top_n == 0:
        actual_top_n = min(top_n, max(len(up_df), len(down_df)))

    up_selected = up_df.head(min(top_n, len(up_df))).copy()
    down_selected = down_df.head(min(top_n, len(down_df))).copy()

    logger.info("Signature: %d up + %d down genes (requested %d each)",
                len(up_selected), len(down_selected), top_n)

    # --- Clip weights for output ---
    up_selected["weight"] = np.clip(up_selected["weight"], 0, 100)
    down_selected["weight"] = np.clip(down_selected["weight"], 0, 100)

    # --- LINCS coverage pre-check ---
    lincs_coverage = {"checked": False, "reason": "skipped"}
    if not args.skip_lincs_check:
        lincs_coverage = _lincs_coverage_check(
            up_selected["feature_id"].tolist(),
            down_selected["feature_id"].tolist(),
        )

    # --- Gene audit: add assembly-stage counts ---
    audit["stages"].append({
        "step": 5,
        "name": "assemble_signature",
        "genes_from_meta": n_de_valid,
        "genes_after_ot_intersection": n_after_ot,
        "genes_after_fdr_filter": n_after_fdr,
        "up_candidates": len(up_df),
        "down_candidates": len(down_df),
        "up_selected": len(up_selected),
        "down_selected": len(down_selected),
        "detail": (f"Meta({n_de_valid}) → OT∩({n_after_ot}) → FDR({n_after_fdr}) "
                  f"→ {len(up_selected)} up + {len(down_selected)} down"),
    })
    audit["lincs_coverage"] = lincs_coverage
    audit["generated_at"] = datetime.now(timezone.utc).isoformat()

    # Write gene audit
    audit_path = sig_dir / "gene_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    logger.info("Gene audit saved: %s", audit_path)

    # --- Write disease_signature_meta.json ---
    def gene_records(df):
        cols = ["feature_id", "meta_logFC", "meta_z", "weight"]
        if "fdr" in df.columns:
            cols.append("fdr")
        if "ot_score" in df.columns:
            cols.append("ot_score")
        records = df[cols].rename(columns={"feature_id": "gene"}).to_dict(orient="records")
        # Clean NaN values
        for r in records:
            for k, v in r.items():
                if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                    r[k] = None
        return records

    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": disease_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_pipeline": "archs4_signature_pipeline",
        "source_table": "gene_meta",
        "top_n": int(min(top_n, max(len(up_selected), len(down_selected)))),
        "filters": {
            "min_de_fdr": min_de_fdr,
            "opentargets_mode": "soft_prior" if ot_df is not None else "none",
        },
        "qc": {
            "total_de_genes": total_de,
            "genes_in_ot": n_after_ot,
            "genes_after_fdr": n_after_fdr,
            "up_genes_output": len(up_selected),
            "down_genes_output": len(down_selected),
            "lincs_coverage": lincs_coverage,
        },
        "up_genes": gene_records(up_selected),
        "down_genes": gene_records(down_selected),
    }

    sig_json = sig_dir / "disease_signature_meta.json"
    sig_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Saved: %s", sig_json)

    # --- Write sigreverse_input.json ---
    sigreverse = {
        "name": disease_name,
        "up": up_selected["feature_id"].tolist(),
        "down": down_selected["feature_id"].tolist(),
        "meta": {
            "source": "archs4_signature_pipeline",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "OpenTargets-filtered ARCHS4 multi-series DE signature",
        },
    }
    sir_path = sig_dir / "sigreverse_input.json"
    sir_path.write_text(json.dumps(sigreverse, indent=2), encoding="utf-8")
    logger.info("Saved: %s", sir_path)

    # --- Write gene lists ---
    (sig_dir / "up_genes.txt").write_text(
        "\n".join(up_selected["feature_id"].astype(str)), encoding="utf-8")
    (sig_dir / "down_genes.txt").write_text(
        "\n".join(down_selected["feature_id"].astype(str)), encoding="utf-8")

    logger.info("Signature assembly complete: %d up + %d down",
                len(up_selected), len(down_selected))


def _write_empty(sig_dir: Path, name: str, top_n: int):
    """Write empty signature files so pipeline doesn't crash downstream."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_pipeline": "archs4_signature_pipeline",
        "top_n": top_n,
        "qc": {"warning": "No valid genes for signature"},
        "up_genes": [],
        "down_genes": [],
    }
    (sig_dir / "disease_signature_meta.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    (sig_dir / "sigreverse_input.json").write_text(
        json.dumps({"name": name, "up": [], "down": []}, indent=2), encoding="utf-8")
    (sig_dir / "up_genes.txt").write_text("", encoding="utf-8")
    (sig_dir / "down_genes.txt").write_text("", encoding="utf-8")
    logger.warning("Wrote empty signature files.")


if __name__ == "__main__":
    main()
