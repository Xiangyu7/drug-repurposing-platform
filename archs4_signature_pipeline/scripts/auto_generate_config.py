#!/usr/bin/env python3
"""
auto_generate_config.py - Auto-generate ARCHS4 pipeline configs from disease list

Automatically fetches disease synonyms from OpenTargets API so that
ARCHS4 sample search uses the best possible keywords without manual
configuration for each new disease.

Usage:
  python scripts/auto_generate_config.py --disease-list ../../ops/internal/disease_list_day1_dual.txt
  python scripts/auto_generate_config.py --disease nash --disease-name "nonalcoholic steatohepatitis" --efo-id EFO_1001249
"""
import argparse
import logging
import re
from pathlib import Path
from urllib.request import Request, urlopen
import json

import yaml

logger = logging.getLogger("archs4.config")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Extra keywords not in ontology (abbreviations, colloquial terms)
# Extra keywords not found in ontology — only use terms >= 3 chars
# that are unambiguous enough to avoid false matches in sample metadata.
EXTRA_KEYWORDS = {
    # --- Metabolic / Liver ---
    "nash": ["NASH", "MASH", "NAFLD", "fatty liver", "hepatic steatosis", "steatosis"],
    "nafld": ["NAFLD", "MASLD", "NASH", "fatty liver", "hepatic steatosis", "steatosis"],
    "metabolic_syndrome": ["MetS", "insulin resistance", "obesity", "hyperglycemia"],
    # --- Autoimmune / Inflammatory ---
    "lupus": ["SLE", "lupus nephritis", "lupus erythematosus"],
    "psoriasis": ["psoriatic", "psoriasis vulgaris", "plaque psoriasis"],
    "crohns_disease": ["Crohn", "Crohns", "ileitis", "inflammatory bowel"],
    "ankylosing_spondylitis": ["ankylosing", "spondylitis", "axial spondyloarthritis"],
    # --- Neurodegeneration ---
    "alzheimers_disease": ["Alzheimer", "Alzheimers", "amyloid", "tauopathy", "dementia"],
    "parkinsons_disease": ["Parkinson", "Parkinsons", "dopaminergic", "substantia nigra"],
    "als": ["ALS", "motor neuron disease", "motor neuron degeneration", "spinal cord"],
    "huntingtons_disease": ["Huntington", "Huntingtons", "huntingtin", "HTT", "polyglutamine"],
    # --- Fibrosis ---
    "ipf": ["IPF", "lung fibrosis", "pulmonary fibrosis", "interstitial lung disease"],
    "liver_fibrosis": ["liver fibrosis", "hepatic fibrosis", "cirrhosis", "fibrotic liver"],
    "renal_fibrosis": ["renal fibrosis", "kidney fibrosis", "nephrosclerosis", "CKD"],
    # --- Oncology ---
    "pancreatic_cancer": ["PDAC", "pancreatic adenocarcinoma", "pancreatic ductal", "pancreatic tumor"],
    "glioblastoma": ["GBM", "glioma", "brain tumor", "astrocytoma"],
    "triple_negative_breast_cancer": ["TNBC", "triple negative", "basal-like breast cancer"],
    "nsclc": ["NSCLC", "non-small cell lung cancer", "lung adenocarcinoma", "lung squamous cell carcinoma"],
    "melanoma": ["cutaneous melanoma", "malignant melanoma", "BRAF melanoma", "metastatic melanoma"],
    "prostate_cancer": ["prostate adenocarcinoma", "CRPC", "castration-resistant", "prostate tumor"],
    "hepatocellular_carcinoma": ["HCC", "liver cancer", "hepatoma", "hepatocellular"],
    "aml": ["AML", "acute myeloid leukemia", "myeloid leukemia", "myeloblastic leukemia"],
    "ovarian_cancer": ["HGSOC", "high-grade serous ovarian", "ovarian carcinoma", "ovarian tumor"],
    "renal_cell_carcinoma": ["ccRCC", "clear cell renal", "kidney cancer", "renal carcinoma"],
    "gastric_cancer": ["stomach cancer", "gastric adenocarcinoma", "gastric carcinoma", "stomach tumor"],
    "head_neck_cancer": ["HNSCC", "head and neck cancer", "oral squamous cell carcinoma", "oropharyngeal"],
    "multiple_myeloma": ["myeloma", "plasma cell neoplasm", "plasmacytoma", "MM"],
    "cholangiocarcinoma": ["bile duct cancer", "biliary tract cancer", "intrahepatic cholangiocarcinoma", "CCA"],
    # --- Respiratory ---
    "asthma": ["bronchial asthma", "eosinophilic asthma", "airway hyperresponsiveness",
               "allergic asthma", "severe asthma"],
    "bronchiectasis": ["non-cystic fibrosis bronchiectasis", "chronic suppurative lung disease",
                       "bronchial dilatation"],
    # --- Psychiatry / Neurology ---
    "schizophrenia": ["schizophrenic", "psychosis", "antipsychotic",
                      "first-episode psychosis"],
    "bipolar_disorder": ["bipolar", "manic-depressive", "mania", "bipolar depression"],
    # --- Dermatology ---
    "vitiligo": ["depigmentation", "melanocyte", "leukoderma", "repigmentation"],
    # --- Hematology / Oncology ---
    "myelofibrosis": ["primary myelofibrosis", "bone marrow fibrosis",
                      "myeloproliferative neoplasm", "post-polycythemia vera myelofibrosis"],
    "colorectal_cancer": ["colorectal carcinoma", "colon cancer", "rectal cancer",
                          "colorectal adenocarcinoma", "CRC"],
    # --- Existing cardiovascular (unchanged) ---
    "pulmonary_arterial_hypertension": ["PAH"],
    "myocardial_infarction": ["AMI", "STEMI", "NSTEMI"],
    "venous_thromboembolism": ["VTE"],
    "deep_vein_thrombosis": ["DVT"],
    "abdominal_aortic_aneurysm": ["AAA"],
    "heart_failure": ["HFrEF", "HFpEF"],
}


