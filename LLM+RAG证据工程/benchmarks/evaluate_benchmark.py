#!/usr/bin/env python3
"""
Drug Repurposing Benchmark Evaluation

Evaluates pipeline output against known positive/negative drug sets.
Supports two input formats:
  1. bridge_repurpose_cross.csv (KG bridge output)
  2. drug_disease_rank.csv (KG full ranking, filtered by disease_id)

Metrics:
  - Recall@K (K=10, 20, 50, all): fraction of positives found in top-K
  - Negative rejection rate: fraction of negatives NOT in top-K
  - Mean reciprocal rank (MRR) of positives
  - Per-drug hit/miss detail

Usage:
  python evaluate_benchmark.py --benchmark benchmark_drugs.yaml --disease rheumatoid_arthritis \
      --rank_csv ../kg_explain/output/rheumatoid_arthritis/signature/drug_disease_rank.csv \
      [--disease_id EFO_0000685] [--output report.json]
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.dr.common.text import strip_salt_form


def load_benchmark(yaml_path: str, disease: str) -> dict:
    with open(yaml_path) as f:
        benchmarks = yaml.safe_load(f)
    if disease not in benchmarks:
        print(f"Error: disease '{disease}' not in benchmark file")
        print(f"Available: {list(benchmarks.keys())}")
        sys.exit(1)
    return benchmarks[disease]


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("-", "").replace(" ", "")


def build_alias_set(drugs: List[dict]) -> Dict[str, str]:
    """Map all normalized aliases to canonical benchmark name."""
    alias_map = {}
    for d in drugs:
        canon = d["name"]
        alias_map[normalize_name(canon)] = canon
        for alias in d.get("aliases", []):
            alias_map[normalize_name(alias)] = canon
    return alias_map


def load_ranked_drugs(csv_path: str, disease_id: Optional[str] = None) -> List[dict]:
    """Load ranked drugs from bridge or drug_disease_rank CSV.

    Returns list of dicts sorted by score descending, with keys:
      drug_name, score, rank
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # Detect format
        is_bridge = "canonical_name" in headers
        is_ddr = "drug_normalized" in headers

        for row in reader:
            if is_ddr:
                # drug_disease_rank.csv: filter by disease_id if provided
                if disease_id and row.get("diseaseId", "") != disease_id:
                    continue
                name = row.get("drug_normalized", "")
                score = float(row.get("final_score", 0))
            elif is_bridge:
                name = row.get("canonical_name", "")
                score = float(row.get("final_score", 0))
            else:
                print(f"Error: unrecognized CSV format. Headers: {headers}")
                sys.exit(1)

            if name.strip():
                rows.append({"drug_name": name.strip(), "score": score})

    # Sort by score descending
    rows.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate by normalized name (keep highest score)
    seen = set()
    deduped = []
    for r in rows:
        norm = normalize_name(r["drug_name"])
        if norm not in seen:
            seen.add(norm)
            r["rank"] = len(deduped) + 1
            deduped.append(r)

    return deduped


