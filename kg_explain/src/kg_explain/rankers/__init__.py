"""
排序算法模块

当前版本:
  - ranker.py: 完整排名器 (DTPD路径 + FAERS安全 + 表型 + Bootstrap CI)
  - dtpd.py:   DTPD 基础路径评分 (ranker 内部调用)
"""
import logging

from .base import hub_penalty
from .dtpd import run_dtpd
from .ranker import run_ranker
from .uncertainty import bootstrap_ci, assign_confidence_tier, add_uncertainty_to_ranking

logger = logging.getLogger(__name__)

__all__ = [
    "hub_penalty", "run_dtpd", "run_ranker",
    "bootstrap_ci", "assign_confidence_tier", "add_uncertainty_to_ranking",
]


def run_pipeline(cfg) -> dict:
    """根据配置运行排名 (仅支持 v5/default)"""
    m = cfg.mode
    if m in ("v5", "5", "v5_test", "default"):
        return run_ranker(cfg)
    raise ValueError(
        f"未知模式: {m}。仅支持 v5 (default)。"
    )
