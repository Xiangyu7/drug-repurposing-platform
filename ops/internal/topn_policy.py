#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Top-N policy utilities for industrial budget-aware route scheduling."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


SCORE_COLUMN_PRIORITY = ("rank_score", "max_mechanism_score", "final_score")


@dataclass(frozen=True)
class DecisionConfig:
    route: str
    profile: str
    stage: str
    topk: int
    configured_topn: str
    stage1_min: int
    stage1_max: int
    cap: int
    expand_ratio: float
    fallback_min: int
    previous_topn: Optional[int]


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_auto(configured_topn: str) -> bool:
    return str(configured_topn).strip().lower() == "auto"


def _select_score_column(df: pd.DataFrame) -> Tuple[Optional[str], pd.Series]:
    for col in SCORE_COLUMN_PRIORITY:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().any():
            return col, s.fillna(0.0)

    # Fallback: first numeric-like column
    for col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().any():
            return col, s.fillna(0.0)
    return None, pd.Series(dtype=float)


def _compute_n50(scores_desc: pd.Series) -> int:
    if scores_desc.empty:
        return 1
    total = float(scores_desc.sum())
    if total <= 0:
        return 1
    cumulative = scores_desc.cumsum() / total
    reached = cumulative[cumulative >= 0.50]
    if reached.empty:
        return int(len(scores_desc))
    return int(reached.index[0]) + 1


def _compute_ratio_count(scores_desc: pd.Series, ratio: float) -> int:
    if scores_desc.empty:
        return 1
    top_score = float(scores_desc.iloc[0])
    if top_score <= 0:
        return 1
    threshold = ratio * top_score
    return int((scores_desc >= threshold).sum())


def decide_topn(bridge_df: pd.DataFrame, cfg: DecisionConfig) -> Dict[str, Any]:
    nrows = int(len(bridge_df))
    if nrows <= 0:
        raise ValueError("bridge has no rows")

    mode = "manual"
    score_col, raw_scores = _select_score_column(bridge_df)
    scores_desc = raw_scores.sort_values(ascending=False).reset_index(drop=True) if score_col else pd.Series(dtype=float)

    out: Dict[str, Any] = {
        "ok": True,
        "route": cfg.route,
        "profile": cfg.profile,
        "stage": cfg.stage,
        "configured_topn": cfg.configured_topn,
        "bridge_rows": nrows,
        "topk": int(cfg.topk),
        "score_column": score_col or "",
        "constraints": {
            "stage1_min": int(cfg.stage1_min),
            "stage1_max": int(cfg.stage1_max),
            "cap": int(cfg.cap),
            "expand_ratio": float(cfg.expand_ratio),
            "fallback_min": int(cfg.fallback_min),
        },
        "metrics": {},
        "should_expand": False,
    }

    if not _is_auto(cfg.configured_topn):
        configured = _to_int(cfg.configured_topn, cfg.fallback_min)
        mode = "manual"
        if configured <= 0:
            resolved = nrows
            reason = "manual_topn_leq_zero_use_all_rows"
        else:
            resolved = _clamp(configured, 1, nrows)
            reason = "manual_numeric_topn"
        out.update(
            {
                "mode": mode,
                "resolved_topn": int(resolved),
                "reason": reason,
                "quality_gate_enabled": False,
            }
        )
        return out

    if cfg.stage == "stage1":
        mode = "auto_stage1"
        n50 = _compute_n50(scores_desc) if score_col else cfg.fallback_min
        lower = max(int(cfg.stage1_min), int(cfg.topk) + 2)
        upper = int(cfg.stage1_max)
        if upper < lower:
            upper = lower
        if nrows < lower:
            resolved = nrows
            reason = "bridge_rows_below_stage1_lower_bound"
        else:
            resolved = _clamp(int(n50), lower, min(upper, nrows))
            reason = "stage1_n50_clamped"
        out["metrics"] = {
            "n50": int(n50),
            "top_score": float(scores_desc.iloc[0]) if not scores_desc.empty else 0.0,
        }
        out.update(
            {
                "mode": mode,
                "resolved_topn": int(resolved),
                "reason": reason,
                "quality_gate_enabled": True,
            }
        )
        return out

    if cfg.stage != "stage2":
        raise ValueError(f"unsupported stage: {cfg.stage}")
    if cfg.previous_topn is None:
        raise ValueError("stage2 requires previous_topn")

    mode = "auto_stage2"
    n_ratio = _compute_ratio_count(scores_desc, cfg.expand_ratio) if score_col else cfg.fallback_min
    candidate = min(max(int(n_ratio), int(cfg.previous_topn)), int(cfg.cap), nrows)
    should_expand = int(candidate) > int(cfg.previous_topn)
    out["metrics"] = {
        "n_ratio": int(n_ratio),
        "top_score": float(scores_desc.iloc[0]) if not scores_desc.empty else 0.0,
    }
    out.update(
        {
            "mode": mode,
            "resolved_topn": int(candidate),
            "reason": "stage2_ratio_cap" if should_expand else "stage2_no_further_expand",
            "should_expand": bool(should_expand),
            "previous_topn": int(cfg.previous_topn),
            "quality_gate_enabled": False,
        }
    )
    return out