def evaluate(
    ranked: List[dict],
    positives: List[dict],
    negatives: List[dict],
    k_values: List[int] = [10, 20, 50],
) -> dict:
    """Compute benchmark metrics."""

    pos_aliases = build_alias_set(positives)
    neg_aliases = build_alias_set(negatives)

    # Map ranked drugs to benchmark names (exact + salt-stripped parent)
    ranked_norm = {normalize_name(r["drug_name"]): r for r in ranked}
    # Also map by salt-stripped parent name (e.g. "tofacitinib citrate" → "tofacitinib")
    for r in ranked:
        parent = strip_salt_form(r["drug_name"])
        parent_norm = normalize_name(parent)
        if parent_norm and parent_norm not in ranked_norm:
            ranked_norm[parent_norm] = r

    # --- Positive evaluation ---
    pos_results = []
    reciprocal_ranks = []
    for p in positives:
        canon = p["name"]
        all_names = [normalize_name(canon)] + [normalize_name(a) for a in p.get("aliases", [])]

        found = False
        best_rank = None
        best_name = None
        for nm in all_names:
            if nm in ranked_norm:
                r = ranked_norm[nm]
                if best_rank is None or r["rank"] < best_rank:
                    best_rank = r["rank"]
                    best_name = r["drug_name"]
                found = True

        pos_results.append({
            "benchmark_name": canon,
            "target": p.get("target", ""),
            "evidence_level": p.get("evidence_level", ""),
            "modality": p.get("modality", "small_molecule"),
            "found": found,
            "rank": best_rank,
            "matched_as": best_name,
            "score": ranked_norm[normalize_name(best_name)]["score"] if best_name and normalize_name(best_name) in ranked_norm else None,
        })
        if found and best_rank:
            reciprocal_ranks.append(1.0 / best_rank)
        else:
            reciprocal_ranks.append(0.0)

    # Recall@K
    recall_at_k = {}
    for k in k_values + [len(ranked)]:
        label = f"recall@{k}" if k != len(ranked) else "recall@all"
        hits = sum(1 for p in pos_results if p["found"] and p["rank"] is not None and p["rank"] <= k)
        recall_at_k[label] = hits / len(positives) if positives else 0

    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0

    # --- Negative evaluation ---
    neg_results = []
    for n in negatives:
        canon = n["name"]
        all_names = [normalize_name(canon)] + [normalize_name(a) for a in n.get("aliases", [])]

        found = False
        best_rank = None
        best_name = None
        for nm in all_names:
            if nm in ranked_norm:
                r = ranked_norm[nm]
                if best_rank is None or r["rank"] < best_rank:
                    best_rank = r["rank"]
                    best_name = r["drug_name"]
                found = True

        neg_results.append({
            "benchmark_name": canon,
            "reason": n.get("reason", ""),
            "found": found,
            "rank": best_rank,
            "matched_as": best_name,
        })

    # Negative rejection rate at each K
    neg_rejection = {}
    for k in k_values:
        in_topk = sum(1 for n in neg_results if n["found"] and n["rank"] is not None and n["rank"] <= k)
        neg_rejection[f"neg_rejection@{k}"] = 1.0 - (in_topk / len(negatives)) if negatives else 1.0

    return {
        "total_ranked_drugs": len(ranked),
        "n_positives": len(positives),
        "n_negatives": len(negatives),
        "positives_found": sum(1 for p in pos_results if p["found"]),
        "recall": recall_at_k,
        "mrr": round(mrr, 4),
        "negative_rejection": neg_rejection,
        "positive_details": pos_results,
        "negative_details": neg_results,
    }


def print_report(disease: str, result: dict):
    print(f"\n{'='*60}")
    print(f"  Benchmark Report: {disease}")
    print(f"{'='*60}")
    print(f"Total ranked drugs: {result['total_ranked_drugs']}")
    print(f"Positives: {result['positives_found']}/{result['n_positives']} found")
    print(f"MRR: {result['mrr']}")
    print()

    # Recall table
    print("Recall@K:")
    for k, v in result["recall"].items():
        bar = "#" * int(v * 20)
        print(f"  {k:>12s}: {v:.1%}  {bar}")
    print()

    # Positive details
    print("Positive drugs:")
    for p in sorted(result["positive_details"], key=lambda x: x["rank"] or 9999):
        status = f"rank={p['rank']}" if p["found"] else "MISSING"
        mod = f" [{p['modality']}]" if p.get("modality") != "small_molecule" else ""
        print(f"  {'✓' if p['found'] else '✗'} {p['benchmark_name']:25s} ({p['target']:15s}) {status}{mod}")
    print()

    # Negative rejection
    print("Negative rejection:")
    for k, v in result["negative_rejection"].items():
        print(f"  {k:>20s}: {v:.1%}")
    print()

    # Negative details
    neg_in = [n for n in result["negative_details"] if n["found"]]
    if neg_in:
        print("WARNING - Negatives found in ranking:")
        for n in neg_in:
            print(f"  ! {n['benchmark_name']:25s} rank={n['rank']} ({n['reason']})")
    else:
        print("All negatives correctly excluded.")
    print()


def main():
    ap = argparse.ArgumentParser(description="Evaluate drug repurposing benchmark")
    ap.add_argument("--benchmark", default=str(Path(__file__).parent / "benchmark_drugs.yaml"))
    ap.add_argument("--disease", required=True)
    ap.add_argument("--rank_csv", required=True, help="bridge_repurpose_cross.csv or drug_disease_rank.csv")
    ap.add_argument("--disease_id", default=None, help="Filter drug_disease_rank.csv by disease ID")
    ap.add_argument("--output", default=None, help="Write JSON report")
    ap.add_argument("--k", default="10,20,50", help="Comma-separated K values for Recall@K")
    args = ap.parse_args()

    bench = load_benchmark(args.benchmark, args.disease)
    k_values = [int(x) for x in args.k.split(",")]

    # Auto-detect disease_id from benchmark if not provided
    disease_id = args.disease_id or bench.get("disease_id")

    ranked = load_ranked_drugs(args.rank_csv, disease_id)
    result = evaluate(ranked, bench["positives"], bench["negatives"], k_values)

    print_report(args.disease, result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"disease": args.disease, **result}, f, indent=2, default=str)
        print(f"Report saved to: {args.output}")


if __name__ == "__main__":
    main()
