"""Multi-disease pipeline validation with parameterized gold standards.

Runs SigReverse v0.3.1 pipeline on cached LDP3 data for multiple diseases
and evaluates each against its gold standard drug set.

Usage:
    # Run all diseases
    python scripts/validate_multi_disease.py --all

    # Run a single disease
    python scripts/validate_multi_disease.py --disease atherosclerosis

    # Run specific diseases
    python scripts/validate_multi_disease.py --disease atherosclerosis breast_cancer_er
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import logging
import time
from typing import Dict, Set, Tuple

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigreverse.scoring import compute_signature_score, ScoringMode
from sigreverse.robustness import aggregate_to_drug
from sigreverse.cmap_algorithms import CMapPipeline, LDP3ESProvider
from sigreverse.dose_response import analyze_dose_response
from sigreverse.qc import signature_qc_summary
from sigreverse.evaluation.metrics import evaluate_ranking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("validate_multi")


# ---------------------------------------------------------------------------
# Disease Registry
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DISEASE_REGISTRY = {
    "atherosclerosis": {
        "signature": "data/input/disease_signature.json",
        "gold": "data/gold_standard/atherosclerosis.json",
    },
    "breast_cancer_er": {
        "signature": "data/input/disease_signature_breast_cancer_er.json",
        "gold": "data/gold_standard/breast_cancer_er.json",
    },
    "ulcerative_colitis": {
        "signature": "data/input/disease_signature_ulcerative_colitis.json",
        "gold": "data/gold_standard/ulcerative_colitis.json",
    },
    "type2_diabetes": {
        "signature": "data/input/disease_signature_type2_diabetes.json",
        "gold": "data/gold_standard/type2_diabetes.json",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_gold_standard_json(path: str) -> Tuple[Set[str], Set[str]]:
    """Load gold standard from JSON file. Returns (known_reversers, known_non_reversers) as lowercase sets."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    reversers = {d.lower() for d in data.get("known_reversers", [])}
    non_reversers = {d.lower() for d in data.get("known_non_reversers", [])}
    return reversers, non_reversers


