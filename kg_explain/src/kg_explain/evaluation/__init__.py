"""
Benchmark 评估模块

用于评估药物重定位排序质量:
  - 标准 IR 指标: Hit@K, MRR, P@K, AP, NDCG@K, AUROC
  - 外部验证 (Hetionet)
  - 时间分割验证
  - 支持 gold-standard CSV 对照
"""
from .metrics import hit_at_k, reciprocal_rank, precision_at_k, average_precision, ndcg_at_k, auroc
from .benchmark import run_benchmark, run_external_benchmark, run_temporal_benchmark
from .leakage_audit import audit_pair_overlap, generate_leakage_report, save_leakage_report

__all__ = [
    "hit_at_k", "reciprocal_rank", "precision_at_k",
    "average_precision", "ndcg_at_k", "auroc",
    "run_benchmark", "run_external_benchmark", "run_temporal_benchmark",
    "audit_pair_overlap", "generate_leakage_report", "save_leakage_report",
]
