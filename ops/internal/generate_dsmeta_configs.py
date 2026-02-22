#!/usr/bin/env python3
"""
generate_dsmeta_configs.py — Batch generate dsmeta YAML configs from auto_discover_geo results
==============================================================================================

Reads the auto_discover_geo.py output (geo_curation/<disease>/selected.tsv) and
generates final dsmeta configs ready for the pipeline.

Usage:
  # Generate configs for all discovered diseases
  python generate_dsmeta_configs.py --geo-dir geo_curation --config-dir ../dsmeta_signature_pipeline/configs

  # Generate for specific disease
  python generate_dsmeta_configs.py --geo-dir geo_curation --config-dir ../dsmeta_signature_pipeline/configs --disease heart_failure

  # Dry run (show what would be generated)
  python generate_dsmeta_configs.py --geo-dir geo_curation --config-dir ../dsmeta_signature_pipeline/configs --dry-run

  # Also update disease_list_day1_dual.txt
  python generate_dsmeta_configs.py --geo-dir geo_curation --config-dir ../dsmeta_signature_pipeline/configs --update-disease-list
"""

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import yaml

TEMPLATE = {
    "de": {
        "method": "limma",
        "covariates": [],
        "qc": {"remove_outliers": True, "pca_outlier_z": 3.5},
    },
    "probe_to_gene": {"enable": True, "skip_if_gene_symbols": True},
    "meta": {
        "model": "random",
        "min_sign_concordance": 0.7,
        "flag_i2_above": 0.6,
        "top_n": 300,
    },
    "rank_aggregation": {
        "enable": True,
        "method": "rra",
        "ensemble": {"enable": True, "w_meta": 0.7, "w_rra": 0.3},
    },
    "genesets": {
        "enable_reactome": True,
        "enable_wikipathways": True,
        "enable_kegg": False,
    },
    "gsea": {"method": "fgsea", "min_size": 15, "max_size": 500, "nperm": 10000},
    "pathway_meta": {"method": "stouffer", "min_concordance": 0.7},
    "report": {"enable": True},
}


