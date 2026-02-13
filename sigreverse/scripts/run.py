"""SigReverse main orchestration script — v0.4.0 (industrial grade)

Pipeline (13 steps):
    1.  Load config & disease signature, validate input quality
    2.  Map gene symbols → LINCS entity UUIDs (cached)
    3.  Run ranktwosided enrichment via LDP3 API (cached)
    4.  Fetch signature metadata (cell_line, dose, time, pert_name) (cached)
    5.  Signature-level scoring: WTCS-like continuous + FDR filter + LDP3 cross-validation
    6.  Drug-level aggregation: confidence-weighted + cell-line/time weights + robustness
    7.  Statistical significance: permutation p-value + BH-FDR + bootstrap CI + z-normalized
    8.  CMap 4-stage pipeline: ES → WTCS → NCS → Tau (parallel scoring)
    9.  Dose-response analysis: monotonicity + Hill fit + quality tier
    10. Drug name standardization: PubChem → InChIKey → UniChem (optional)
    11. QC: toxicity flags + signature-level diagnostics
    12. Fusion ranking: SigReverse × KG_Explain × Safety (optional)
    13. Output: drug_reversal_rank.csv + signature_level_details.csv + run_manifest.json

v0.4.0 improvements:
    - Structured logging with step timing
    - Cache with TTL metadata and statistics
    - Enhanced run_manifest with cache stats and API client stats
    - Config validation
    - Error classification for LDP3 API calls
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any

import numpy as np
import pandas as pd
import yaml

from sigreverse.io import (
    read_disease_signature, sanitize_genes, ensure_dir, write_csv, write_json,
)
from sigreverse.ldp3_client import LDP3Client
from sigreverse.scoring import (
    compute_signature_score, maybe_flip_z_down, ScoringMode,
)
from sigreverse.robustness import aggregate_to_drug, load_cell_line_weights
from sigreverse.qc import (
    missing_gene_ratio, check_signature_size, signature_qc_summary,
    apply_toxicity_flags,
)
from sigreverse.statistics import compute_drug_significance
from sigreverse.cmap_algorithms import CMapPipeline, LDP3ESProvider, load_touchstone_reference
from sigreverse.dose_response import analyze_dose_response

logger = logging.getLogger("sigreverse.run")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha1_of_obj(obj: Any) -> str:
    b = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(b).hexdigest()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cache_get(cache_path: str):
    """Read from cache file. Supports both legacy and new metadata format."""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Support new cache format with _cache_meta wrapper
        if isinstance(raw, dict) and "_cache_meta" in raw:
            return raw.get("data")
        return raw
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Cache read error: {cache_path}: {e}")
        return None


def cache_put(cache_path: str, obj: Any):
    """Write to cache file with metadata."""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    from sigreverse.cache import CacheEntry
    entry = CacheEntry(data=obj, key=os.path.basename(cache_path))
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
    except (OSError, TypeError) as e:
        logger.warning(f"Cache write error: {cache_path}: {e}")
        # Fallback: write without metadata
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_load_and_validate(args, cfg) -> dict:
    """Step 1: Load config, disease signature, validate inputs."""
    sig = read_disease_signature(args.inp)

    dedupe = bool(cfg.get("signature", {}).get("dedupe", True))
    trim_topn = cfg.get("signature", {}).get("trim_topn", None)
    up = sanitize_genes(sig.get("up", []), dedupe=dedupe, trim_topn=trim_topn)
    down = sanitize_genes(sig.get("down", []), dedupe=dedupe, trim_topn=trim_topn)

    if len(up) == 0 or len(down) == 0:
        raise ValueError("After sanitization, 'up' and 'down' must both be non-empty.")

    # Check signature size
    size_cfg = cfg.get("qc", {}).get("signature_size", {})
    size_check = check_signature_size(
        len(up), len(down),
        min_recommended=int(size_cfg.get("min_recommended", 50)),
        optimal_min=int(size_cfg.get("optimal_min", 150)),
    )
    for w in size_check.get("warnings", []):
        logger.warning(f"[QC] {w}")

    return {
        "sig": sig, "up": up, "down": down,
        "size_check": size_check,
    }


def step_entity_mapping(up, down, client, cache_dir, cache_enabled) -> dict:
    """Step 2: Map gene symbols to LINCS entity UUIDs."""
    symbols = list(dict.fromkeys(up + down))
    ent_cache_key = {"type": "entities_find_by_symbols", "symbols": symbols}
    ent_cache_path = os.path.join(cache_dir, f"entities_{sha1_of_obj(ent_cache_key)}.json")

    entities = cache_get(ent_cache_path) if cache_enabled else None
    if entities is None:
        entities = client.entities_find_by_symbols(symbols)
        if cache_enabled:
            cache_put(ent_cache_path, entities)

    sym2uuid = {e["meta"]["symbol"]: e["id"] for e in entities}
    up_entities = [sym2uuid[g] for g in up if g in sym2uuid]
    down_entities = [sym2uuid[g] for g in down if g in sym2uuid]
    missing_up = [g for g in up if g not in sym2uuid]
    missing_down = [g for g in down if g not in sym2uuid]

    miss_ratio = missing_gene_ratio(missing_up, missing_down, up, down)
    logger.info(f"Entity mapping: {len(sym2uuid)}/{len(symbols)} found, missing_ratio={miss_ratio:.3f}")

    return {
        "up_entities": up_entities, "down_entities": down_entities,
        "missing_up": missing_up, "missing_down": missing_down,
        "miss_ratio": miss_ratio,
    }


def step_enrichment(up_entities, down_entities, client, ldp3_cfg, cache_dir, cache_enabled) -> pd.DataFrame:
    """Step 3: Run LDP3 ranktwosided enrichment."""
    rank_req = {
        "type": "ranktwosided",
        "up_entities": up_entities,
        "down_entities": down_entities,
        "limit": int(ldp3_cfg.get("topk_signatures", 500)),
        "database": ldp3_cfg.get("database", "l1000_cp"),
    }
    rank_cache_path = os.path.join(cache_dir, f"ranktwosided_{sha1_of_obj(rank_req)}.json")

    rank_res = cache_get(rank_cache_path) if cache_enabled else None
    if rank_res is None:
        rank_res = client.enrich_ranktwosided(
            up_entities=up_entities, down_entities=down_entities,
            limit=rank_req["limit"], database=rank_req["database"],
        )
        if cache_enabled:
            cache_put(rank_cache_path, rank_res)

    df_sig = pd.DataFrame(rank_res.get("results", []))
    if df_sig.empty:
        raise RuntimeError("No results returned from LDP3 ranktwosided.")

    logger.info(f"Enrichment returned {len(df_sig)} signatures")
    return df_sig


def step_fetch_metadata(df_sig, client, cache_dir, cache_enabled) -> pd.DataFrame:
    """Step 4: Fetch signature metadata and merge."""
    sig_uuids = df_sig["uuid"].tolist()
    meta_req = {"type": "signatures_meta", "uuids": sig_uuids}
    meta_cache_path = os.path.join(cache_dir, f"sigmeta_{sha1_of_obj(meta_req)}.json")

    sig_meta = cache_get(meta_cache_path) if cache_enabled else None
    if sig_meta is None:
        sig_meta = client.signatures_find_metadata(sig_uuids)
        if cache_enabled:
            cache_put(meta_cache_path, sig_meta)

    df_meta = pd.json_normalize(sig_meta)
    df_detail = df_sig.merge(df_meta, left_on="uuid", right_on="id", how="left")
    logger.info(f"Metadata merged: {len(df_detail)} rows, {len(df_detail.columns)} columns")
    return df_detail


def step_signature_scoring(df_detail, scoring_cfg) -> pd.DataFrame:
    """Step 5: Signature-level scoring with FDR filtering and LDP3 cross-validation.

    OPTIMIZED (v0.4.1): Vectorized scoring for WTCS-like mode (~50x faster).
    Falls back to row-by-row for continuous/legacy modes.
    """
    mode_str = scoring_cfg.get("mode", "wtcs_like")
    mode = ScoringMode(mode_str)
    fdr_threshold = float(scoring_cfg.get("fdr_threshold", 0.05))

    # Optional z-down flip
    flip_z_down = bool(scoring_cfg.get("flip_z_down", False))
    if flip_z_down and "z-down" in df_detail.columns:
        df_detail["z-down"] = -df_detail["z-down"]

    # --- Vectorized path for WTCS-like mode (default, ~95% of use cases) ---
    if mode == ScoringMode.WTCS_LIKE:
        z_up = df_detail["z-up"].values.astype(float)
        z_down = df_detail["z-down"].values.astype(float)

        # Clip extreme z-scores
        z_up = np.clip(z_up, -50.0, 50.0)
        z_down = np.clip(z_down, -50.0, 50.0)

        # NaN/Inf guard
        valid = np.isfinite(z_up) & np.isfinite(z_down)
        z_up = np.where(valid, z_up, 0.0)
        z_down = np.where(valid, z_down, 0.0)

        # Direction classification
        sign_up = np.sign(z_up)
        sign_down = np.sign(z_down)
        same_sign = (sign_up == sign_down) & ((z_up != 0) | (z_down != 0))

        # WTCS score: coherent → (z_up+z_down)/2, else 0
        sig_score = np.where(same_sign, (z_up + z_down) / 2.0, 0.0)
        sig_strength = np.where(same_sign, np.abs(sig_score), np.abs(z_up - z_down) / 2.0)

        # Direction categories
        direction_category = np.full(len(z_up), "orthogonal", dtype=object)
        direction_category[(z_up < 0) & (z_down < 0)] = "reverser"
        direction_category[(z_up > 0) & (z_down > 0)] = "mimicker"
        direction_category[((z_up < 0) & (z_down > 0)) | ((z_up > 0) & (z_down < 0))] = "partial"
        direction_category[~valid] = "invalid"

        is_reverser = (direction_category == "reverser")

        # FDR filtering
        fdr_pass = np.ones(len(z_up), dtype=bool)
        if "fdr-up" in df_detail.columns and "fdr-down" in df_detail.columns:
            fdr_up_vals = pd.to_numeric(df_detail["fdr-up"], errors="coerce").values
            fdr_down_vals = pd.to_numeric(df_detail["fdr-down"], errors="coerce").values
            has_fdr = np.isfinite(fdr_up_vals) & np.isfinite(fdr_down_vals)
            fdr_pass = np.where(has_fdr, (fdr_up_vals < fdr_threshold) | (fdr_down_vals < fdr_threshold), True)

        # Confidence weight from Fisher logp
        confidence_weight = np.ones(len(z_up), dtype=float)
        if "logp-fisher" in df_detail.columns:
            logp = pd.to_numeric(df_detail["logp-fisher"], errors="coerce").values
            has_logp = np.isfinite(logp) & (logp > 0)
            confidence_weight = np.where(has_logp, np.minimum(logp / 10.0, 2.0), 1.0)

        # LDP3 type cross-validation
        ldp3_type_agree = np.full(len(z_up), np.nan)
        if "type" in df_detail.columns:
            ldp3_says_reverser = df_detail["type"].astype(str).str.strip().str.lower() == "reversers"
            has_type = df_detail["type"].notna()
            ldp3_type_agree = np.where(has_type, is_reverser == ldp3_says_reverser, np.nan)

        # Invalid rows
        sig_score[~valid] = 0.0
        sig_strength[~valid] = 0.0
        fdr_pass[~valid] = False
        confidence_weight[~valid] = 0.0

        df_detail = df_detail.copy()
        df_detail["is_reverser"] = is_reverser
        df_detail["sig_score"] = sig_score
        df_detail["sig_strength"] = sig_strength
        df_detail["fdr_pass"] = fdr_pass
        df_detail["ldp3_type_agree"] = ldp3_type_agree
        df_detail["confidence_weight"] = confidence_weight
        df_detail["direction_category"] = direction_category

    else:
        # Fallback: row-by-row for continuous/legacy_binary modes
        score_results = []
        for _, row in df_detail.iterrows():
            z_up = float(row.get("z-up", 0.0))
            z_down = float(row.get("z-down", 0.0))
            fdr_up = _safe_float(row.get("fdr-up"))
            fdr_down = _safe_float(row.get("fdr-down"))
            logp_fisher = _safe_float(row.get("logp-fisher"))
            ldp3_type = row.get("type") if "type" in row.index else None

            ss = compute_signature_score(
                z_up=z_up, z_down=z_down, mode=mode,
                fdr_up=fdr_up, fdr_down=fdr_down,
                fdr_threshold=fdr_threshold,
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

    logger.info(
        f"Scoring ({mode_str}): "
        f"{df_detail['is_reverser'].sum()} reversers, "
        f"{df_detail['fdr_pass'].sum()} FDR-pass, "
        f"{(~df_detail['fdr_pass']).sum()} FDR-fail"
    )
    return df_detail


def step_drug_aggregation(df_detail, robustness_cfg) -> pd.DataFrame:
    """Step 6: Drug-level aggregation with robustness weighting."""
    # Load optional cell line weights
    cl_weights_path = robustness_cfg.get("cell_line_weights_path")
    cl_weights = load_cell_line_weights(cl_weights_path)

    # Time weights
    time_weights = robustness_cfg.get("time_weights")

    df_drug = aggregate_to_drug(
        df_detail,
        n_cap=int(robustness_cfg.get("n_cap", 8)),
        min_signatures=int(robustness_cfg.get("min_signatures", 1)),
        min_reverser=int(robustness_cfg.get("min_reverser", 1)),
        filter_fdr=bool(robustness_cfg.get("filter_fdr", True)),
        cell_line_weights=cl_weights if cl_weights else None,
        time_weights=time_weights,
        aggregation_mode=robustness_cfg.get("aggregation_mode", "weighted_median"),
        n_factor_mode=robustness_cfg.get("n_factor_mode", "log"),
        cl_diversity_bonus=float(robustness_cfg.get("cl_diversity_bonus", 0.1)),
    )
    return df_drug


def step_statistical_significance(df_detail, df_drug, stats_cfg, robustness_cfg=None) -> pd.DataFrame:
    """Step 7: Permutation test + FDR + bootstrap CI + z-normalized.

    FIXED (v0.4.1): passes robustness config to permutation test so the null
    distribution uses the SAME aggregation formula as the observed scores.
    """
    if not stats_cfg.get("enabled", True):
        logger.info("Statistics disabled in config, skipping.")
        return df_drug

    if robustness_cfg is None:
        robustness_cfg = {}

    df_sig_stats = compute_drug_significance(
        df_detail=df_detail,
        df_drug=df_drug,
        score_col="sig_score",
        drug_col="meta.pert_name",
        drug_score_col="final_reversal_score",
        n_permutations=int(stats_cfg.get("n_permutations", 1000)),
        n_bootstrap=int(stats_cfg.get("n_bootstrap", 2000)),
        confidence=float(stats_cfg.get("confidence_level", 0.95)),
        seed=int(stats_cfg.get("seed", 42)),
        n_cap=int(robustness_cfg.get("n_cap", 8)),
        cl_diversity_bonus=float(robustness_cfg.get("cl_diversity_bonus", 0.1)),
        n_factor_mode=robustness_cfg.get("n_factor_mode", "log"),
    )

    # Merge statistics into drug table
    df_drug = df_drug.merge(df_sig_stats, on="drug", how="left")
    return df_drug


def step_qc_and_flags(df_detail, df_drug, qc_cfg) -> tuple[pd.DataFrame, dict]:
    """Step 8: QC diagnostics and toxicity flags."""
    # Signature-level QC summary
    sig_qc = signature_qc_summary(df_detail)

    # Drug-level toxicity flags
    tox_cfg = qc_cfg.get("toxicity_flag", {"enabled": True})
    df_drug = apply_toxicity_flags(df_drug, tox_cfg)

    return df_drug, sig_qc


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def step_write_outputs(
    args, cfg, df_drug, df_detail, sig_qc, entity_info, size_check, sig,
    client=None, step_timings=None,
):
    """Step 13: Write all output files with comprehensive manifest."""
    ldp3_cfg = cfg["ldp3"]

    out_rank = os.path.join(args.out_dir, "drug_reversal_rank.csv")
    out_detail = os.path.join(args.out_dir, "signature_level_details.csv")
    out_manifest = os.path.join(args.out_dir, "run_manifest.json")

    write_csv(out_rank, df_drug)
    write_csv(out_detail, df_detail)

    manifest = {
        "version": "0.4.1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_signature_path": args.inp,
        "disease_name": sig.get("name", ""),
        "disease_meta": sig.get("meta", {}),
        "config_path": args.config,
        "ldp3": {
            "metadata_api": ldp3_cfg["metadata_api"],
            "data_api": ldp3_cfg["data_api"],
            "database": ldp3_cfg.get("database", "l1000_cp"),
            "topk_signatures": int(ldp3_cfg.get("topk_signatures", 500)),
        },
        "scoring": {
            "mode": cfg.get("scoring", {}).get("mode", "wtcs_like"),
            "fdr_threshold": cfg.get("scoring", {}).get("fdr_threshold", 0.05),
        },
        "robustness": {
            "n_cap": int(cfg.get("robustness", {}).get("n_cap", 8)),
            "n_factor_mode": cfg.get("robustness", {}).get("n_factor_mode", "log"),
            "cl_diversity_bonus": float(cfg.get("robustness", {}).get("cl_diversity_bonus", 0.1)),
            "min_signatures": int(cfg.get("robustness", {}).get("min_signatures", 1)),
            "aggregation_mode": cfg.get("robustness", {}).get("aggregation_mode", "weighted_median"),
        },
        "cmap_pipeline": {
            "ncs_method": cfg.get("cmap_pipeline", {}).get("ncs_method", "cell_line_null"),
            "tau_reference_mode": cfg.get("cmap_pipeline", {}).get("tau_reference_mode", "bootstrap"),
        },
        "signature": {
            "up_n": len(entity_info.get("up_entities", [])) + len(entity_info.get("missing_up", [])),
            "down_n": len(entity_info.get("down_entities", [])) + len(entity_info.get("missing_down", [])),
            "missing_up_n": len(entity_info.get("missing_up", [])),
            "missing_down_n": len(entity_info.get("missing_down", [])),
            "missing_gene_ratio": entity_info.get("miss_ratio", 0.0),
            "qc_status": "ok" if entity_info.get("miss_ratio", 0) <= float(cfg.get("qc", {}).get("max_missing_gene_ratio", 0.30)) else "too_many_missing_genes",
            "size_check": size_check,
            "missing_up": entity_info.get("missing_up", [])[:50],
            "missing_down": entity_info.get("missing_down", [])[:50],
        },
        "signature_qc": sig_qc,
        "statistics": {
            "enabled": cfg.get("statistics", {}).get("enabled", True),
            "n_permutations": cfg.get("statistics", {}).get("n_permutations", 1000),
            "n_bootstrap": cfg.get("statistics", {}).get("n_bootstrap", 2000),
        },
        "cache": {
            "enabled": bool(cfg.get("cache", {}).get("enabled", True)),
            "cache_dir": cfg.get("cache", {}).get("cache_dir", "data/cache"),
        },
        "notes": {
            "final_reversal_score_more_negative_is_better": True,
            "scoring_mode": cfg.get("scoring", {}).get("mode", "wtcs_like"),
            "ldp3_sign_convention": (
                "z_up<0 = disease-UP genes reversed by drug; "
                "z_down<0 = disease-DOWN genes reversed by drug; "
                "REVERSER = both<0; MIMICKER = both>0"
            ),
            "wtcs_like_definition": "if sign(z_up)==sign(z_down): score=(z_up+z_down)/2; else: 0",
            "direction_categories": "reverser(both<0)|mimicker(both>0)|partial(opposing signs)|orthogonal(zero)",
            "robustness_weight": "median_score * p_reverser * n_factor(log or sqrt) * cl_bonus",
            "fdr_filter": "at least one of fdr-up or fdr-down < threshold",
            "statistics": "permutation p-value + BH-FDR + bootstrap 95% CI + z-normalized effect size",
        },
    }

    # Add API client statistics (if available)
    if client is not None and hasattr(client, "stats"):
        manifest["api_client_stats"] = client.stats

    # Add step-level timing information
    if step_timings is not None:
        manifest["step_timings"] = step_timings

    write_json(out_manifest, manifest)

    logger.info(f"Wrote: {out_rank}")
    logger.info(f"Wrote: {out_detail}")
    logger.info(f"Wrote: {out_manifest}")

    # Summary to stdout
    n_ok = (df_drug["status"] == "ok").sum() if "status" in df_drug.columns else len(df_drug)
    n_sig = (df_drug.get("fdr_bh", pd.Series(dtype=float)) < 0.05).sum() if "fdr_bh" in df_drug.columns else "N/A"
    print(f"\n{'='*60}")
    print(f"SigReverse v0.4.1 — Results Summary")
    print(f"{'='*60}")
    print(f"Disease: {sig.get('name', 'unknown')}")
    print(f"Signatures scored: {len(df_detail)}")
    print(f"Drugs ranked: {n_ok} (status=ok)")
    print(f"Drugs FDR<0.05: {n_sig}")
    if "fdr_pass" in df_detail.columns:
        print(f"Signatures FDR-pass: {df_detail['fdr_pass'].sum()}/{len(df_detail)}")
    if sig_qc.get("ldp3_type_agreement_rate") is not None:
        print(f"LDP3 type agreement: {sig_qc['ldp3_type_agreement_rate']:.1%}")
    print(f"Output: {args.out_dir}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_float(val) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def step_cmap_pipeline(df_detail, cmap_cfg) -> pd.DataFrame:
    """Step 8: Run CMap 4-stage pipeline (ES → WTCS → NCS → Tau)."""
    if not cmap_cfg.get("enabled", True):
        logger.info("CMap pipeline disabled in config, skipping.")
        return pd.DataFrame()

    provider = LDP3ESProvider(df_detail)

    # Load optional Touchstone reference
    reference_ncs = None
    ts_path = cmap_cfg.get("touchstone_path")
    if ts_path and os.path.exists(ts_path):
        try:
            reference_ncs = load_touchstone_reference(ts_path)
            logger.info(f"Loaded Touchstone reference: {len(reference_ncs)} NCS values")
        except Exception as e:
            logger.warning(f"Failed to load Touchstone reference: {e}")

    pipeline = CMapPipeline(
        es_provider=provider,
        ncs_method=cmap_cfg.get("ncs_method", "cell_line_null"),
        tau_aggregation=cmap_cfg.get("tau_aggregation", "quantile_max"),
        reference_ncs=reference_ncs,
        tau_reference_mode=cmap_cfg.get("tau_reference_mode", "bootstrap"),
    )
    pipeline.run()
    return pipeline.to_dataframe()


def step_dose_response(df_detail, dr_cfg) -> pd.DataFrame:
    """Step 9: Dose-response analysis."""
    if not dr_cfg.get("enabled", True):
        logger.info("Dose-response analysis disabled, skipping.")
        return pd.DataFrame()

    return analyze_dose_response(
        df_detail,
        dose_col=dr_cfg.get("dose_col", "meta.pert_dose"),
        dose_unit_col=dr_cfg.get("dose_unit_col", "meta.pert_dose_unit"),
        score_col=dr_cfg.get("score_col", "sig_score"),
    )


def step_drug_standardization(df_drug, std_cfg, cache_dir) -> pd.DataFrame:
    """Step 10: Drug name standardization via PubChem/UniChem."""
    if not std_cfg.get("enabled", False):
        logger.info("Drug standardization disabled, skipping.")
        return df_drug

    from sigreverse.drug_standardization import DrugStandardizer

    cache_path = std_cfg.get("cache_path", os.path.join(cache_dir, "drug_identity_cache.json"))
    standardizer = DrugStandardizer(
        cache_path=cache_path,
        use_unichem=bool(std_cfg.get("use_unichem", True)),
        pubchem_delay=float(std_cfg.get("pubchem_delay_sec", 0.25)),
        unichem_delay=float(std_cfg.get("unichem_delay_sec", 0.30)),
    )

    df_drug = standardizer.standardize_dataframe(df_drug, drug_col="drug")

    if bool(std_cfg.get("deduplicate", True)):
        df_drug = standardizer.deduplicate_by_inchikey(df_drug)

    standardizer.save_cache()
    return df_drug


def step_fusion_ranking(df_drug, df_dr, fusion_cfg) -> pd.DataFrame:
    """Step 12: Multi-source fusion ranking.

    Supports direct KG_Explain V5 output (drug_normalized + final_score with
    drug-disease rows) — auto-detects columns and aggregates to drug level.
    Optional disease_filter narrows KG results to a specific disease.
    """
    if not fusion_cfg.get("enabled", False):
        logger.info("Fusion ranking disabled, skipping.")
        return pd.DataFrame()

    from sigreverse.fusion import (
        FusionRanker, SignatureEvidence, KGExplainEvidence, SafetyEvidence,
    )

    weights = fusion_cfg.get("weights", {})
    ranker = FusionRanker(
        weights=weights,
        normalization=fusion_cfg.get("normalization", "rank"),
    )

    # Add SigReverse evidence
    ranker.add_evidence(SignatureEvidence(df_drug))

    # Add KG_Explain evidence (if available)
    # Supports both pre-processed (drug, final_score) and raw V5 output
    # (drug_normalized, diseaseId, final_score) with optional disease filtering
    kg_path = fusion_cfg.get("kg_scores_path")
    if kg_path and os.path.exists(kg_path):
        disease_filter = fusion_cfg.get("disease_filter")
        ranker.add_evidence(KGExplainEvidence(
            csv_path=kg_path,
            disease_filter=disease_filter,
        ))

    # Add FAERS safety evidence (if available)
    # Supports both pre-aggregated (drug, safety_score) and raw KG_Explain
    # FAERS format (drug_normalized, ae_term, report_count, prr)
    safety_path = fusion_cfg.get("safety_scores_path")
    if safety_path and os.path.exists(safety_path):
        safety_df = pd.read_csv(safety_path)
        ranker.add_evidence(SafetyEvidence(df_safety=safety_df))

    # Add dose-response bonus
    if df_dr is not None and len(df_dr) > 0:
        ranker.set_dose_response(df_dr)

    results = ranker.fuse()
    return ranker.to_dataframe()


def _timed_step(step_num, total_steps, name, func, *args, **kwargs):
    """Execute a pipeline step with timing and structured logging."""
    logger.info(f"Step {step_num}/{total_steps}: {name}...")
    t_start = time.time()
    try:
        result = func(*args, **kwargs)
        elapsed = time.time() - t_start
        logger.info(f"  Step {step_num} completed in {elapsed:.2f}s")
        return result, {"step": step_num, "name": name, "elapsed_sec": round(elapsed, 2), "status": "ok"}
    except Exception as e:
        elapsed = time.time() - t_start
        logger.error(f"  Step {step_num} FAILED after {elapsed:.2f}s: {type(e).__name__}: {e}")
        raise


def main():
    ap = argparse.ArgumentParser(description="SigReverse v0.4.0 — Industrial-grade LINCS/CMap reversal scoring")
    ap.add_argument("--config", required=True, help="YAML config path")

    # Input: either a signature file OR a disease name to fetch from CREEDS
    input_group = ap.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--in", dest="inp", help="disease_signature.json")
    input_group.add_argument("--fetch", dest="fetch_disease",
                             help="Auto-fetch signature from CREEDS (e.g., 'atherosclerosis')")

    ap.add_argument("--out", dest="out_dir", required=True, help="output directory")
    ap.add_argument("--no-stats", action="store_true", help="skip statistical significance (faster)")
    ap.add_argument("--no-cmap", action="store_true", help="skip CMap 4-stage pipeline")
    ap.add_argument("--no-dr", action="store_true", help="skip dose-response analysis")
    ap.add_argument("--top-n", type=int, default=200,
                    help="genes per direction when using --fetch (default: 200)")
    args = ap.parse_args()

    # If --fetch, auto-download signature from CREEDS
    if args.fetch_disease:
        from fetch_disease_signature import (
            creeds_search, creeds_get_signature, merge_signatures,
            single_signature_genes, write_signature_json,
        )
        disease = args.fetch_disease
        logger.info(f"Fetching disease signature for '{disease}' from CREEDS...")

        results = creeds_search(disease)
        if not results:
            raise SystemExit(f"No CREEDS signatures found for '{disease}'. "
                             f"Run: python scripts/fetch_disease_signature.py --list")

        # Fetch all and merge for robustness
        full_sigs = []
        for sig_meta in results:
            full = creeds_get_signature(sig_meta["id"])
            if full:
                full_sigs.append(full)

        if not full_sigs:
            raise SystemExit("Failed to fetch any signatures from CREEDS.")

        if len(full_sigs) > 1:
            up_genes, down_genes, merge_meta = merge_signatures(full_sigs, top_n=args.top_n)
            meta = {"source": "CREEDS", "method": "auto-merge", **merge_meta}
        else:
            up_genes, down_genes = single_signature_genes(full_sigs[0], top_n=args.top_n)
            meta = {"source": "CREEDS", "method": "single", "geo_id": results[0].get("geo_id", "")}

        # Write to output dir
        ensure_dir(args.out_dir)
        auto_sig_path = os.path.join(args.out_dir, "disease_signature_auto.json")
        write_signature_json(auto_sig_path, disease, up_genes, down_genes, meta)
        args.inp = auto_sig_path
        logger.info(f"Auto-fetched signature: {len(up_genes)} up + {len(down_genes)} down genes")

    cfg = load_config(args.config)
    ensure_dir(args.out_dir)
    cache_dir = cfg.get("cache", {}).get("cache_dir", "data/cache")
    cache_enabled = bool(cfg.get("cache", {}).get("enabled", True))
    ensure_dir(cache_dir)

    total_steps = 13
    t0 = time.time()
    step_timings = []

    # Step 1: Load & validate
    load_result, timing = _timed_step(1, total_steps, "Load and validate input signature",
                                       step_load_and_validate, args, cfg)
    step_timings.append(timing)
    sig, up, down = load_result["sig"], load_result["up"], load_result["down"]
    size_check = load_result["size_check"]

    # Step 2: Entity mapping
    ldp3_cfg = cfg["ldp3"]
    client = LDP3Client(
        metadata_api=ldp3_cfg["metadata_api"],
        data_api=ldp3_cfg["data_api"],
        timeout_sec=int(ldp3_cfg.get("timeout_sec", 120)),
        retries=int(ldp3_cfg.get("retries", 3)),
        backoff_sec=float(ldp3_cfg.get("backoff_sec", 2.0)),
    )
    entity_info, timing = _timed_step(2, total_steps, "Map gene symbols to LINCS entity UUIDs",
                                       step_entity_mapping, up, down, client, cache_dir, cache_enabled)
    step_timings.append(timing)

    # Step 3: Enrichment
    df_sig, timing = _timed_step(3, total_steps, "Run LDP3 ranktwosided enrichment",
                                  step_enrichment, entity_info["up_entities"], entity_info["down_entities"],
                                  client, ldp3_cfg, cache_dir, cache_enabled)
    step_timings.append(timing)

    # Step 4: Metadata
    df_detail, timing = _timed_step(4, total_steps, "Fetch signature metadata",
                                     step_fetch_metadata, df_sig, client, cache_dir, cache_enabled)
    step_timings.append(timing)

    # Step 5: Scoring
    scoring_cfg = cfg.get("scoring", {})
    df_detail, timing = _timed_step(5, total_steps, "Score signatures (WTCS-like + FDR filter)",
                                     step_signature_scoring, df_detail, scoring_cfg)
    step_timings.append(timing)

    # Step 6: Drug aggregation
    robustness_cfg = cfg.get("robustness", {})
    df_drug, timing = _timed_step(6, total_steps, "Aggregate to drug-level with robustness weighting",
                                   step_drug_aggregation, df_detail, robustness_cfg)
    step_timings.append(timing)

    # Step 7: Statistical significance
    stats_cfg = cfg.get("statistics", {})
    if args.no_stats:
        stats_cfg["enabled"] = False
    df_drug, timing = _timed_step(7, total_steps, "Compute statistical significance",
                                   step_statistical_significance, df_detail, df_drug, stats_cfg, robustness_cfg)
    step_timings.append(timing)

    # Step 8: CMap 4-stage pipeline (ES → WTCS → NCS → Tau)
    cmap_cfg = cfg.get("cmap_pipeline", {})
    if args.no_cmap:
        cmap_cfg["enabled"] = False
    df_tau, timing = _timed_step(8, total_steps, "Run CMap 4-stage pipeline (Tau scoring)",
                                  step_cmap_pipeline, df_detail, cmap_cfg)
    step_timings.append(timing)
    if len(df_tau) > 0:
        # Rename n_cell_lines to avoid collision with robustness column
        if "n_cell_lines" in df_tau.columns and "n_cell_lines" in df_drug.columns:
            df_tau = df_tau.rename(columns={"n_cell_lines": "tau_n_cell_lines"})
        df_drug = df_drug.merge(df_tau, on="drug", how="left")

    # Step 9: Dose-response analysis
    dr_cfg = cfg.get("dose_response", {})
    if args.no_dr:
        dr_cfg["enabled"] = False
    df_dr, timing = _timed_step(9, total_steps, "Analyze dose-response relationships",
                                 step_dose_response, df_detail, dr_cfg)
    step_timings.append(timing)
    if len(df_dr) > 0:
        df_drug = df_drug.merge(df_dr, on="drug", how="left")

    # Step 10: Drug name standardization
    std_cfg = cfg.get("drug_standardization", {})
    df_drug, timing = _timed_step(10, total_steps, "Drug name standardization",
                                   step_drug_standardization, df_drug, std_cfg, cache_dir)
    step_timings.append(timing)

    # Step 11: QC & flags
    qc_cfg = cfg.get("qc", {})
    qc_result, timing = _timed_step(11, total_steps, "Run QC diagnostics and toxicity flags",
                                     step_qc_and_flags, df_detail, df_drug, qc_cfg)
    step_timings.append(timing)
    df_drug, sig_qc = qc_result

    # Step 12: Fusion ranking (optional)
    fusion_cfg = cfg.get("fusion", {})
    df_fusion, timing = _timed_step(12, total_steps, "Multi-source fusion ranking",
                                     step_fusion_ranking, df_drug, df_dr, fusion_cfg)
    step_timings.append(timing)
    if len(df_fusion) > 0:
        write_csv(os.path.join(args.out_dir, "fusion_ranking.csv"), df_fusion)

    # Step 13: Write outputs
    logger.info(f"Step 13/{total_steps}: Writing output files...")
    step_write_outputs(
        args, cfg, df_drug, df_detail, sig_qc, entity_info, size_check, sig,
        client=client, step_timings=step_timings,
    )

    elapsed = time.time() - t0
    logger.info(f"Pipeline completed in {elapsed:.1f}s")
    logger.info(f"  API stats: {client.stats}")
    logger.info(f"  Steps: {len(step_timings)} completed")


if __name__ == "__main__":
    main()
