#!/usr/bin/env python3
"""Evaluation script for evidence extraction quality.

Loads gold-standard annotations and evaluates extraction methods against them.

Usage:
    python scripts/eval_extraction.py --gold data/gold_standard/gold_standard_v1.csv
    python scripts/eval_extraction.py --gold data/gold_standard/gold_standard_v1.csv --dossier-dir output/step6/dossiers
    python scripts/eval_extraction.py --bootstrap output/step6/dossiers --out data/gold_standard/bootstrapped.csv
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dr.evaluation.gold_standard import (
    load_gold_standard,
    save_gold_standard,
    bootstrap_from_dossiers,
)
from src.dr.evaluation.metrics import evaluate_extraction, ExtractionMetrics


def extract_predictions_from_dossiers(dossier_dir: str) -> list:
    """Extract all evidence items from dossier JSONs as prediction dicts."""
    predictions = []
    dpath = Path(dossier_dir)
    if not dpath.exists():
        print(f"ERROR: Dossier directory not found: {dossier_dir}")
        return []

    for jf in sorted(dpath.glob("*.json")):
        try:
            with open(jf, "r", encoding="utf-8") as f:
                dossier = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: Failed to read {jf.name}: {e}")
            continue

        drug_name = dossier.get("canonical_name", "")
        llm_data = dossier.get("llm_structured", {})

        for ev in llm_data.get("supporting_evidence", []):
            predictions.append({
                "pmid": str(ev.get("pmid", "")).strip(),
                "drug_name": drug_name,
                "direction": str(ev.get("direction", "")).lower(),
                "model": str(ev.get("model", "")).lower(),
                "endpoint": str(ev.get("endpoint", "")),
                "confidence": ev.get("confidence", 0),
                "source": ev.get("source", "unknown"),
            })

        for ev in llm_data.get("harm_or_neutral_evidence", []):
            predictions.append({
                "pmid": str(ev.get("pmid", "")).strip(),
                "drug_name": drug_name,
                "direction": str(ev.get("direction", "")).lower(),
                "model": str(ev.get("model", "")).lower(),
                "endpoint": str(ev.get("endpoint", "")),
                "confidence": ev.get("confidence", 0),
                "source": ev.get("source", "unknown"),
            })

    return predictions


def print_report(metrics: ExtractionMetrics) -> None:
    """Print evaluation report to terminal."""
    print(metrics.summary())
    print()


def main():
    ap = argparse.ArgumentParser(description="Evaluate evidence extraction quality")
    ap.add_argument("--gold", default="data/gold_standard/gold_standard_v1.csv",
                    help="Path to gold-standard CSV")
    ap.add_argument("--dossier-dir", default="output/step6/dossiers",
                    help="Path to dossier directory for predictions")
    ap.add_argument("--fields", nargs="+", default=["direction", "model"],
                    help="Fields to evaluate (default: direction model)")
    ap.add_argument("--bootstrap", default=None,
                    help="Bootstrap gold standard from dossier dir (instead of evaluating)")
    ap.add_argument("--min-confidence", type=float, default=0.7,
                    help="Min confidence for bootstrap (default: 0.7)")
    ap.add_argument("--out", default=None,
                    help="Output path for bootstrap results or JSON report")
    args = ap.parse_args()

    # Bootstrap mode
    if args.bootstrap:
        print(f"Bootstrapping gold standard from: {args.bootstrap}")
        records = bootstrap_from_dossiers(args.bootstrap, min_confidence=args.min_confidence)
        out_path = args.out or "data/gold_standard/bootstrapped.csv"
        save_gold_standard(records, out_path)
        print(f"Saved {len(records)} records to {out_path}")
        return

    # Evaluation mode
    print(f"Loading gold standard: {args.gold}")
    gold = load_gold_standard(args.gold)
    print(f"Loaded {len(gold)} gold-standard records")

    print(f"\nExtracting predictions from: {args.dossier_dir}")
    predictions = extract_predictions_from_dossiers(args.dossier_dir)
    print(f"Extracted {len(predictions)} prediction records")

    if not predictions:
        print("ERROR: No predictions found. Check dossier directory.")
        sys.exit(1)

    print(f"\nEvaluating fields: {args.fields}")
    metrics = evaluate_extraction(predictions, gold, fields=args.fields)

    print()
    print_report(metrics)

    # Save JSON report if requested
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(metrics.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"\nJSON report saved to: {args.out}")


if __name__ == "__main__":
    main()
