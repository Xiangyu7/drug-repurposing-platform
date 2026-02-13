"""
Reactome 数据源

用途: 获取靶点(UniProt) → 通路(Pathway)关系
API: https://reactome.org/ContentService
"""
from __future__ import annotations
import logging
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from ..cache import HTTPCache, cached_get_json
from ..utils import read_csv

logger = logging.getLogger(__name__)

# Reactome API端点
REACTOME_API = "https://reactome.org/ContentService"

# Hard failure guardrails (requested by user)
_FAIL_RATE_WARN = 0.05
_FAIL_RATE_ABORT = 0.15
_FAIL_STREAK_ABORT = 20
_SMALL_SAMPLE_N = 50
_SMALL_WARN_ABS = 3
_SMALL_ABORT_ABS = 8


def _reactome_pathways_for_uniprot(cache: HTTPCache, uniprot: str) -> list[dict]:
    """获取UniProt蛋白参与的通路"""
    url = f"{REACTOME_API}/data/mapping/UniProt/{uniprot}/pathways"
    js = cached_get_json(cache, url, params=None)
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

    通过UniProt ID从Reactome获取通路信息

    Returns:
        输出文件路径
    """
    xref = read_csv(data_dir / "target_xref.csv", dtype=str)
    up = xref[["target_chembl_id", "uniprot_accession"]].dropna().drop_duplicates()
    pair_list = [(r["target_chembl_id"], r["uniprot_accession"]) for _, r in up.iterrows()]

    rows: list[dict] = []
    total = len(pair_list)
    hard_failures = 0
    consecutive_hard_failures = 0
    hard_fail_examples: list[str] = []

    for tid, u in tqdm(pair_list, desc="Reactome Target→Pathway"):
        try:
            ps = _reactome_pathways_for_uniprot(cache, u)
        except Exception as e:
            if _is_hard_failure(e):
                hard_failures += 1
                consecutive_hard_failures += 1
                if len(hard_fail_examples) < 5:
                    hard_fail_examples.append(f"{u}: {e}")
                logger.warning("Reactome 硬失败, uniprot=%s: %s", u, e)

                if consecutive_hard_failures >= _FAIL_STREAK_ABORT:
                    raise RuntimeError(
                        "Reactome 连续硬失败触发熔断: "
                        f"{consecutive_hard_failures} 次 (阈值={_FAIL_STREAK_ABORT})"
                    ) from e
                continue

            # 非硬失败直接上抛, 避免掩盖程序错误
            raise

        consecutive_hard_failures = 0
        rows.extend([{
            "target_chembl_id": tid,
            "uniprot_accession": u,
            "reactome_stid": p.get("stId") or p.get("stIdVersion") or p.get("id"),
            "reactome_name": p.get("displayName") or p.get("name"),
        } for p in ps])

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
