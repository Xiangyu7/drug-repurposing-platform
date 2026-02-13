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
import hashlib
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


def split_gold_records(records: list, holdout_ratio: float, seed: int, split_key: str) -> tuple[list, list]:
    """Deterministically split gold records into train/holdout sets."""
    if holdout_ratio <= 0.0:
        return records, []

    ratio = max(0.0, min(0.95, holdout_ratio))
    train = []
    holdout = []

    for rec in records:
        if split_key == "pmid":
            key = str(getattr(rec, "pmid", "")).strip()
        elif split_key == "pair":
            key = f"{str(getattr(rec, 'pmid', '')).strip()}::{str(getattr(rec, 'drug_name', '')).strip().lower()}"
        else:
            key = str(getattr(rec, "drug_name", "")).strip().lower()

        digest = hashlib.sha1(f"{seed}::{key}".encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        if bucket < ratio:
            holdout.append(rec)
        else:
            train.append(rec)

    return train, holdout


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
    ap.add_argument("--holdout-ratio", type=float, default=0.0,
                    help="Optional holdout ratio for anti-overfit eval (0-0.95)")
    ap.add_argument("--holdout-seed", type=int, default=42,
                    help="Seed used for deterministic holdout split")
    ap.add_argument("--split-key", choices=["drug", "pmid", "pair"], default="drug",
                    help="How to split holdout: by drug, pmid, or pair")
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
    train_gold, holdout_gold = split_gold_records(
        gold,
        holdout_ratio=float(args.holdout_ratio),
        seed=int(args.holdout_seed),
        split_key=args.split_key,
    )

    report_payload = {
        "config": {
            "gold": args.gold,
            "dossier_dir": args.dossier_dir,
            "fields": args.fields,
            "holdout_ratio": float(args.holdout_ratio),
            "holdout_seed": int(args.holdout_seed),
            "split_key": args.split_key,
        },
        "all": {},
    }

    metrics_all = evaluate_extraction(predictions, gold, fields=args.fields)
    print("\n[All records]")
    print_report(metrics_all)
    report_payload["all"] = metrics_all.to_dict()

    if holdout_gold:
        print(
            f"[Holdout split] train={len(train_gold)} holdout={len(holdout_gold)} "
            f"(ratio={float(args.holdout_ratio):.2f}, key={args.split_key}, seed={int(args.holdout_seed)})"
        )
        metrics_train = evaluate_extraction(predictions, train_gold, fields=args.fields)
        metrics_holdout = evaluate_extraction(predictions, holdout_gold, fields=args.fields)

        print("\n[Train subset]")
        print_report(metrics_train)
        print("[Holdout subset]")
        print_report(metrics_holdout)

        report_payload["train"] = metrics_train.to_dict()
        report_payload["holdout"] = metrics_holdout.to_dict()
    else:
        report_payload["train"] = report_payload["all"]
        report_payload["holdout"] = {}

    # Save JSON report if requested
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload_to_save = report_payload
        # Backward-compatible output shape for existing tooling.
        if float(args.holdout_ratio) <= 0.0 and not holdout_gold:
            payload_to_save = metrics_all.to_dict()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload_to_save, f, indent=2, ensure_ascii=False)
        print(f"\nJSON report saved to: {args.out}")


if __name__ == "__main__":
    main()
