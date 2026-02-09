"""End-to-end pipeline validation with gold standard evaluation.

Uses cached LDP3 data (atherosclerosis signature) to:
    1. Run the full v0.3.0 pipeline (scoring + CMap Tau + dose-response)
    2. Evaluate against known drug-disease relationships (gold standard)
    3. Compare old v0.2.0 (broken sign convention) vs new v0.3.0
    4. Generate detailed diagnostic report

Gold Standard (Atherosclerosis):
    Known therapeutics that should appear as reversers:
        - Statins: simvastatin, atorvastatin, lovastatin, pravastatin, rosuvastatin
        - Anti-inflammatory: dexamethasone, prednisolone, hydrocortisone
        - PPARγ agonists: pioglitazone, rosiglitazone, troglitazone
        - NF-κB/inflammation: celecoxib, ibuprofen, aspirin
        - ACE inhibitors: captopril, enalapril, ramipril
        - ARBs: losartan, valsartan
        - Cholesterol absorption: ezetimibe
        - Omega-3: EPA (eicosapentaenoic acid)
        - mTOR: sirolimus, everolimus
        - Other CVD: metformin, colchicine, resveratrol

    Known NON-therapeutics (should NOT rank highly):
        - Oncology drugs: doxorubicin, cisplatin, vincristine, paclitaxel
        - CNS drugs: haloperidol, chlorpromazine, fluoxetine
        - Random: wortmannin (PI3K inhibitor, toxic)
"""
from __future__ import annotations

import json
import os
import sys
import logging
import time

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
logger = logging.getLogger("validate")


# ---------------------------------------------------------------------------
# Gold Standard for Atherosclerosis
# ---------------------------------------------------------------------------

KNOWN_REVERSERS = {
    # Statins (HMG-CoA reductase inhibitors)
    "simvastatin", "atorvastatin", "lovastatin", "pravastatin",
    "rosuvastatin", "fluvastatin", "pitavastatin", "mevastatin",
    # Anti-inflammatory / Corticosteroids
    "dexamethasone", "prednisolone", "hydrocortisone", "methylprednisolone",
    "prednisone", "betamethasone", "triamcinolone",
    # PPARγ agonists (insulin sensitizers)
    "pioglitazone", "rosiglitazone", "troglitazone",
    # NSAIDs / COX inhibitors
    "celecoxib", "ibuprofen", "aspirin", "indomethacin", "naproxen",
    "diclofenac", "sulindac",
    # ACE inhibitors
    "captopril", "enalapril", "ramipril", "lisinopril", "perindopril",
    # ARBs
    "losartan", "valsartan", "telmisartan", "irbesartan",
    # Other CVD-relevant
    "metformin", "colchicine", "resveratrol", "ezetimibe",
    "sirolimus", "everolimus", "rapamycin",
    # HDAC inhibitors (anti-inflammatory potential)
    "vorinostat", "trichostatin-a", "trichostatin a",
    # Antioxidants
    "quercetin", "curcumin",
    # Lipid-related
    "fenofibrate", "gemfibrozil", "niacin",
}

KNOWN_NON_REVERSERS = {
    # Oncology (cytotoxic, should not reverse atherosclerosis)
    "doxorubicin", "cisplatin", "vincristine", "paclitaxel",
    "etoposide", "camptothecin", "methotrexate",
    # CNS drugs
    "haloperidol", "chlorpromazine", "fluoxetine", "clozapine",
    # Toxic / non-relevant
    "wortmannin", "staurosporine", "thapsigargin",
}