def evaluate_quality(
    step7_cards: Path,
    step8_shortlist: Path,
    topk: int,
    min_go: int,
    route: str,
    stage: str,
) -> Dict[str, Any]:
    errors: List[str] = []
    shortlist_rows = 0
    shortlist_go_count = 0
    cards_total = 0
    cards_go_count = 0
    cards_maybe_count = 0

    if not step8_shortlist.exists():
        errors.append(f"missing shortlist: {step8_shortlist}")
    else:
        s8 = pd.read_csv(step8_shortlist)
        shortlist_rows = int(len(s8))
        go_col = "gate" if "gate" in s8.columns else ("gate_decision" if "gate_decision" in s8.columns else None)
        if go_col:
            shortlist_go_count = int((s8[go_col].astype(str) == "GO").sum())
        else:
            errors.append("shortlist missing gate column")

    if not step7_cards.exists():
        errors.append(f"missing step7 cards: {step7_cards}")
    else:
        cards = json.loads(step7_cards.read_text(encoding="utf-8"))
        if isinstance(cards, list):
            cards_total = int(len(cards))
            cards_go_count = int(sum(1 for row in cards if str((row or {}).get("gate_decision") or (row or {}).get("gate")) == "GO"))
            cards_maybe_count = int(sum(1 for row in cards if str((row or {}).get("gate_decision") or (row or {}).get("gate")) == "MAYBE"))
        else:
            errors.append("step7 cards payload is not a list")

    pass_rows = shortlist_rows >= int(topk)
    pass_go = shortlist_go_count >= int(min_go)
    quality_passed = pass_rows and pass_go and not errors
    trigger_stage2, reasons = should_trigger_stage2(
        shortlist_rows=shortlist_rows,
        topk=int(topk),
        shortlist_go_count=shortlist_go_count,
        min_go=int(min_go),
    )
    if errors:
        reasons.extend(errors)
        trigger_stage2 = True
        quality_passed = False

    return {
        "ok": True,
        "route": route,
        "stage": stage,
        "topk": int(topk),
        "min_go": int(min_go),
        "shortlist_rows": int(shortlist_rows),
        "shortlist_go_count": int(shortlist_go_count),
        "step7_cards_total": int(cards_total),
        "step7_cards_go_count": int(cards_go_count),
        "step7_cards_maybe_count": int(cards_maybe_count),
        "pass_shortlist_rows": bool(pass_rows),
        "pass_go_threshold": bool(pass_go),
        "quality_passed": bool(quality_passed),
        "trigger_stage2": bool(trigger_stage2),
        "reasons": reasons,
    }