def fetch_opentargets_synonyms(efo_id: str, timeout: float = 10.0) -> list[str]:
    """Fetch disease name + synonyms from OpenTargets GraphQL API."""
    query = """
    query DiseaseInfo($efoId: String!) {
      disease(efoId: $efoId) {
        name
        synonyms { terms }
      }
    }
    """
    payload = json.dumps({"query": query, "variables": {"efoId": efo_id}}).encode()
    req = Request(
        "https://api.platform.opentargets.org/api/v4/graphql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        disease = data.get("data", {}).get("disease")
        if not disease:
            return []
        terms = []
        if disease.get("name"):
            terms.append(disease["name"])
        for syn_group in disease.get("synonyms", []) or []:
            for t in syn_group.get("terms", []) or []:
                if t and t not in terms:
                    terms.append(t)
        return terms
    except Exception as e:
        logger.warning("OpenTargets synonym fetch failed for %s: %s", efo_id, e)
        return []


def _clean_keywords(raw_terms: list[str], disease_name: str, max_keywords: int = 12) -> list[str]:
    """Deduplicate, filter overly long/generic terms, and limit count."""
    seen = set()
    keywords = []
    # Always include the disease name first
    for term in [disease_name] + raw_terms:
        normalized = term.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        # Skip very long synonyms (> 60 chars) — they won't match sample metadata
        if len(normalized) > 60:
            continue
        # Skip very short terms (< 3 chars) — too ambiguous (AD, PE, etc.)
        if len(normalized) < 3:
            continue
        # Skip terms that are just IDs (e.g., "OMIM:123456")
        if re.match(r'^[A-Z]+:\d+$', normalized):
            continue
        seen.add(key)
        keywords.append(normalized)
        if len(keywords) >= max_keywords:
            break
    return keywords


def build_keywords(disease_key: str, disease_name: str, efo_id: str) -> list[str]:
    """Build case keywords: OpenTargets synonyms + extra abbreviations."""
    # 1. Fetch synonyms from OpenTargets
    ot_terms = fetch_opentargets_synonyms(efo_id)
    if ot_terms:
        logger.info("OpenTargets synonyms for %s (%s): %d terms", disease_key, efo_id, len(ot_terms))
    else:
        logger.warning("No OpenTargets synonyms for %s, using disease name only", disease_key)
        ot_terms = [disease_name]

    # 2. Append extra abbreviations/colloquial terms
    extras = EXTRA_KEYWORDS.get(disease_key, [])

    # 3. Clean and deduplicate
    return _clean_keywords(ot_terms + extras, disease_name)


def generate_config(disease_key: str, disease_name: str, efo_id: str,
                    h5_path: str = "data/archs4/human_gene_v2.5.h5",
                    case_keywords: list[str] | None = None) -> dict:
    """Generate a config dict for a single disease."""
    if case_keywords is None:
        case_keywords = build_keywords(disease_key, disease_name, efo_id)

    config = {
        "project": {
            "name": f"{disease_key}_archs4_signature",
            "outdir": f"outputs/{disease_key}",
            "workdir": f"work/{disease_key}",
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
            "weight_formula": "meta_z_times_ot_score_times_1minusFDR",
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
    ap.add_argument("--h5-path", default="data/archs4/human_gene_v2.5.h5",
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
        logger.info("Generated: %s (%s, %s) — %d keywords",
                     out_path, d["name"], d["efo_id"], len(cfg["archs4"]["case_keywords"]))

    logger.info("Generated %d config files in %s", len(diseases), configs_dir)


if __name__ == "__main__":
    main()
