#!/usr/bin/env python3
"""
auto_generate_config.py - Auto-generate ARCHS4 pipeline configs from disease list

Reads disease_list.txt (or any disease list file) and generates per-disease
YAML configs for the ARCHS4 signature pipeline.

Usage:
  python scripts/auto_generate_config.py --disease-list ../../ops/internal/disease_list_day1_dual.txt
  python scripts/auto_generate_config.py --disease-list ../../ops/internal/disease_list_b_only.txt
  python scripts/auto_generate_config.py --disease atherosclerosis --efo-id EFO_0003914
"""
import argparse
import logging
from pathlib import Path

import yaml

logger = logging.getLogger("archs4.config")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Disease-specific case keywords (beyond the disease name itself)
DISEASE_KEYWORD_MAP = {
    "atherosclerosis": ["atherosclerosis", "atherosclerotic", "plaque", "stenosis"],
    "heart_failure": ["heart failure", "cardiac failure", "cardiomyopathy", "HFrEF", "HFpEF"],
    "coronary_artery_disease": ["coronary artery disease", "coronary heart disease", "CAD", "CHD"],
    "myocardial_infarction": ["myocardial infarction", "heart attack", "acute MI", "AMI", "STEMI", "NSTEMI"],
    "atrial_fibrillation": ["atrial fibrillation", "AF", "AFib"],
    "hypertension": ["hypertension", "high blood pressure", "essential hypertension"],
    "stroke": ["stroke", "ischemic stroke", "cerebral infarction", "cerebrovascular"],
    "deep_vein_thrombosis": ["deep vein thrombosis", "DVT", "venous thrombosis"],
    "venous_thromboembolism": ["venous thromboembolism", "VTE", "pulmonary embolism", "DVT"],
    "angina_pectoris": ["angina", "angina pectoris", "chest pain"],
    "myocarditis": ["myocarditis", "cardiac inflammation"],
    "pulmonary_embolism": ["pulmonary embolism", "PE", "pulmonary thromboembolism"],
    "endocarditis": ["endocarditis", "infective endocarditis"],
    "pulmonary_arterial_hypertension": ["pulmonary arterial hypertension", "PAH", "pulmonary hypertension"],
    "cardiomyopathy": ["cardiomyopathy", "dilated cardiomyopathy", "hypertrophic cardiomyopathy", "DCM", "HCM"],
    "abdominal_aortic_aneurysm": ["abdominal aortic aneurysm", "AAA", "aortic aneurysm"],
}


def generate_config(disease_key: str, disease_name: str, efo_id: str,
                    h5_path: str = "archs4_signature_pipeline/data/archs4/human_gene_v2.4.h5") -> dict:
    """Generate a config dict for a single disease."""
    # Get disease-specific keywords or use disease name
    case_keywords = DISEASE_KEYWORD_MAP.get(disease_key, [disease_name])

    config = {
        "project": {
            "name": f"{disease_key}_archs4_signature",
            "outdir": f"archs4_signature_pipeline/outputs/{disease_key}",
            "workdir": f"archs4_signature_pipeline/work/{disease_key}",
            "seed": 42,
        },
        "disease": {
            "name": disease_name,
            "efo_id": efo_id,
        },
        "archs4": {
            "h5_path": h5_path,
            "min_samples_per_group": 3,
            "max_samples_per_group": 50,
            "max_series": 5,
            "case_keywords": case_keywords,
            "control_keywords": ["normal", "healthy", "control"],
        },
        "opentargets": {
            "min_association_score": 0.1,
        },
        "de": {
            "method": "deseq2",
            "min_count": 10,
            "min_samples": 3,
        },
        "meta": {
            "model": "DL",
            "min_sign_concordance": 0.8,
            "flag_i2_above": 0.75,
        },
        "signature": {
            "top_n": 300,
            "min_de_fdr": 0.5,
            "weight_formula": "meta_z_times_ot_score",
        },
    }
    return config


def parse_disease_list(list_path: str) -> list[dict]:
    """Parse disease list file (same format as ops/disease_list*.txt)."""
    diseases = []
    with open(list_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                logger.warning("Skipping invalid line: %s", line)
                continue
            disease_key = parts[0].strip()
            disease_name = parts[1].strip() if parts[1].strip() else disease_key.replace("_", " ")
            efo_id = parts[2].strip() if len(parts) > 2 else ""
            if not efo_id:
                logger.warning("No EFO ID for %s, skipping", disease_key)
                continue
            diseases.append({
                "key": disease_key,
                "name": disease_name,
                "efo_id": efo_id,
            })
    return diseases


def main():
    ap = argparse.ArgumentParser(description="Auto-generate ARCHS4 pipeline configs")
    ap.add_argument("--disease-list", help="Path to disease list file")
    ap.add_argument("--disease", help="Single disease key")
    ap.add_argument("--disease-name", help="Disease display name (with --disease)")
    ap.add_argument("--efo-id", help="EFO ID (with --disease)")
    ap.add_argument("--h5-path", default="archs4_signature_pipeline/data/archs4/human_gene_v2.4.h5",
                    help="Path to ARCHS4 H5 file")
    ap.add_argument("--outdir", default="archs4_signature_pipeline/configs",
                    help="Output directory for config files")
    args = ap.parse_args()

    configs_dir = Path(args.outdir)
    configs_dir.mkdir(parents=True, exist_ok=True)

    diseases = []

    if args.disease_list:
        diseases = parse_disease_list(args.disease_list)
    elif args.disease:
        if not args.efo_id:
            logger.error("--efo-id required with --disease")
            raise SystemExit(1)
        name = args.disease_name or args.disease.replace("_", " ")
        diseases = [{"key": args.disease, "name": name, "efo_id": args.efo_id}]
    else:
        logger.error("Either --disease-list or --disease required")
        raise SystemExit(1)

    for d in diseases:
        cfg = generate_config(d["key"], d["name"], d["efo_id"], h5_path=args.h5_path)
        out_path = configs_dir / f"{d['key']}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info("Generated: %s (%s, %s)", out_path, d["name"], d["efo_id"])

    logger.info("Generated %d config files in %s", len(diseases), configs_dir)


if __name__ == "__main__":
    main()