def _safe_float(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        f = float(val)
        return None if np.isnan(f) else f
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Single Disease Validation
# ---------------------------------------------------------------------------

def run_single_disease(disease_name: str, cache_dir: str) -> Dict:
    """Run full validation pipeline for a single disease.

    Returns a dict with metrics, drug rankings, and diagnostic info.
    """
    reg = DISEASE_REGISTRY[disease_name]
    gold_path = os.path.join(BASE_DIR, reg["gold"])

    known_reversers, known_non_reversers = load_gold_standard_json(gold_path)

    print(f"\n{'='*70}")
    print(f"  {disease_name.upper().replace('_', ' ')}")
    print(f"{'='*70}")

    t0 = time.time()

    # --- Load cached data ---
    cache_files = os.listdir(cache_dir)
    rank_files = [f for f in cache_files if f.startswith("ranktwosided_")]
    meta_files = [f for f in cache_files if f.startswith("sigmeta_")]

    if not rank_files or not meta_files:
        print(f"  [SKIP] No cached data found for {disease_name}.")
        print(f"         Run: python scripts/run.py --config configs/default.yaml "
              f"--in {reg['signature']} --out data/output_{disease_name}")
        return {"disease": disease_name, "status": "no_cache"}

    # We need to find the correct cache files for this disease.
    # The cache is SHA1-hashed by gene list, so each disease has unique files.
    # If multiple diseases are cached, we need to match by loading the signature
    # and computing the expected hash.
    # For simplicity, if there's only one set of cache files, use it.
    # If multiple, we load the signature to determine which is correct.

    sig_path = os.path.join(BASE_DIR, reg["signature"])
    with open(sig_path, "r", encoding="utf-8") as f:
        sig_data = json.load(f)

    # Try all rank files to find the one matching this disease's gene count
    best_rank_file = None
    best_meta_file = None

    if len(rank_files) == 1:
        best_rank_file = rank_files[0]
        best_meta_file = meta_files[0]
    else:
        # Load each rank file and check drug count or try to match
        # by loading signature entity count
        import hashlib
        from sigreverse.io import sanitize_genes

        up = sanitize_genes(sig_data.get("up", []))
        down = sanitize_genes(sig_data.get("down", []))
        symbols = list(dict.fromkeys(up + down))

        # Compute entity cache key hash
        ent_cache_key = {"type": "entities_find_by_symbols", "symbols": symbols}
        ent_hash = hashlib.sha1(
            json.dumps(ent_cache_key, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        ent_file = f"entities_{ent_hash}.json"
        if ent_file in cache_files:
            # Load entities to get UUIDs
            with open(os.path.join(cache_dir, ent_file)) as f:
                entities = json.load(f)
            sym2uuid = {e["meta"]["symbol"]: e["id"] for e in entities}
            up_ents = [sym2uuid[g] for g in up if g in sym2uuid]
            down_ents = [sym2uuid[g] for g in down if g in sym2uuid]

            # Compute rank cache key hash
            from sigreverse.io import read_disease_signature
            import yaml

            cfg_path = os.path.join(BASE_DIR, "configs", "default.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)

            ldp3_cfg = cfg.get("ldp3", {})
            rank_req = {
                "type": "ranktwosided",
                "up_entities": up_ents,
                "down_entities": down_ents,
                "limit": int(ldp3_cfg.get("topk_signatures", 2000)),
                "database": ldp3_cfg.get("database", "l1000_cp"),
            }
            rank_hash = hashlib.sha1(
                json.dumps(rank_req, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()

            rank_target = f"ranktwosided_{rank_hash}.json"
            if rank_target in cache_files:
                best_rank_file = rank_target
                # Find corresponding meta file
                with open(os.path.join(cache_dir, rank_target)) as f:
                    rank_data_tmp = json.load(f)
                sig_uuids = [r["uuid"] for r in rank_data_tmp.get("results", [])]
                meta_req = {"type": "signatures_meta", "uuids": sig_uuids}
                meta_hash = hashlib.sha1(
                    json.dumps(meta_req, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                meta_target = f"sigmeta_{meta_hash}.json"
                if meta_target in cache_files:
                    best_meta_file = meta_target

    if not best_rank_file or not best_meta_file:
        print(f"  [SKIP] Cannot find matching cache files for {disease_name}.")
        print(f"         Run: python scripts/run.py --config configs/default.yaml "
              f"--in {reg['signature']} --out data/output_{disease_name}")
        return {"disease": disease_name, "status": "no_cache"}

    # Load data
    with open(os.path.join(cache_dir, best_rank_file)) as f:
        rank_data = json.load(f)
    with open(os.path.join(cache_dir, best_meta_file)) as f:
        sig_meta = json.load(f)

    df_sig = pd.DataFrame(rank_data.get("results", []))
    df_meta = pd.json_normalize(sig_meta)
    df_detail = df_sig.merge(df_meta, left_on="uuid", right_on="id", how="left")

    print(f"  Signatures: {len(df_detail)}, Unique drugs: {df_detail['meta.pert_name'].nunique()}")

    # --- Scoring ---
    score_results = []
    for _, row in df_detail.iterrows():
        z_up = float(row.get("z-up", 0.0))
        z_down = float(row.get("z-down", 0.0))
        fdr_up = _safe_float(row.get("fdr-up"))
        fdr_down = _safe_float(row.get("fdr-down"))
        logp_fisher = _safe_float(row.get("logp-fisher"))
        ldp3_type = row.get("type") if "type" in row.index else None

        ss = compute_signature_score(
            z_up=z_up, z_down=z_down, mode=ScoringMode.WTCS_LIKE,
            fdr_up=fdr_up, fdr_down=fdr_down, fdr_threshold=0.05,
            logp_fisher=logp_fisher,
            ldp3_type=str(ldp3_type) if ldp3_type is not None else None,
        )
        score_results.append({
            "is_reverser": ss.is_reverser,
            "sig_score": ss.sig_score,
            "sig_strength": ss.sig_strength,
            "fdr_pass": ss.fdr_pass,
            "ldp3_type_agree": ss.ldp3_type_agree,
            "confidence_weight": ss.confidence_weight,
            "direction_category": ss.direction_category,
        })

    df_scores = pd.DataFrame(score_results, index=df_detail.index)
    df_detail = pd.concat([df_detail, df_scores], axis=1)

    n_rev = int(df_detail["is_reverser"].sum())
    n_mim = int((df_detail["direction_category"] == "mimicker").sum())
    fdr_pass = int(df_detail["fdr_pass"].sum())
    print(f"  Reversers: {n_rev} | Mimickers: {n_mim} | FDR pass: {fdr_pass}/{len(df_detail)}")

    # --- Drug aggregation ---
    df_drug = aggregate_to_drug(
        df_detail, n_cap=8, min_signatures=1, min_reverser=1,
        filter_fdr=True, aggregation_mode="weighted_median",
        n_factor_mode="log", cl_diversity_bonus=0.1,
    )
    n_ok = int((df_drug["status"] == "ok").sum())

    # Confidence tiers
    tier_dist = {}
    if "confidence_tier" in df_drug.columns:
        tier_dist = df_drug[df_drug["status"] == "ok"]["confidence_tier"].value_counts().to_dict()

    # Cell-line conflicts
    n_conflict = 0
    if "has_cl_conflict" in df_drug.columns:
        n_conflict = int(df_drug["has_cl_conflict"].sum())

    print(f"  Drugs ranked: {n_ok} | Tiers: {tier_dist} | CL conflicts: {n_conflict}")

    # --- CMap Tau ---
    provider = LDP3ESProvider(df_detail)
    cmap = CMapPipeline(
        provider, ncs_method="cell_line_null", tau_aggregation="quantile_max",
        tau_reference_mode="bootstrap",
    )
    cmap.run()
    df_tau = cmap.to_dataframe()
    if len(df_tau) > 0:
        df_drug = df_drug.merge(df_tau, on="drug", how="left")

    # --- Dose-response ---
    df_dr = analyze_dose_response(df_detail)
    if len(df_dr) > 0:
        df_drug = df_drug.merge(df_dr, on="drug", how="left")

    # --- Ranking & Evaluation ---
    df_ranked = df_drug[df_drug["status"] == "ok"].sort_values(
        "final_reversal_score", ascending=True
    ).reset_index(drop=True)

    ranked_drugs = df_ranked["drug"].str.lower().tolist()
    all_drugs = set(ranked_drugs)
    found_gold = known_reversers & all_drugs
    found_neg = known_non_reversers & all_drugs

    print(f"  Gold drugs found: {len(found_gold)}/{len(known_reversers)}")
    print(f"  Non-reversers found: {len(found_neg)}/{len(known_non_reversers)}")

    # Top-20
    print(f"\n  --- Top 20 Reversers ---")
    for i, row in df_ranked.head(20).iterrows():
        drug = row["drug"]
        score = row["final_reversal_score"]
        tau = row.get("tau", np.nan)
        tau_str = f"tau={tau:.1f}" if isinstance(tau, (int, float)) and not np.isnan(tau) else "tau=N/A"
        is_gold = " *" if drug.lower() in known_reversers else "  "
        tier = row.get("confidence_tier", "?")
        n_cl = row.get("n_cell_lines", 1)
        print(f"  {is_gold} {i+1:3d}. {drug:30s} score={score:+.4f}  {tau_str}  [{tier}] cl={n_cl}")

    # Metrics
    metrics = {}
    if found_gold:
        metrics = evaluate_ranking(
            ranked_drugs=ranked_drugs,
            known_positives=found_gold,
            ks=[5, 10, 20, 50, 100],
        )
        print(f"\n  --- Metrics ---")
        for k in ["auroc", "auprc", "mrr", "map", "hit@20", "hit@50", "hit@100",
                   "precision@20", "precision@50", "ndcg@20", "ndcg@50"]:
            if k in metrics:
                print(f"    {k:20s}: {metrics[k]:.4f}")

    # Gold drug positions
    gold_positions = {}
    if found_gold:
        print(f"\n  --- Gold Drug Positions ---")
        for drug_lower in sorted(found_gold):
            if drug_lower in ranked_drugs:
                pos = ranked_drugs.index(drug_lower) + 1
                pct = 100 * pos / len(ranked_drugs)
                row_data = df_ranked[df_ranked["drug"].str.lower() == drug_lower].iloc[0]
                score = row_data["final_reversal_score"]
                gold_positions[drug_lower] = pos
                print(f"    {drug_lower:25s}: rank {pos:4d}/{len(ranked_drugs)} "
                      f"(top {pct:4.0f}%)  score={score:+.4f}")

    # Non-reverser positions
    if found_neg:
        print(f"\n  --- Non-Reverser Positions (should be low-ranked) ---")
        for drug_lower in sorted(found_neg):
            if drug_lower in ranked_drugs:
                pos = ranked_drugs.index(drug_lower) + 1
                pct = 100 * pos / len(ranked_drugs)
                print(f"    {drug_lower:25s}: rank {pos:4d}/{len(ranked_drugs)} (top {pct:4.0f}%)")

    elapsed = time.time() - t0

    # Summary counts
    gold_top20 = sum(1 for d in df_ranked.head(20)["drug"].str.lower() if d in known_reversers)
    gold_top50 = sum(1 for d in df_ranked.head(50)["drug"].str.lower() if d in known_reversers)
    gold_top100 = sum(1 for d in df_ranked.head(100)["drug"].str.lower() if d in known_reversers)

    # Save output
    out_dir = os.path.join(BASE_DIR, "data", f"output_{disease_name}_validation")
    os.makedirs(out_dir, exist_ok=True)
    df_drug.to_csv(os.path.join(out_dir, "drug_reversal_rank.csv"), index=False)

    result = {
        "disease": disease_name,
        "status": "ok",
        "n_signatures": len(df_detail),
        "n_drugs_ranked": n_ok,
        "n_reversers": n_rev,
        "n_mimickers": n_mim,
        "fdr_pass_rate": fdr_pass / len(df_detail) if len(df_detail) > 0 else 0,
        "n_gold_found": len(found_gold),
        "n_gold_total": len(known_reversers),
        "n_neg_found": len(found_neg),
        "gold_top20": gold_top20,
        "gold_top50": gold_top50,
        "gold_top100": gold_top100,
        "tier_dist": tier_dist,
        "n_cl_conflicts": n_conflict,
        "elapsed_sec": elapsed,
        "metrics": metrics,
        "gold_positions": gold_positions,
    }

    return result


# ---------------------------------------------------------------------------
# Cross-Disease Comparison
# ---------------------------------------------------------------------------

def print_comparison_table(results: list):
    """Print a comparison table across all diseases."""
    print(f"\n\n{'='*90}")
    print(f"  CROSS-DISEASE COMPARISON — SigReverse v0.3.1")
    print(f"{'='*90}")

    # Header
    diseases = [r["disease"] for r in results if r["status"] == "ok"]
    if not diseases:
        print("  No successful runs to compare.")
        return

    # Basic stats table
    print(f"\n  {'Metric':<25s}", end="")
    for d in diseases:
        label = d.replace("_", " ")[:18]
        print(f"  {label:>18s}", end="")
    print()
    print(f"  {'-'*25}", end="")
    for _ in diseases:
        print(f"  {'-'*18}", end="")
    print()

    metrics_to_show = [
        ("Signatures", "n_signatures"),
        ("Drugs ranked", "n_drugs_ranked"),
        ("Gold found", "n_gold_found"),
        ("Gold total", "n_gold_total"),
        ("Gold in top-20", "gold_top20"),
        ("Gold in top-50", "gold_top50"),
        ("Gold in top-100", "gold_top100"),
        ("CL conflicts", "n_cl_conflicts"),
    ]

    for label, key in metrics_to_show:
        print(f"  {label:<25s}", end="")
        for r in results:
            if r["status"] != "ok":
                print(f"  {'N/A':>18s}", end="")
            else:
                val = r.get(key, "N/A")
                if isinstance(val, int):
                    if key in ("n_gold_found",):
                        total = r.get("n_gold_total", 0)
                        print(f"  {val}/{total:>14}", end="")
                    else:
                        print(f"  {val:>18d}", end="")
                else:
                    print(f"  {str(val):>18s}", end="")
        print()

    # Metric scores table
    metric_keys = ["auroc", "auprc", "mrr", "map", "hit@20", "hit@50",
                   "precision@20", "precision@50", "ndcg@20", "ndcg@50"]

    print(f"\n  {'Ranking Metric':<25s}", end="")
    for d in diseases:
        label = d.replace("_", " ")[:18]
        print(f"  {label:>18s}", end="")
    print()
    print(f"  {'-'*25}", end="")
    for _ in diseases:
        print(f"  {'-'*18}", end="")
    print()

    for mk in metric_keys:
        print(f"  {mk:<25s}", end="")
        for r in results:
            if r["status"] != "ok":
                print(f"  {'N/A':>18s}", end="")
            else:
                val = r.get("metrics", {}).get(mk)
                if val is not None:
                    print(f"  {val:>18.4f}", end="")
                else:
                    print(f"  {'N/A':>18s}", end="")
        print()

    # Confidence tier comparison
    print(f"\n  {'Confidence Tier':<25s}", end="")
    for d in diseases:
        label = d.replace("_", " ")[:18]
        print(f"  {label:>18s}", end="")
    print()
    print(f"  {'-'*25}", end="")
    for _ in diseases:
        print(f"  {'-'*18}", end="")
    print()

    for tier in ["high", "medium", "low", "exploratory"]:
        print(f"  {tier:<25s}", end="")
        for r in results:
            if r["status"] != "ok":
                print(f"  {'N/A':>18s}", end="")
            else:
                val = r.get("tier_dist", {}).get(tier, 0)
                print(f"  {val:>18d}", end="")
        print()

    print(f"\n{'='*90}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="SigReverse v0.3.1 — Multi-disease validation"
    )
    ap.add_argument("--all", action="store_true", help="Run all registered diseases")
    ap.add_argument("--disease", nargs="+", help="Disease name(s) to validate")
    args = ap.parse_args()

    if args.all:
        diseases = list(DISEASE_REGISTRY.keys())
    elif args.disease:
        diseases = args.disease
    else:
        ap.print_help()
        print(f"\nAvailable diseases: {list(DISEASE_REGISTRY.keys())}")
        return

    # Validate disease names
    for d in diseases:
        if d not in DISEASE_REGISTRY:
            print(f"Unknown disease: {d}")
            print(f"Available: {list(DISEASE_REGISTRY.keys())}")
            return

    cache_dir = os.path.join(BASE_DIR, "data", "cache")

    print("=" * 70)
    print("SigReverse v0.3.1 — Multi-Disease Validation")
    print(f"Diseases: {diseases}")
    print("=" * 70)

    t_total = time.time()
    results = []

    for disease in diseases:
        try:
            result = run_single_disease(disease, cache_dir)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to validate {disease}: {e}")
            import traceback
            traceback.print_exc()
            results.append({"disease": disease, "status": "error", "error": str(e)})

    # Cross-disease comparison
    if len([r for r in results if r["status"] == "ok"]) > 1:
        print_comparison_table(results)

    total_elapsed = time.time() - t_total
    print(f"\nTotal validation time: {total_elapsed:.1f}s")

    # Save summary
    summary_path = os.path.join(BASE_DIR, "data", "output_multi_disease_summary.json")
    summary = []
    for r in results:
        s = {k: v for k, v in r.items() if k != "gold_positions"}
        # Convert numpy types for JSON
        for k, v in s.items():
            if isinstance(v, dict):
                s[k] = {str(kk): float(vv) if isinstance(vv, (np.floating, np.integer)) else vv
                         for kk, vv in v.items()}
            elif isinstance(v, (np.floating, np.integer)):
                s[k] = float(v)
        summary.append(s)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved to: {summary_path}")


if __name__ == "__main__":
    main()