def read_selected_tsv(path: Path) -> list:
    """Read selected.tsv from auto_discover_geo output."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(row)
    return rows


def generate_config(disease_key: str, selected_rows: list) -> dict:
    """Generate a dsmeta config dict from selected GSE rows."""
    config = {
        "project": {
            "name": f"{disease_key}_meta_signature",
            "outdir": f"outputs/{disease_key}",
            "workdir": f"work/{disease_key}",
            "seed": 13,
        },
        "geo": {
            "gse_list": [],
            "prefer_series_matrix": True,
        },
        "labeling": {
            "mode": "regex",
            "regex_rules": {},
        },
    }

    for row in selected_rows:
        gse_id = row.get("gse_id", "")
        case_rule = row.get("case_rule", "")
        control_rule = row.get("control_rule", "")

        if not gse_id:
            continue

        config["geo"]["gse_list"].append(gse_id)
        config["labeling"]["regex_rules"][gse_id] = {
            "case": {"any": [case_rule] if case_rule else ["TODO"]},
            "control": {"any": [control_rule] if control_rule else ["TODO"]},
        }

    # Merge template
    for k, v in TEMPLATE.items():
        config[k] = v

    return config


def has_todo(config: dict) -> bool:
    """Check if config has any TODO placeholders."""
    text = yaml.dump(config)
    return "TODO" in text


def main():
    parser = argparse.ArgumentParser(description="Batch generate dsmeta configs from GEO discovery")
    parser.add_argument("--geo-dir", type=str, required=True, help="geo_curation output directory")
    parser.add_argument("--config-dir", type=str, required=True, help="dsmeta configs directory")
    parser.add_argument("--disease", type=str, help="Generate for specific disease only")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing configs")
    parser.add_argument("--update-disease-list", action="store_true",
                        help="Update disease_list_day1_dual.txt with ready diseases")
    parser.add_argument("--min-confidence", choices=["high", "medium", "low"], default="medium",
                        help="Minimum confidence to include a GSE (default: medium)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    geo_dir = Path(args.geo_dir)
    config_dir = Path(args.config_dir)

    if not geo_dir.exists():
        logging.error(f"GEO directory not found: {geo_dir}")
        sys.exit(1)

    config_dir.mkdir(parents=True, exist_ok=True)

    # Find all disease directories with selected.tsv
    if args.disease:
        disease_dirs = [geo_dir / args.disease]
    else:
        disease_dirs = sorted([d for d in geo_dir.iterdir() if d.is_dir() and (d / "selected.tsv").exists()])

    if not disease_dirs:
        logging.warning("No diseases found with selected.tsv")
        sys.exit(0)

    generated = []
    skipped = []

    for disease_dir in disease_dirs:
        disease_key = disease_dir.name
        selected_path = disease_dir / "selected.tsv"

        if not selected_path.exists():
            logging.warning(f"{disease_key}: no selected.tsv, skipping")
            skipped.append((disease_key, "no selected.tsv"))
            continue

        rows = read_selected_tsv(selected_path)
        if not rows:
            logging.warning(f"{disease_key}: empty selected.tsv, skipping")
            skipped.append((disease_key, "empty selected.tsv"))
            continue

        # Filter by confidence
        conf_order = {"high": 3, "medium": 2, "low": 1}
        min_conf = conf_order.get(args.min_confidence, 2)
        # Note: selected.tsv from auto_discover uses 'note' field with confidence info
        # For now, include all rows from selected.tsv (they're already pre-filtered)

        n_gse = len(rows)

        # Warn about low GSE count
        if n_gse == 1:
            logging.warning(f"{disease_key}: only 1 GSE — meta-analysis will degrade to single-study. "
                            f"Consider manual GEO search for more datasets, or use Direction B only.")
        elif n_gse >= 2:
            logging.info(f"{disease_key}: {n_gse} GSEs — sufficient for meta-analysis")

        config = generate_config(disease_key, rows)
        config_path = config_dir / f"{disease_key}.yaml"

        if config_path.exists() and not args.overwrite:
            logging.info(f"{disease_key}: config exists, skipping (use --overwrite to replace)")
            skipped.append((disease_key, "config exists"))
            continue

        if args.dry_run:
            logging.info(f"[DRY RUN] Would write: {config_path}")
            logging.info(f"  GSE list: {config['geo']['gse_list']}")
            todo = has_todo(config)
            logging.info(f"  Has TODO: {todo}")
            generated.append((disease_key, len(config["geo"]["gse_list"]), todo))
            continue

        # Write config
        header = (
            f"# Generated by generate_dsmeta_configs.py\n"
            f"# Disease: {disease_key}\n"
            f"# Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# Source: {selected_path}\n"
            f"# GSE count: {len(config['geo']['gse_list'])}\n"
        )
        if has_todo(config):
            header += "# ⚠️  Contains TODO placeholders — review regex rules before running!\n"
        header += "\n"

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        todo = has_todo(config)
        generated.append((disease_key, len(config["geo"]["gse_list"]), todo))
        logging.info(f"  Wrote: {config_path} ({len(config['geo']['gse_list'])} GSEs, TODO={'yes' if todo else 'no'})")

    # Update disease list
    if args.update_disease_list and not args.dry_run:
        ready = [(key, n) for key, n, todo in generated if not todo and n >= 2]
        if ready:
            ops_dir = geo_dir.parent if geo_dir.name == "geo_curation" else geo_dir.parent / "ops"
            list_path = ops_dir / "disease_list_day1_dual.txt"

            with open(list_path, "w", encoding="utf-8") as f:
                f.write("# disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)\n")
                f.write(f"# Auto-generated {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Diseases with dsmeta config ready (no TODO, >= 2 GSE)\n")
                for key, n in ready:
                    query = key.replace("_", " ")
                    f.write(f"{key}|{query}||\n")
            logging.info(f"Updated disease list: {list_path} ({len(ready)} diseases)")

    # Categorized summary
    ready_list = [(key, n) for key, n, todo in generated if not todo and n >= 2]
    single_gse = [(key, n) for key, n, todo in generated if not todo and n == 1]
    has_todo_list = [(key, n) for key, n, todo in generated if todo]

    print(f"\n{'='*60}")
    print(f"Summary — Route Recommendations")
    print(f"{'='*60}")

    if ready_list:
        print(f"\n✅ Direction A READY ({len(ready_list)} diseases, ≥2 GSE, no TODO):")
        for key, n in ready_list:
            print(f"   {key}: {n} GSEs → dual mode (Direction A + B)")

    if single_gse:
        print(f"\n⚠️  Direction A LOW CONFIDENCE ({len(single_gse)} diseases, only 1 GSE):")
        for key, n in single_gse:
            print(f"   {key}: {n} GSE → can run but single-study only, no cross-validation")
        print(f"   → Consider: manual GEO search, or skip Direction A for these diseases")

    if has_todo_list:
        print(f"\n🔧 NEEDS REVIEW ({len(has_todo_list)} diseases, has TODO placeholders):")
        for key, n in has_todo_list:
            print(f"   {key}: {n} GSEs → fix TODO regex in config before running")

    if skipped:
        print(f"\n⏭️  Skipped ({len(skipped)}):")
        for key, reason in skipped:
            print(f"   {key}: {reason}")

    # Diseases with 0 GSE (only in discovery, not in generated)
    zero_gse = [(key, n) for key, n, _ in generated if n == 0]
    if zero_gse:
        print(f"\n❌ Direction B ONLY ({len(zero_gse)} diseases, 0 GSE found):")
        for key, n in zero_gse:
            print(f"   {key} → no GEO expression data, skip Direction A")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    main()
