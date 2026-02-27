#!/usr/bin/env python3
"""Merge SigReverse reversal_score into bridge CSV.

After kg_explain generates a bridge CSV (bridge_repurpose_cross.csv or
bridge_origin_reassess.csv), this script enriches it with the
``reversal_score`` column from SigReverse's ``drug_reversal_rank.csv``.

The merge key is drug name (lowercased): bridge ``canonical_name`` matches
SigReverse ``drug``.  Drugs without a SigReverse match keep NaN.

Usage:
    python ops/merge_sig_to_bridge.py \
        --bridge kg_explain/output/atherosclerosis/bridge_repurpose_cross.csv \
        --sig-rank runtime/work/atherosclerosis/sigreverse_output/drug_reversal_rank.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def merge(bridge_path: str, sig_rank_path: str) -> None:
    """Merge reversal_score into bridge CSV in-place."""
    bp = Path(bridge_path)
    sp = Path(sig_rank_path)

    if not bp.exists():
        logger.error("Bridge CSV not found: %s", bp)
        sys.exit(1)

    if not sp.exists():
        logger.info("SigReverse rank not found (%s), skipping merge", sp)
        return

    bridge = pd.read_csv(bp, dtype=str)
    sig = pd.read_csv(sp)

    if "drug" not in sig.columns or "final_reversal_score" not in sig.columns:
        logger.warning("SigReverse CSV missing expected columns, skipping merge")
        return

    # Build lookup: lowered drug name -> reversal score
    sig_map = {}
    for _, row in sig.iterrows():
        name = str(row["drug"]).strip().lower()
        score = row["final_reversal_score"]
        if name and pd.notna(score):
            sig_map[name] = float(score)

    logger.info("SigReverse drugs loaded: %d", len(sig_map))

    # Merge into bridge
    bridge["reversal_score"] = bridge["canonical_name"].apply(
        lambda x: sig_map.get(str(x).strip().lower())
    )

    matched = bridge["reversal_score"].notna().sum()
    logger.info("Merged reversal_score: %d / %d drugs matched", matched, len(bridge))

    bridge.to_csv(bp, index=False)
    logger.info("Bridge updated: %s", bp)


def main():
    ap = argparse.ArgumentParser(description="Merge SigReverse reversal_score into bridge CSV")
    ap.add_argument("--bridge", required=True, help="Bridge CSV path (updated in-place)")
    ap.add_argument("--sig-rank", required=True, help="SigReverse drug_reversal_rank.csv path")
    args = ap.parse_args()
    merge(args.bridge, args.sig_rank)


if __name__ == "__main__":
    main()
