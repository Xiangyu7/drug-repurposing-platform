#!/usr/bin/env python3
"""
08_make_signature_json.py - Generate final disease signature (industrial grade)

Improvements:
  - schema_version in JSON output
  - Timestamp and provenance metadata
  - Output compatible with sigreverse input format
  - Additional "sigreverse_input.json" for direct pipeline handoff
  - Empty result handling with clear error messages
  - QC statistics in output
"""
import argparse, json
from pathlib import Path
from datetime import datetime, timezone

import yaml
import pandas as pd
import numpy as np
from rich import print


SCHEMA_VERSION = "2.0"


def main():
    ap = argparse.ArgumentParser(description="Generate disease signature JSON from meta-analysis")
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    outdir = Path(cfg["project"]["outdir"])
    top_n = int(cfg["meta"]["top_n"])
    min_sign = float(cfg["meta"]["min_sign_concordance"])

    # --- Load gene data ---
    # Try ensemble (combined meta + RRA) first; fall back to gene_meta
    ens_path = outdir / "signature" / "gene_meta_ensemble.tsv"
    meta_path = outdir / "signature" / "gene_meta.tsv"

    df = None
    source_table = None

    if ens_path.exists():
        df_ens = pd.read_csv(ens_path, sep="\t")
        df_ens = df_ens.dropna(subset=["meta_logFC", "meta_z", "fdr"]).copy()
        if len(df_ens) > 0:
            df = df_ens
            source_table = "gene_meta_ensemble"
            if "ensemble_rank_up" in df.columns:
                df["signed_score"] = np.where(df["meta_logFC"] > 0,
                                              -df["ensemble_rank_up"],
                                              df["ensemble_rank_up"])
            else:
                df["signed_score"] = -df["meta_z"]
            print(f"[cyan]Using ensemble table: {len(df)} genes[/cyan]")
        else:
            print("[yellow]Ensemble file exists but is empty, falling back to gene_meta[/yellow]")

    if df is None:
        if not meta_path.exists():
            raise SystemExit(f"Neither ensemble nor gene_meta file found in {outdir / 'signature'}")
        df = pd.read_csv(meta_path, sep="\t")
        df = df.dropna(subset=["meta_logFC", "meta_z", "fdr"]).copy()
        source_table = "gene_meta"
        df["signed_score"] = -df["meta_z"]
        print(f"[cyan]Using gene_meta table: {len(df)} genes[/cyan]")

    # --- Validate we have data ---
    if len(df) == 0:
        print("[red]ERROR: No genes with valid meta_logFC/meta_z/fdr.[/red]")
        print("[red]This usually means meta-analysis produced all NAs.[/red]")
        print("[red]Check: do your GSE datasets share the same gene/probe IDs?[/red]")
        # Still produce output files (empty) so pipeline doesn't crash
        sig_dir = outdir / "signature"
        sig_dir.mkdir(parents=True, exist_ok=True)
        empty_payload = {
            "schema_version": SCHEMA_VERSION,
            "name": cfg["project"]["name"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "top_n": top_n,
            "filters": {"min_sign_concordance": min_sign},
            "qc": {"total_genes_input": 0, "genes_after_filter": 0, "warning": "No valid genes"},
            "up_genes": [],
            "down_genes": [],
        }
        (sig_dir / "disease_signature_meta.json").write_text(
            json.dumps(empty_payload, indent=2), encoding="utf-8")
        (sig_dir / "up_genes.txt").write_text("", encoding="utf-8")
        (sig_dir / "down_genes.txt").write_text("", encoding="utf-8")
        _write_sigreverse_input(sig_dir, cfg["project"]["name"], [], [])
        print("[yellow]Wrote empty signature files.[/yellow]")
        return

    total_input = len(df)

    if "sign_concordance" in df.columns:
        df = df[df["sign_concordance"] >= min_sign]

    genes_after_filter = len(df)

    # --- Select top genes ---
    # signed_score: negative values = UP in disease, positive values = DOWN in disease
    # (because signed_score = -meta_z, and meta_z > 0 means up in disease)
    # So: sort ascending → head = most negative = most UP-regulated
    #     tail = most positive = most DOWN-regulated
    df = df.sort_values("signed_score")
    actual_top_n = min(top_n, len(df) // 2) if len(df) >= 2 else len(df)

    # head() = most negative signed_score = most positive meta_z = UP-regulated in disease
    up = df.head(actual_top_n).copy()
    # tail() = most positive signed_score = most negative meta_z = DOWN-regulated in disease
    down = df.tail(actual_top_n).copy()

    # Validate direction
    if len(up) > 0 and len(down) > 0:
        if "meta_logFC" in up.columns:
            up_mean_lfc = up["meta_logFC"].mean()
            down_mean_lfc = down["meta_logFC"].mean()
            if up_mean_lfc < down_mean_lfc:
                print("[yellow]WARNING: Up-gene mean logFC < Down-gene mean logFC. "
                      "Swapping directions.[/yellow]")
                up, down = down, up

    sig_dir = outdir / "signature"
    sig_dir.mkdir(parents=True, exist_ok=True)

    up["weight"] = np.clip(np.abs(up["meta_z"]), 0, 10)
    down["weight"] = np.clip(np.abs(down["meta_z"]), 0, 10)

    # --- Write gene lists ---
    (sig_dir / "up_genes.txt").write_text(
        "\n".join(up["feature_id"].astype(str)), encoding="utf-8")
    (sig_dir / "down_genes.txt").write_text(
        "\n".join(down["feature_id"].astype(str)), encoding="utf-8")

    # --- Main JSON output (detailed, with provenance) ---
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": cfg["project"]["name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_table": source_table,
        "top_n": actual_top_n,
        "filters": {"min_sign_concordance": min_sign},
        "qc": {
            "total_genes_input": total_input,
            "genes_after_filter": genes_after_filter,
            "up_genes_output": len(up),
            "down_genes_output": len(down),
        },
        "up_genes": up[["feature_id", "meta_logFC", "meta_z", "fdr", "weight"]].rename(
            columns={"feature_id": "gene"}).to_dict(orient="records"),
        "down_genes": down[["feature_id", "meta_logFC", "meta_z", "fdr", "weight"]].rename(
            columns={"feature_id": "gene"}).to_dict(orient="records"),
    }
    (sig_dir / "disease_signature_meta.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    # --- sigreverse-compatible output ---
    _write_sigreverse_input(
        sig_dir, cfg["project"]["name"],
        up["feature_id"].tolist(), down["feature_id"].tolist()
    )

    print(f"[green]Saved signature:[/green] {len(up)} up + {len(down)} down genes")
    print(f"  disease_signature_meta.json (schema v{SCHEMA_VERSION})")
    print(f"  sigreverse_input.json (ready for sigreverse pipeline)")


def _write_sigreverse_input(sig_dir: Path, name: str, up_genes: list, down_genes: list):
    """Write a JSON file directly compatible with sigreverse input format."""
    sigreverse_payload = {
        "name": name,
        "up": up_genes,
        "down": down_genes,
        "meta": {
            "source": "dsmeta_signature_pipeline",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Auto-generated from multi-GSE meta-analysis",
        }
    }
    (sig_dir / "sigreverse_input.json").write_text(
        json.dumps(sigreverse_payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