def _safe_float(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        f = float(val)
        return None if np.isnan(f) else f
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main validation pipeline
# ---------------------------------------------------------------------------

def run_validation():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(base_dir, "data", "cache")

    print("=" * 70)
    print("SigReverse v0.3.1 — Pipeline Validation with Gold Standard")
    print("=" * 70)

    t0 = time.time()

    # --- Load cached data ---
    print("\n[1] Loading cached LDP3 data...")

    # Find cache files
    cache_files = os.listdir(cache_dir)
    rank_file = [f for f in cache_files if f.startswith("ranktwosided_")][0]
    meta_file = [f for f in cache_files if f.startswith("sigmeta_")][0]

    with open(os.path.join(cache_dir, rank_file)) as f:
        rank_data = json.load(f)
    with open(os.path.join(cache_dir, meta_file)) as f:
        sig_meta = json.load(f)

    df_sig = pd.DataFrame(rank_data.get("results", []))
    df_meta = pd.json_normalize(sig_meta)
    df_detail = df_sig.merge(df_meta, left_on="uuid", right_on="id", how="left")

    print(f"    Signatures: {len(df_detail)}")
    print(f"    Unique drugs: {df_detail['meta.pert_name'].nunique()}")

    # --- Step 2: Signature-level scoring (v0.3.0 corrected) ---
    print("\n[2] Scoring signatures (v0.3.0 corrected LDP3 convention)...")

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

    n_rev = df_detail["is_reverser"].sum()
    n_mim = (df_detail["direction_category"] == "mimicker").sum()
    n_partial = (df_detail["direction_category"] == "partial").sum()
    fdr_pass = df_detail["fdr_pass"].sum()

    # LDP3 type agreement
    agree_col = df_detail["ldp3_type_agree"].dropna()
    agree_rate = float(agree_col.sum()) / len(agree_col) if len(agree_col) > 0 else 0

    print(f"    Reversers: {n_rev} | Mimickers: {n_mim} | Partial: {n_partial}")
    print(f"    FDR pass: {fdr_pass}/{len(df_detail)}")
    print(f"    LDP3 type agreement: {agree_rate:.1%}")

    # --- Step 3: Drug-level aggregation (v0.3.1: soft log penalty, min_sig=1) ---
    print("\n[3] Aggregating to drug level (v0.3.1: n_cap=8, log penalty, min_sig=1)...")
    df_drug = aggregate_to_drug(
        df_detail, n_cap=8, min_signatures=1, min_reverser=1,
        filter_fdr=True, aggregation_mode="weighted_median",
        n_factor_mode="log", cl_diversity_bonus=0.1,
    )

    n_ok = (df_drug["status"] == "ok").sum()
    n_too_few = (df_drug["status"] == "too_few_signatures").sum()
    n_no_rev = (df_drug["status"] == "no_reverser_context").sum()
    print(f"    Drugs: {len(df_drug)} total, {n_ok} ok, {n_too_few} too_few, {n_no_rev} no_reverser")

    # Confidence tier distribution
    if "confidence_tier" in df_drug.columns:
        tier_dist = df_drug[df_drug["status"]=="ok"]["confidence_tier"].value_counts().to_dict()
        print(f"    Confidence tiers: {tier_dist}")

    # Cell-line conflict stats
    if "has_cl_conflict" in df_drug.columns:
        n_conflict = df_drug["has_cl_conflict"].sum()
        print(f"    Cell-line conflicts: {n_conflict} drugs")

    # --- Step 4: CMap 4-stage pipeline (Tau with bootstrap ref) ---
    print("\n[4] Running CMap Tau pipeline (bootstrap reference)...")
    provider = LDP3ESProvider(df_detail)
    cmap = CMapPipeline(
        provider, ncs_method="cell_line_null", tau_aggregation="quantile_max",
        tau_reference_mode="bootstrap",
    )
    cmap.run()
    df_tau = cmap.to_dataframe()
    if len(df_tau) > 0:
        df_drug = df_drug.merge(df_tau, on="drug", how="left")
    print(f"    Tau scored: {len(df_tau)} drugs")

    # --- Step 5: Dose-response ---
    print("\n[5] Analyzing dose-response...")
    df_dr = analyze_dose_response(df_detail)
    if len(df_dr) > 0:
        df_drug = df_drug.merge(df_dr, on="drug", how="left")
    print(f"    Drugs with dose data: {len(df_dr)}")

    # --- Step 6: QC Summary ---
    print("\n[6] QC Summary...")
    sig_qc = signature_qc_summary(df_detail)
    print(f"    Zero-score fraction: {sig_qc.get('zero_score_fraction', 'N/A'):.1%}")
    print(f"    Direction distribution: {sig_qc.get('direction_distribution', {})}")

    # --- Step 7: Gold Standard Evaluation ---
    print("\n" + "=" * 70)
    print("GOLD STANDARD EVALUATION — Atherosclerosis")
    print("=" * 70)

    # Sort by final_reversal_score ascending (most negative = best reverser)
    df_ranked = df_drug[df_drug["status"] == "ok"].sort_values(
        "final_reversal_score", ascending=True
    ).reset_index(drop=True)

    all_drugs = set(df_ranked["drug"].str.lower())
    found_gold = KNOWN_REVERSERS & all_drugs
    found_neg = KNOWN_NON_REVERSERS & all_drugs

    print(f"\n  Known reversers found in dataset: {len(found_gold)}/{len(KNOWN_REVERSERS)}")
    print(f"  Known non-reversers found: {len(found_neg)}/{len(KNOWN_NON_REVERSERS)}")

    if found_gold:
        print(f"\n  Gold standard drugs found: {sorted(found_gold)}")

    # Find gold standard drugs in ranking
    ranked_drugs = df_ranked["drug"].str.lower().tolist()

    print(f"\n  --- Top 20 Reverser Drugs (most negative score) ---")
    for i, row in df_ranked.head(20).iterrows():
        drug = row["drug"]
        score = row["final_reversal_score"]
        tau = row.get("tau", "N/A")
        tau_str = f"tau={tau:.1f}" if isinstance(tau, (int, float)) and not np.isnan(tau) else "tau=N/A"
        is_gold = "★" if drug.lower() in KNOWN_REVERSERS else " "
        is_neg = "✗" if drug.lower() in KNOWN_NON_REVERSERS else " "
        n_rev_d = row.get("n_reverser", 0)
        n_sig = row.get("n_signatures_fdr_pass", 0)
        tier = row.get("confidence_tier", "?")
        n_cl = row.get("n_cell_lines", 1)
        print(f"    {is_gold}{is_neg} Rank {i+1:3d}: {drug:30s} score={score:+.4f}  {tau_str}  "
              f"n_rev={n_rev_d}/{n_sig}  cl={n_cl}  [{tier}]")

    print(f"\n  --- Bottom 10 (Mimickers / worst reversers) ---")
    for i, row in df_ranked.tail(10).iterrows():
        drug = row["drug"]
        score = row["final_reversal_score"]
        is_neg = "✗" if drug.lower() in KNOWN_NON_REVERSERS else " "
        print(f"    {is_neg} Rank {i+1:3d}: {drug:30s} score={score:+.4f}")

    # Evaluate ranking quality
    if found_gold:
        eval_results = evaluate_ranking(
            ranked_drugs=ranked_drugs,
            known_positives=found_gold,
            ks=[5, 10, 20, 50],
        )
        print(f"\n  --- Ranking Metrics (against {len(found_gold)} gold standard drugs) ---")
        for k, v in sorted(eval_results.items()):
            print(f"    {k:20s}: {v:.4f}")

    # Gold drug positions
    if found_gold:
        print(f"\n  --- Gold Standard Drug Positions ---")
        for drug_lower in sorted(found_gold):
            if drug_lower in ranked_drugs:
                pos = ranked_drugs.index(drug_lower) + 1
                row_data = df_ranked[df_ranked["drug"].str.lower() == drug_lower].iloc[0]
                score = row_data["final_reversal_score"]
                tau = row_data.get("tau", np.nan)
                tau_str = f"tau={tau:.1f}" if isinstance(tau, (int, float)) and not np.isnan(tau) else ""
                pct = 100 * pos / len(ranked_drugs)
                print(f"    {drug_lower:25s}: rank {pos:3d}/{len(ranked_drugs)} "
                      f"(top {pct:.0f}%)  score={score:+.4f}  {tau_str}")

    # Non-reverser positions
    if found_neg:
        print(f"\n  --- Known NON-Reverser Positions (should be low-ranked) ---")
        for drug_lower in sorted(found_neg):
            if drug_lower in ranked_drugs:
                pos = ranked_drugs.index(drug_lower) + 1
                row_data = df_ranked[df_ranked["drug"].str.lower() == drug_lower].iloc[0]
                score = row_data["final_reversal_score"]
                pct = 100 * pos / len(ranked_drugs)
                print(f"    {drug_lower:25s}: rank {pos:3d}/{len(ranked_drugs)} "
                      f"(top {pct:.0f}%)  score={score:+.4f}")

    # --- Dose-response quality for gold drugs ---
    if found_gold and "dr_quality" in df_drug.columns:
        print(f"\n  --- Dose-Response Quality for Gold Drugs ---")
        for drug_lower in sorted(found_gold):
            rows = df_drug[df_drug["drug"].str.lower() == drug_lower]
            if len(rows) > 0:
                row = rows.iloc[0]
                quality = row.get("dr_quality", "N/A")
                n_doses = row.get("dr_n_doses", 0)
                print(f"    {drug_lower:25s}: quality={quality}, n_doses={n_doses}")

    # --- Summary Stats ---
    elapsed = time.time() - t0
    gold_top20 = sum(1 for d in df_ranked.head(20)['drug'].str.lower() if d in KNOWN_REVERSERS)
    gold_top50 = sum(1 for d in df_ranked.head(50)['drug'].str.lower() if d in KNOWN_REVERSERS)
    gold_top100 = sum(1 for d in df_ranked.head(100)['drug'].str.lower() if d in KNOWN_REVERSERS)

    print(f"\n{'=' * 70}")
    print(f"VALIDATION COMPLETE (v0.3.1) - {elapsed:.1f}s")
    print(f"{'=' * 70}")
    print(f"  Total drugs ranked: {len(df_ranked)}")
    print(f"  Reverser detection: {n_rev} sigs ({n_rev/len(df_detail):.0%})")
    print(f"  LDP3 agreement: {agree_rate:.1%}")
    print(f"  Gold drugs in top-20: {gold_top20}/{len(found_gold)}")
    print(f"  Gold drugs in top-50: {gold_top50}/{len(found_gold)}")
    print(f"  Gold drugs in top-100: {gold_top100}/{len(found_gold)}")

    # Confidence tier summary
    if "confidence_tier" in df_ranked.columns:
        print(f"\n  --- Confidence Tier Distribution (ranked drugs) ---")
        for tier in ["high", "medium", "low", "exploratory"]:
            n_tier = (df_ranked["confidence_tier"] == tier).sum()
            print(f"    {tier:12s}: {n_tier:3d} drugs")

    # v0.3.1 improvements summary
    print(f"\n  --- v0.3.1 Improvements Applied ---")
    print(f"    n_factor: log(1+n)/log(1+n_cap) with n_cap=8 (was sqrt with n_cap=20)")
    print(f"    min_signatures: 1 (was 3)")
    print(f"    Tau reference: bootstrap (was self-referencing)")
    print(f"    Cell-line conflicts: auto-detected, quantile_max aggregation")

    # Save results
    out_dir = os.path.join(base_dir, "data", "output_v3_validation")
    os.makedirs(out_dir, exist_ok=True)
    df_drug.to_csv(os.path.join(out_dir, "drug_reversal_rank.csv"), index=False)
    df_detail.to_csv(os.path.join(out_dir, "signature_level_details.csv"), index=False)
    print(f"\n  Output saved to: {out_dir}")

    return df_drug, df_detail, df_ranked


if __name__ == "__main__":
    run_validation()
