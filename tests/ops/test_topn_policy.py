"""Unit tests for ops/topn_policy.py decision logic."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
TOPN_POLICY_PATH = ROOT / "ops" / "topn_policy.py"
spec = spec_from_file_location("topn_policy", TOPN_POLICY_PATH)
topn_policy = module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = topn_policy
spec.loader.exec_module(topn_policy)


def test_stage1_auto_uses_n50_and_clamp():
    df = pd.DataFrame(
        {
            "canonical_name": [f"d{i}" for i in range(20)],
            "max_mechanism_score": [10.0, 8.0, 6.0, 5.0] + [0.5] * 16,
        }
    )
    cfg = topn_policy.DecisionConfig(
        route="origin",
        profile="stable",
        stage="stage1",
        topk=10,
        configured_topn="auto",
        stage1_min=12,
        stage1_max=14,
        cap=18,
        expand_ratio=0.30,
        fallback_min=12,
        previous_topn=None,
    )
    out = topn_policy.decide_topn(df, cfg)
    assert out["mode"] == "auto_stage1"
    assert out["resolved_topn"] == 12
    assert out["metrics"]["n50"] >= 1


def test_stage2_auto_applies_ratio_and_cap():
    df = pd.DataFrame(
        {
            "canonical_name": [f"d{i}" for i in range(30)],
            "max_mechanism_score": [10.0] * 5 + [3.5] * 10 + [2.0] * 15,
        }
    )
    cfg = topn_policy.DecisionConfig(
        route="cross",
        profile="stable",
        stage="stage2",
        topk=5,
        configured_topn="auto",
        stage1_min=10,
        stage1_max=12,
        cap=14,
        expand_ratio=0.30,
        fallback_min=10,
        previous_topn=12,
    )
    out = topn_policy.decide_topn(df, cfg)
    assert out["mode"] == "auto_stage2"
    assert out["resolved_topn"] == 14
    assert out["should_expand"] is True


def test_missing_score_columns_falls_back_to_route_min():
    df = pd.DataFrame({"canonical_name": [f"d{i}" for i in range(6)], "targets": ["x"] * 6})
    cfg = topn_policy.DecisionConfig(
        route="origin",
        profile="stable",
        stage="stage1",
        topk=3,
        configured_topn="auto",
        stage1_min=12,
        stage1_max=14,
        cap=18,
        expand_ratio=0.30,
        fallback_min=12,
        previous_topn=None,
    )
    out = topn_policy.decide_topn(df, cfg)
    # bridge rows < lower bound -> use all rows
    assert out["resolved_topn"] == 6
    assert out["score_column"] == ""


def test_manual_numeric_compatibility():
    df = pd.DataFrame({"canonical_name": [f"d{i}" for i in range(9)], "max_mechanism_score": list(range(9, 0, -1))})
    cfg_all = topn_policy.DecisionConfig(
        route="cross",
        profile="stable",
        stage="stage1",
        topk=5,
        configured_topn="0",
        stage1_min=10,
        stage1_max=12,
        cap=14,
        expand_ratio=0.30,
        fallback_min=10,
        previous_topn=None,
    )
    out_all = topn_policy.decide_topn(df, cfg_all)
    assert out_all["resolved_topn"] == 9
    assert out_all["quality_gate_enabled"] is False

    cfg_clamp = topn_policy.DecisionConfig(
        route="cross",
        profile="stable",
        stage="stage1",
        topk=5,
        configured_topn="50",
        stage1_min=10,
        stage1_max=12,
        cap=14,
        expand_ratio=0.30,
        fallback_min=10,
        previous_topn=None,
    )
    out_clamp = topn_policy.decide_topn(df, cfg_clamp)
    assert out_clamp["resolved_topn"] == 9
