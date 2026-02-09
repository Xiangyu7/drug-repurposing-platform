"""Benchmark evaluation for drug reversal rankings.

Evaluates ranking quality against known drug-disease associations
from gold-standard datasets (DrugBank, RepurposeDB, TTD, etc.).

Usage:
    python -m sigreverse.evaluation.benchmark \\
        --ranking data/output/drug_reversal_rank.csv \\
        --gold data/benchmark/gold_standard.csv \\
        --ks 5,10,20
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Dict, List, Set, Any, Optional

import pandas as pd

from .metrics import evaluate_ranking

logger = logging.getLogger("sigreverse.evaluation.benchmark")


def load_gold_standard(path: str, drug_col: str = "drug", label_col: str = "label") -> Set[str]:
    """Load gold-standard drug-disease associations.

    Expected CSV format:
        drug,label
        atorvastatin,1
        simvastatin,1
        aspirin,1

    Args:
        path: Path to CSV file.
        drug_col: Column name for drug identifier.
        label_col: Column name for positive label (1 = known effective).

    Returns:
        Set of known positive drug names.
    """
    df = pd.read_csv(path)
    positives = set(df.loc[df[label_col] == 1, drug_col].str.strip().str.lower())
    logger.info(f"Loaded {len(positives)} gold-standard positives from {path}")
    return positives


def run_benchmark(
    df_drug: pd.DataFrame,
    gold_positives: Set[str],
    drug_col: str = "drug",
    score_col: str = "final_reversal_score",
    ks: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Run full benchmark evaluation.

    Args:
        df_drug: Drug-level ranking DataFrame.
        gold_positives: Set of known positive drug names (lowercase).
        drug_col: Column name for drug identifier.
        score_col: Column name for score (lower = better).
        ks: K values for top-K metrics.

    Returns:
        Dict with all evaluation results and metadata.
    """
    if ks is None:
        ks = [5, 10, 20, 50]

    # Sort by score ascending (more negative = better)
    df_sorted = df_drug.sort_values(score_col, ascending=True)
    ranked_drugs = df_sorted[drug_col].str.strip().str.lower().tolist()

    # Evaluate
    metrics = evaluate_ranking(ranked_drugs, gold_positives, ks=ks)

    # Find ranks of known positives
    positive_ranks = {}
    for i, d in enumerate(ranked_drugs):
        if d in gold_positives:
            positive_ranks[d] = i + 1  # 1-indexed

    missing_from_ranking = gold_positives - set(ranked_drugs)

    results = {
        "metrics": metrics,
        "positive_ranks": positive_ranks,
        "missing_from_ranking": sorted(missing_from_ranking),
        "n_total_drugs": len(ranked_drugs),
        "n_positives_found": len(positive_ranks),
        "n_positives_missing": len(missing_from_ranking),
    }

    # Log summary
    logger.info(f"Benchmark results:")
    logger.info(f"  MRR: {metrics['mrr']:.4f}")
    logger.info(f"  MAP: {metrics['map']:.4f}")
    logger.info(f"  AUROC: {metrics['auroc']:.4f}")
    logger.info(f"  AUPRC: {metrics['auprc']:.4f}")
    for k in ks:
        logger.info(f"  Hit@{k}: {metrics[f'hit@{k}']:.2f}  P@{k}: {metrics[f'precision@{k}']:.4f}  NDCG@{k}: {metrics[f'ndcg@{k}']:.4f}")
    logger.info(f"  Positive ranks: {positive_ranks}")
    if missing_from_ranking:
        logger.info(f"  Missing from ranking: {sorted(missing_from_ranking)}")

    return results


def main():
    ap = argparse.ArgumentParser(description="Benchmark drug reversal ranking")
    ap.add_argument("--ranking", required=True, help="drug_reversal_rank.csv")
    ap.add_argument("--gold", required=True, help="gold_standard.csv")
    ap.add_argument("--ks", default="5,10,20,50", help="comma-separated K values")
    ap.add_argument("--output", default=None, help="output JSON path")
    args = ap.parse_args()

    ks = [int(k) for k in args.ks.split(",")]

    df_drug = pd.read_csv(args.ranking)
    gold = load_gold_standard(args.gold)

    results = run_benchmark(df_drug, gold, ks=ks)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"Results written to {args.output}")
    else:
        print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
