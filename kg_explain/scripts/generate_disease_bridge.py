#!/usr/bin/env python3
"""Generate an origin-disease reassessment bridge CSV.

Generic script — works for any target disease, not hardcoded to a specific one.
Switch diseases by changing ``--disease`` or ``--disease-ids``.

Usage examples:
    # Atherosclerosis (fuzzy name match)
    python scripts/generate_disease_bridge.py \\
        --disease atherosclerosis \\
        --inject configs/inject_atherosclerosis.yaml \\
        --out output/bridge_origin_reassess.csv

    # Type 2 diabetes
    python scripts/generate_disease_bridge.py \\
        --disease "type 2 diabetes" \\
        --out output/bridge_origin_reassess.csv

    # Exact disease IDs
    python scripts/generate_disease_bridge.py \\
        --disease-ids EFO_0003914,MONDO_0021661 \\
        --out output/bridge_origin_reassess.csv
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yaml

# Allow importing from kg_explain package
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root / "src"))

from kg_explain.rankers.uncertainty import assign_confidence_tier, bootstrap_ci

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_drug_id(name: str) -> str:
    """Same algorithm as v5.py / LLM+RAG step5: D + md5[:10].upper()."""
    return "D" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10].upper()


def _infer_endpoint_type(condition: str) -> str:
    """Heuristic endpoint_type from trial condition text."""
    if not condition:
        return "OTHER"
    c = condition.lower()
    cv_kw = ["cardiovascular", "myocardial", "coronary", "heart", "stroke",
             "mace", "cardiac", "ischemic", "angina", "infarction", "cv event"]
    img_kw = ["plaque", "imaging", "intima", "carotid", "ivus", "oct"]
    lipid_kw = ["ldl", "cholesterol", "lipid", "lipoprotein"]
    inflam_kw = ["crp", "il-1", "il-6", "inflammation", "inflammatory"]
    for kw in cv_kw:
        if kw in c:
            return "CV_EVENTS"
    for kw in img_kw:
        if kw in c:
            return "PLAQUE_IMAGING"
    for kw in lipid_kw:
        if kw in c:
            return "LIPID"
    for kw in inflam_kw:
        if kw in c:
            return "INFLAMMATION"
    return "OTHER"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def load_inject_drugs(inject_path: Optional[str]) -> List[dict]:
    """Load literature-inject drug list from YAML."""
    if not inject_path:
        return []
    p = Path(inject_path)
    if not p.exists():
        logger.warning("Inject file not found: %s — skipping", p)
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        logger.warning("Inject file should be a YAML list — skipping")
        return []
    logger.info("Loaded %d literature-inject drugs from %s", len(data), p.name)
    return data


def match_disease_rows(
    v3: pd.DataFrame,
    disease_name: Optional[str],
    disease_ids: Optional[str],
) -> pd.DataFrame:
    """Filter V3 rows matching the target disease."""
    if disease_ids:
        id_list = [x.strip() for x in disease_ids.split(",") if x.strip()]
        mask = v3["diseaseId"].isin(id_list)
        matched = v3[mask].copy()
        logger.info(
            "Disease ID filter: %d IDs → %d rows (%d drugs)",
            len(id_list), len(matched), matched["drug_normalized"].nunique(),
        )
    elif disease_name:
        mask = v3["diseaseName"].str.contains(disease_name, case=False, na=False)
        matched = v3[mask].copy()
        ids_found = matched["diseaseId"].unique().tolist()
        logger.info(
            "Fuzzy match '%s': %d disease IDs %s → %d rows (%d drugs)",
            disease_name, len(ids_found), ids_found,
            len(matched), matched["drug_normalized"].nunique(),
        )
    else:
        logger.error("Must provide --disease or --disease-ids")
        sys.exit(1)

    if matched.empty:
        logger.error("No V3 rows match the target disease — check spelling or IDs")
        sys.exit(1)

    return matched


def build_bridge(
    v3_path: str,
    v5_path: str,
    paths_path: str,
    chembl_path: str,
    bridge_cross_path: Optional[str],
    disease_name: Optional[str],
    disease_ids: Optional[str],
    inject_drugs: List[dict],
    n_bootstrap: int = 1000,
    ci_level: float = 0.95,
    seed: int = 42,
) -> pd.DataFrame:
    """Build the origin-disease reassessment bridge DataFrame."""

    # 1. Load V3
    v3 = pd.read_csv(v3_path)
    logger.info("V3 loaded: %d rows, %d drugs", len(v3), v3["drug_normalized"].nunique())

    # 2. Filter to target disease
    disease_rows = match_disease_rows(v3, disease_name, disease_ids)

    # 3. Per-drug aggregation: MAX mechanism_score across sub-disease variants
    drug_agg = (
        disease_rows.sort_values("mechanism_score", ascending=False)
        .groupby("drug_normalized", as_index=False)
        .first()
    )
    logger.info("After per-drug MAX aggregation: %d drugs", len(drug_agg))

    # 4. Load ChEMBL pref_name mapping
    chembl_map: Dict[str, str] = {}
    cp = Path(chembl_path)
    if cp.exists():
        cm = pd.read_csv(cp, dtype=str)
        for _, r in cm.iterrows():
            canon = str(r.get("canonical_name", "")).strip()
            pref = str(r.get("chembl_pref_name", "")).strip()
            if canon and pref and pref != "nan":
                chembl_map[canon] = pref

    # 5. Load V5 per-drug penalties (median across all diseases)
    penalty_map: Dict[str, dict] = {}
    v5p = Path(v5_path)
    if v5p.exists() and v5p.stat().st_size > 100:
        v5 = pd.read_csv(v5p)
        for drug, grp in v5.groupby("drug_normalized"):
            penalty_map[str(drug)] = {
                "safety_penalty": float(grp["safety_penalty"].median()),
                "trial_penalty": float(grp["trial_penalty"].median()),
                "risk_multiplier": float(grp["risk_multiplier"].median()),
                "phenotype_boost": float(grp["phenotype_boost"].median()),
                "phenotype_multiplier": float(grp["phenotype_multiplier"].median()),
            }
        logger.info("V5 penalties loaded for %d drugs", len(penalty_map))

    # 6. Load global max scores from existing cross-disease bridge
    global_max: Dict[str, float] = {}
    if bridge_cross_path and Path(bridge_cross_path).exists():
        bc = pd.read_csv(bridge_cross_path)
        for _, r in bc.iterrows():
            canon = str(r.get("canonical_name", "")).strip()
            if canon:
                global_max[canon] = float(r.get("max_mechanism_score", 0))
        logger.info("Global max scores loaded for %d drugs from bridge_repurpose_cross", len(global_max))

    # 7. Load evidence paths and compute bootstrap CI
    pair_scores: Dict[str, List[float]] = defaultdict(list)
    disease_id_set = set(disease_rows["diseaseId"].unique())
    pp = Path(paths_path)
    if pp.exists():
        with open(pp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("diseaseId") in disease_id_set:
                    drug = str(d.get("drug", "")).lower().strip()
                    ps = d.get("path_score")
                    if ps is not None:
                        pair_scores[drug].append(float(ps))
        logger.info("Evidence paths: %d drugs with %d total paths",
                     len(pair_scores), sum(len(v) for v in pair_scores.values()))

    # 8. Build bridge rows from KG
    bridge_rows = []
    for _, r in drug_agg.iterrows():
        drug = str(r["drug_normalized"]).strip()
        mech = float(r.get("mechanism_score", 0))
        pen = penalty_map.get(drug, {})
        risk_mult = pen.get("risk_multiplier", 1.0)
        pheno_mult = pen.get("phenotype_multiplier", 1.0)
        final = mech * risk_mult * pheno_mult

        # Bootstrap CI
        scores = pair_scores.get(drug.lower(), [])
        if scores:
            ci_result = bootstrap_ci(scores, n_bootstrap=n_bootstrap, ci=ci_level, seed=seed)
        else:
            ci_result = {"ci_lower": 0.0, "ci_upper": 0.0, "ci_width": 0.0, "n_paths": 0}

        condition = str(r.get("example_condition", "")) if "example_condition" in r.index else ""

        bridge_rows.append({
            "drug_id": _stable_drug_id(drug),
            "canonical_name": drug,
            "chembl_pref_name": chembl_map.get(drug, ""),
            "max_mechanism_score": round(mech, 4),
            "max_mechanism_score_global": round(global_max.get(drug, mech), 4),
            "top_disease": str(r.get("diseaseName", "")),
            "final_score": round(final, 4),
            "endpoint_type": _infer_endpoint_type(condition),
            "n_trials": r.get("n_trials", ""),
            "trial_statuses": r.get("trial_statuses", ""),
            "trial_source": r.get("trial_source", ""),
            "example_condition": condition,
            "why_stopped": r.get("example_whyStopped", "") if "example_whyStopped" in r.index else "",
            "ci_lower": ci_result["ci_lower"],
            "ci_upper": ci_result["ci_upper"],
            "ci_width": ci_result["ci_width"],
            "confidence_tier": assign_confidence_tier(ci_result["ci_width"]),
            "n_evidence_paths": ci_result.get("n_paths", len(scores)),
            "source": "kg",
        })

    # 9. Add literature-inject drugs (if not already in KG set)
    kg_drugs = {row["canonical_name"].lower() for row in bridge_rows}
    for inj in inject_drugs:
        name = str(inj.get("name", "")).strip().lower()
        if not name:
            continue
        if name in kg_drugs:
            # Drug already in KG — mark source as "kg+literature"
            for row in bridge_rows:
                if row["canonical_name"].lower() == name:
                    row["source"] = "kg+literature"
            continue
        bridge_rows.append({
            "drug_id": _stable_drug_id(name),
            "canonical_name": name,
            "chembl_pref_name": chembl_map.get(name, name.upper()),
            "max_mechanism_score": 0.0,
            "max_mechanism_score_global": round(global_max.get(name, 0.0), 4),
            "top_disease": disease_name or "",
            "final_score": 0.0,
            "endpoint_type": inj.get("endpoint_type", "OTHER"),
            "n_trials": "",
            "trial_statuses": "",
            "trial_source": "",
            "example_condition": inj.get("note", ""),
            "why_stopped": "",
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "ci_width": 0.0,
            "confidence_tier": "LOW",
            "n_evidence_paths": 0,
            "source": "literature",
        })
        logger.info("  +inject: %s (source=literature, endpoint=%s)", name, inj.get("endpoint_type", "OTHER"))

    # 10. Build DataFrame, deduplicate salt forms (keep highest score)
    df = pd.DataFrame(bridge_rows)
    df = df.sort_values("max_mechanism_score", ascending=False)

    n_kg = len(df[df["source"] == "kg"])
    n_lit = len(df[df["source"] == "literature"])
    n_both = len(df[df["source"] == "kg+literature"])
    logger.info(
        "Bridge built: %d drugs total (KG=%d, literature=%d, KG+literature=%d)",
        len(df), n_kg, n_lit, n_both,
    )

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Generate origin-disease reassessment bridge CSV (generic for any disease)"
    )
    parser.add_argument("--disease", type=str, default=None,
                        help="Target disease name (fuzzy match, e.g. 'atherosclerosis')")
    parser.add_argument("--disease-ids", type=str, default=None,
                        help="Comma-separated disease IDs (e.g. EFO_0003914,MONDO_0021661)")
    parser.add_argument("--v3", type=str,
                        default=str(_project_root / "output" / "drug_disease_rank_v3.csv"),
                        help="V3 ranking CSV path")
    parser.add_argument("--v5", type=str,
                        default=str(_project_root / "output" / "drug_disease_rank_v5.csv"),
                        help="V5 ranking CSV path")
    parser.add_argument("--paths", type=str,
                        default=str(_project_root / "output" / "evidence_paths_v3.jsonl"),
                        help="Evidence paths JSONL path")
    parser.add_argument("--chembl", type=str,
                        default=str(_project_root / "data" / "drug_chembl_map.csv"),
                        help="ChEMBL mapping CSV path")
    parser.add_argument("--bridge-cross", type=str,
                        default=str(_project_root / "output" / "bridge_repurpose_cross.csv"),
                        help="Cross-disease bridge CSV (for global max scores)")
    parser.add_argument("--inject", type=str, default=None,
                        help="YAML file with literature-inject drug list")
    parser.add_argument("--out", type=str,
                        default=str(_project_root / "output" / "bridge_origin_reassess.csv"),
                        help="Output bridge CSV path")
    parser.add_argument("--n-bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)

    args = parser.parse_args()

    if not args.disease and not args.disease_ids:
        parser.error("Must provide --disease or --disease-ids")

    df = build_bridge(
        v3_path=args.v3,
        v5_path=args.v5,
        paths_path=args.paths,
        chembl_path=args.chembl,
        bridge_cross_path=args.bridge_cross,
        disease_name=args.disease,
        disease_ids=args.disease_ids,
        inject_drugs=load_inject_drugs(args.inject),
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    logger.info("Bridge CSV written to %s (%d rows)", out, len(df))

    # Summary
    print(f"\n{'='*60}")
    print(f"Origin-Disease Reassessment Bridge: {len(df)} drugs")
    print(f"  Disease filter: {args.disease or args.disease_ids}")
    print(f"  KG drugs:        {len(df[df['source'] == 'kg'])}")
    print(f"  Literature drugs: {len(df[df['source'] == 'literature'])}")
    print(f"  KG+Literature:    {len(df[df['source'] == 'kg+literature'])}")
    print(f"  Output: {out}")
    print(f"{'='*60}")
    print(f"\nTop 10 by mechanism score:")
    top = df.head(10)[["canonical_name", "max_mechanism_score", "top_disease", "source", "confidence_tier"]]
    print(top.to_string(index=False))


if __name__ == "__main__":
    main()