def should_trigger_stage2(
    shortlist_rows: int,
    topk: int,
    shortlist_go_count: int,
    min_go: int,
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if shortlist_rows < topk:
        reasons.append(f"shortlist_rows<{topk}")
    if shortlist_go_count < min_go:
        reasons.append(f"go_count<{min_go}")
    return (len(reasons) > 0), reasons


def stage2_allowed(
    stage2_enable: bool,
    configured_topn: str,
    expand_round: int,
    max_expand_rounds: int,
) -> bool:
    if not stage2_enable:
        return False
    if not _is_auto(configured_topn):
        return False
    return int(expand_round) < int(max_expand_rounds)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _cmd_decide(args: argparse.Namespace) -> int:
    fallback_min = _to_int(args.fallback_min, 1)
    previous_topn = _to_int(args.previous_topn, 0) if args.previous_topn is not None else None
    try:
        df = pd.read_csv(args.bridge_csv, dtype=str).fillna("")
        cfg = DecisionConfig(
            route=str(args.route),
            profile=str(args.profile),
            stage=str(args.stage),
            topk=_to_int(args.topk, 1),
            configured_topn=str(args.configured_topn),
            stage1_min=_to_int(args.stage1_min, 1),
            stage1_max=_to_int(args.stage1_max, 1),
            cap=_to_int(args.cap, 1),
            expand_ratio=_to_float(args.expand_ratio, 0.30),
            fallback_min=fallback_min,
            previous_topn=previous_topn,
        )
        payload = decide_topn(df, cfg)
    except Exception as exc:
        # Degraded mode (v2): instead of falling back to minimum (which loses
        # candidates), expand the budget so the pipeline explores MORE rather
        # than less.  This is appropriate for drug repurposing where missing a
        # real candidate is worse than including a few extra weak ones.
        DEGRADE_EXPAND_FACTOR = 1.5
        stage1_max = _to_int(args.stage1_max, 30)
        if str(args.stage) == "stage2" and previous_topn is not None and previous_topn > 0:
            resolved = min(int(previous_topn * DEGRADE_EXPAND_FACTOR), _to_int(args.cap, previous_topn))
        else:
            # stage1: use stage1_max as generous fallback (not fallback_min)
            resolved = max(fallback_min, min(stage1_max, _to_int(args.cap, stage1_max)))
        payload = {
            "ok": False,
            "route": str(args.route),
            "profile": str(args.profile),
            "stage": str(args.stage),
            "configured_topn": str(args.configured_topn),
            "resolved_topn": int(max(1, resolved)),
            "mode": "degraded",
            "reason": "decision_exception_degraded_expand",
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": {},
            "should_expand": True,
            "quality_gate_enabled": str(args.stage) == "stage1",
            "degraded": True,
        }
    if args.output:
        _write_json(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_quality(args: argparse.Namespace) -> int:
    topk = _to_int(args.topk, 1)
    min_go = _to_int(args.min_go, 1)
    try:
        payload = evaluate_quality(
            step7_cards=Path(args.step7_cards),
            step8_shortlist=Path(args.step8_shortlist),
            topk=topk,
            min_go=min_go,
            route=str(args.route),
            stage=str(args.stage),
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "route": str(args.route),
            "stage": str(args.stage),
            "topk": int(topk),
            "min_go": int(min_go),
            "shortlist_rows": 0,
            "shortlist_go_count": 0,
            "step7_cards_total": 0,
            "step7_cards_go_count": 0,
            "step7_cards_maybe_count": 0,
            "pass_shortlist_rows": False,
            "pass_go_threshold": False,
            "quality_passed": False,
            "trigger_stage2": True,
            "reasons": [f"quality_exception:{type(exc).__name__}:{exc}"],
        }
    if args.output:
        _write_json(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Industrial topn policy for bridge->step6 scheduling")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decide", help="Resolve topn for stage1/stage2")
    d.add_argument("--bridge_csv", required=True)
    d.add_argument("--route", required=True, choices=["origin", "cross"])
    d.add_argument("--profile", default="stable", choices=["stable", "balanced", "recall"])
    d.add_argument("--stage", required=True, choices=["stage1", "stage2"])
    d.add_argument("--topk", type=int, required=True)
    d.add_argument("--configured_topn", default="auto")
    d.add_argument("--stage1_min", type=int, required=True)
    d.add_argument("--stage1_max", type=int, required=True)
    d.add_argument("--cap", type=int, required=True)
    d.add_argument("--expand_ratio", type=float, default=0.30)
    d.add_argument("--fallback_min", type=int, default=1)
    d.add_argument("--previous_topn", type=int, default=None)
    d.add_argument("--output", default="")
    d.set_defaults(func=_cmd_decide)

    q = sub.add_parser("quality", help="Evaluate stage output quality and stage2 trigger")
    q.add_argument("--step7_cards", required=True)
    q.add_argument("--step8_shortlist", required=True)
    q.add_argument("--topk", type=int, required=True)
    q.add_argument("--min_go", type=int, required=True)
    q.add_argument("--route", required=True, choices=["origin", "cross"])
    q.add_argument("--stage", required=True, choices=["stage1", "stage2"])
    q.add_argument("--output", default="")
    q.set_defaults(func=_cmd_quality)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
