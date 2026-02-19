"""
Reactome 数据源

用途: 获取靶点(UniProt) → 通路(Pathway)关系
API: https://reactome.org/ContentService
"""
from __future__ import annotations
import logging
import threading
from pathlib import Path

import pandas as pd
import requests

from ..cache import HTTPCache, cached_get_json
from ..utils import concurrent_map, read_csv

logger = logging.getLogger(__name__)

# Reactome API端点
REACTOME_API = "https://reactome.org/ContentService"

# Hard failure guardrails (requested by user)
_FAIL_RATE_WARN = 0.05
_FAIL_RATE_ABORT = 0.15
_SMALL_SAMPLE_N = 50
_SMALL_WARN_ABS = 3
_SMALL_ABORT_ABS = 8

# 线程安全的失败计数器
_fail_lock = threading.Lock()


def _reactome_pathways_for_uniprot(cache: HTTPCache, uniprot: str) -> list[dict]:
    """获取UniProt蛋白参与的通路.

    HTTP 404 表示该蛋白在 Reactome 中无通路数据 (例如非人类蛋白),
    属于预期情况, 直接返回空列表而非抛出异常.
    """
    url = f"{REACTOME_API}/data/mapping/UniProt/{uniprot}/pathways"
    try:
        js = cached_get_json(cache, url, params=None)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    if isinstance(js, list):
        return js
    return js.get("pathways") or js.get("results") or []


def _is_hard_failure(exc: Exception) -> bool:
    """
    判定是否属于硬失败:
      - HTTP error / connection error / timeout
      - JSON 解析失败
    """
    return isinstance(exc, (requests.RequestException, TimeoutError, ValueError))


def fetch_target_pathways(
    data_dir: Path,
    cache: HTTPCache,
) -> Path:
    """
    获取靶点-通路关系 (edge_target_pathway)

    通过UniProt ID从Reactome获取通路信息.
    使用 concurrent_map() 并行获取, 与其他 datasource 模式一致.

    Returns:
        输出文件路径
    """
    xref = read_csv(data_dir / "target_xref.csv", dtype=str)
    up = xref[["target_chembl_id", "uniprot_accession"]].dropna().drop_duplicates()
    pair_list = [(r["target_chembl_id"], r["uniprot_accession"]) for _, r in up.iterrows()]

    total = len(pair_list)

    # 线程安全的失败统计 (在并发环境下安全累加)
    fail_state = {"count": 0, "examples": []}

    def _fetch_one(pair):
        tid, u = pair
        try:
            ps = _reactome_pathways_for_uniprot(cache, u)
        except Exception as e:
            if _is_hard_failure(e):
                with _fail_lock:
                    fail_state["count"] += 1
                    if len(fail_state["examples"]) < 5:
                        fail_state["examples"].append(f"{u}: {e}")
                logger.warning("Reactome 硬失败, uniprot=%s: %s", u, e)
                return None
            # 非硬失败直接上抛, 避免掩盖程序错误
            raise

        return [{
            "target_chembl_id": tid,
            "uniprot_accession": u,
            "reactome_stid": p.get("stId") or p.get("stIdVersion") or p.get("id"),
            "reactome_name": p.get("displayName") or p.get("name"),
        } for p in ps]

    results = concurrent_map(
        _fetch_one, pair_list,
        max_workers=cache.max_workers, desc="Reactome Target→Pathway",
    )

    # 展平结果 (跳过 None = 失败的任务)
    rows = [row for result in results if result is not None for row in result]

    # ── 失败率门控 (与并行前逻辑一致) ──
    hard_failures = fail_state["count"]
    hard_fail_examples = fail_state["examples"]
    fail_rate = (hard_failures / total) if total > 0 else 0.0

    if total < _SMALL_SAMPLE_N:
        warn_tripped = hard_failures >= _SMALL_WARN_ABS
        fail_tripped = hard_failures >= _SMALL_ABORT_ABS
        threshold_desc = (
            f"small-sample mode (N={total}<{_SMALL_SAMPLE_N}), "
            f"warn>={_SMALL_WARN_ABS}, fail>={_SMALL_ABORT_ABS}"
        )
    else:
        warn_tripped = fail_rate > _FAIL_RATE_WARN
        fail_tripped = fail_rate > _FAIL_RATE_ABORT
        threshold_desc = (
            f"rate mode, warn>{_FAIL_RATE_WARN:.0%}, fail>{_FAIL_RATE_ABORT:.0%}"
        )

    if warn_tripped:
        logger.warning(
            "Reactome 硬失败告警: %d/%d (%.2f%%), %s",
            hard_failures, total, fail_rate * 100, threshold_desc,
        )
    else:
        logger.info(
            "Reactome 硬失败统计: %d/%d (%.2f%%), %s",
            hard_failures, total, fail_rate * 100, threshold_desc,
        )

    if fail_tripped:
        sample = "; ".join(hard_fail_examples) if hard_fail_examples else "无"
        raise RuntimeError(
            "Reactome 硬失败率过高, 已中止: "
            f"{hard_failures}/{total} ({fail_rate:.2%}), {threshold_desc}. "
            f"样例: {sample}"
        )

    out_df = pd.DataFrame(rows, columns=[
        "target_chembl_id", "uniprot_accession", "reactome_stid", "reactome_name"
    ]).dropna(subset=["target_chembl_id", "reactome_stid"]).drop_duplicates()
    logger.info("Target→Pathway 关系: %d 条边, %d 个靶点, %d 个通路",
                len(out_df), out_df["target_chembl_id"].nunique(), out_df["reactome_stid"].nunique())

    out = data_dir / "edge_target_pathway_all.csv"
    out_df.to_csv(out, index=False)
    return out
