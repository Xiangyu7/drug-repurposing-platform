#!/usr/bin/env python3
"""Build a manual audit queue from rejected candidates.

Purpose:
- Prevent precision-only filtering from missing true repurposing opportunities.
- Sample from NO-GO (and optional MAYBE-explore) for human back-audit.
"""

import argparse
from pathlib import Path
import random

import pandas as pd


def _reason_bucket(reasons: str) -> str:
    text = (reasons or "").lower()
    if "benefit<" in text:
        return "low_benefit"
    if "pmids<" in text:
        return "low_coverage"
    if "harm_ratio" in text or "safety" in text:
        return "risk_or_harm"
    if "score<" in text:
        return "low_score"
    if "explore_track" in text:
        return "explore_override"
    return "other"


def _stratified_sample(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    if df.empty or n <= 0:
        return df.head(0).copy()
    if len(df) <= n:
        return df.copy()

    rng = random.Random(seed)
    out_parts = []
    groups = {k: g.copy() for k, g in df.groupby("reason_bucket")}
    keys = sorted(groups.keys())

    # Round-robin draw to preserve reason diversity.
    while len(out_parts) < n and keys:
        next_keys = []
        for key in keys:
            if len(out_parts) >= n:
                break
            g = groups[key]
            if g.empty:
                continue
            idx = rng.randrange(0, len(g))
            out_parts.append(g.iloc[[idx]])
            groups[key] = g.drop(g.index[idx]).reset_index(drop=True)
            if not groups[key].empty:
                next_keys.append(key)
        keys = next_keys

    if not out_parts:
        return df.sample(n=n, random_state=seed)
    return pd.concat(out_parts, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build reject-audit queue from Step7 outputs")
    ap.add_argument("--step7-dir", default="output/step7", help="Step7 output directory")
    ap.add_argument("--n", type=int, default=30, help="Target sample size")
    ap.add_argument("--seed", type=int, default=42, help="Random seed")
    ap.add_argument(
        "--include-maybe-explore",
        type=int,
        default=1,
        help="1=include MAYBE with decision_channel=explore in audit pool",
    )
    ap.add_argument(
        "--out",
        default="output/quality/reject_audit_queue.csv",
        help="Output CSV path",
    )
    args = ap.parse_args()

    step7_dir = Path(args.step7_dir).resolve()
    gating_path = step7_dir / "step7_gating_decision.csv"
    scores_path = step7_dir / "step7_scores.csv"

    if not gating_path.exists():
        raise FileNotFoundError(f"Missing Step7 gating CSV: {gating_path}")

    gating = pd.read_csv(gating_path)
    scores = pd.read_csv(scores_path) if scores_path.exists() else pd.DataFrame()

    if gating.empty:
        raise ValueError("Step7 gating CSV is empty")

    pool = gating[gating["gate_decision"].astype(str).str.upper() == "NO-GO"].copy()
    if int(args.include_maybe_explore) == 1 and "decision_channel" in gating.columns:
        explore = gating[
            (gating["gate_decision"].astype(str).str.upper() == "MAYBE")
            & (gating["decision_channel"].astype(str).str.lower() == "explore")
        ].copy()
        pool = pd.concat([pool, explore], ignore_index=True)

    if pool.empty:
        raise ValueError("No reject candidates found for audit queue")

    pool["reason_bucket"] = pool.get("gate_reasons", "").astype(str).map(_reason_bucket)
    sampled = _stratified_sample(pool, n=int(args.n), seed=int(args.seed)).copy()

    if not scores.empty and {"drug_id", "canonical_name"}.issubset(scores.columns):
        keep_cols = ["drug_id", "canonical_name", "total_score_0_100"]
        sampled = sampled.merge(scores[keep_cols], on=["drug_id", "canonical_name"], how="left")

    sampled["audit_status"] = "PENDING"
    sampled["auditor"] = ""
    sampled["audit_verdict"] = ""
    sampled["audit_notes"] = ""

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sampled.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"Built reject audit queue: {out_path}")
    print(f" - pool_size: {len(pool)}")
    print(f" - sample_size: {len(sampled)}")
    print(" - next: manually review and feed corrections into gold_standard_v1.csv")


if __name__ == "__main__":
    main()

