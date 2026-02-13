"""
ClinicalTrials.gov 数据源

用途: 获取指定疾病的失败/终止临床试验
当前配置: 动脉粥样硬化 (Atherosclerosis) 及相关心血管疾病

API文档: https://clinicaltrials.gov/data-api/api
"""
from __future__ import annotations
import logging
import re
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from ..cache import HTTPCache, cached_get_json
from ..config import ensure_dir

logger = logging.getLogger(__name__)

# ClinicalTrials.gov API端点
CTG_API = "https://clinicaltrials.gov/api/v2/studies"


def _extract_basic(study: dict) -> dict:
    """提取试验基本信息"""
    ps = (study or {}).get("protocolSection", {}) or {}
    idm = ps.get("identificationModule") or {}
    sm = ps.get("statusModule") or {}
    cm = ps.get("conditionsModule") or {}
    dm = ps.get("designModule") or {}
    return {
        "nctId": idm.get("nctId"),
        "briefTitle": idm.get("briefTitle") or idm.get("officialTitle"),
        "overallStatus": sm.get("overallStatus"),
        "whyStopped": sm.get("whyStopped"),
        "phases": "|".join(dm.get("phases") or []),
        "conditions": " | ".join(cm.get("conditions") or []),
    }


def _extract_interventions(study: dict) -> list[dict]:
    """提取干预措施(药物)"""
    ps = (study or {}).get("protocolSection", {}) or {}
    out = []
    im = ps.get("interventionsModule") or {}
    for it in im.get("interventions", []) or []:
        out.append({"name": it.get("name"), "type": it.get("type")})
    aim = ps.get("armsInterventionsModule") or {}
    for it in aim.get("interventions", []) or []:
        out.append({"name": it.get("name"), "type": it.get("type")})

    # 去重
    seen = set()
    dedup = []
    for x in out:
        if not x.get("name"):
            continue
        key = (x.get("name") or "", x.get("type") or "")
        if key not in seen:
            seen.add(key)
            dedup.append(x)
    return dedup


def _filter_interventions(
    interventions: list[dict],
    include_types: list[str] | None,
    exclude_types: list[str] | None,
) -> list[dict]:
    """
    按干预类型过滤

    Args:
        interventions: 干预列表 [{"name": ..., "type": ...}, ...]
        include_types: 仅保留这些类型 (如 ["DRUG", "BIOLOGICAL"])，None 表示不限
        exclude_types: 排除这些类型 (如 ["DEVICE", "PROCEDURE"])，None 表示不排除

    Returns:
        过滤后的干预列表
    """
    if not include_types and not exclude_types:
        return interventions

    inc = {t.upper() for t in include_types} if include_types else None
    exc = {t.upper() for t in exclude_types} if exclude_types else set()

    filtered = []
    for it in interventions:
        itype = (it.get("type") or "").upper()
        if not itype:
            # 类型未知的干预保留 (不误杀)
            filtered.append(it)
            continue
        if inc is not None and itype not in inc:
            continue
        if itype in exc:
            continue
        filtered.append(it)
    return filtered


_NEGATIVE_HINTS = [
    re.compile(r"did not meet.*primary endpoint", re.I),
    re.compile(r"failed to (meet|demonstrate|achieve)", re.I),
    re.compile(r"no significant (difference|benefit|reduction|improvement)", re.I),
    re.compile(r"\bfutility\b", re.I),
    re.compile(r"lack of efficacy", re.I),
    re.compile(r"did not (significantly )?(reduce|improve|lower)", re.I),
    re.compile(r"no (statistically )?significant .{0,30}(effect|change|difference)", re.I),
]

_POSITIVE_HINTS = [
    re.compile(r"met.*primary endpoint", re.I),
    re.compile(r"significant (improvement|reduction|benefit|decrease)", re.I),
    re.compile(r"\bsuperior\b", re.I),
    re.compile(r"significantly (reduced|improved|lowered)", re.I),
]


def _flatten_results_text(study: dict) -> str:
    """Recursively extract all text from the resultsSection of a CT.gov study."""
    rs = (study or {}).get("resultsSection") or {}
    parts = []

    def _walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(rs)
    return " ".join(parts)


def _label_outcome(study: dict) -> str:
    """
    Classify a COMPLETED trial's outcome as NEGATIVE, POSITIVE, MIXED, or UNCLEAR.

    Uses regex pattern matching on the resultsSection text.
    """
    text = _flatten_results_text(study)
    if not text or len(text) < 20:
        return "UNCLEAR"

    neg = any(p.search(text) for p in _NEGATIVE_HINTS)
    pos = any(p.search(text) for p in _POSITIVE_HINTS)

    if neg and pos:
        return "MIXED"
    if neg:
        return "NEGATIVE"
    if pos:
        return "POSITIVE"
    return "UNCLEAR"


def _fetch_studies(
    cache: HTTPCache,
    condition: str,
    statuses: list[str],
    page_size: int,
    max_pages: int,
    include_types: list[str] | None,
    exclude_types: list[str] | None,
    trial_source: str,
    label_completed: bool = False,
) -> tuple[list[dict], int]:
    """Fetch studies from CT.gov and extract drug rows.

    Returns:
        (rows, n_filtered): list of row dicts and count of filtered interventions
    """
    rows = []
    n_filtered = 0
    page_token = None
    desc_status = ",".join(statuses) if statuses else "ALL"

    for _ in tqdm(range(max_pages), desc=f"CT.gov [{condition} {desc_status}]"):
        params = {
            "query.cond": condition,
            "pageSize": min(int(page_size), 1000),
            "countTotal": "true",
        }
        if statuses:
            params["filter.overallStatus"] = ",".join(statuses)
        if page_token:
            params["pageToken"] = page_token

        js = cached_get_json(cache, CTG_API, params=params)

        for st in js.get("studies") or []:
            # For COMPLETED trials, only keep NEGATIVE/MIXED outcomes
            if label_completed:
                outcome = _label_outcome(st)
                if outcome not in ("NEGATIVE", "MIXED"):
                    continue

            b = _extract_basic(st)
            ints_raw = _extract_interventions(st)
            ints = _filter_interventions(ints_raw, include_types, exclude_types)
            n_filtered += len(ints_raw) - len(ints)

            if not ints:
                rows.append({**b, "drug_raw": None, "intervention_type": None,
                             "trial_source": trial_source})
            else:
                for it in ints:
                    rows.append({
                        **b,
                        "drug_raw": it.get("name"),
                        "intervention_type": it.get("type"),
                        "trial_source": trial_source,
                    })

        page_token = js.get("nextPageToken")
        if not page_token:
            break

    return rows, n_filtered


def fetch_failed_trials(
    condition: str,
    data_dir: Path,
    cache: HTTPCache,
    statuses: list[str] = None,
    page_size: int = 200,
    max_pages: int = 20,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    also_completed: bool = False,
) -> tuple[Path, Path]:
    """
    从CT.gov获取失败的临床试验

    Args:
        condition: 疾病条件 (如 "atherosclerosis")
        data_dir: 数据输出目录
        cache: HTTP缓存
        statuses: 试验状态 (默认: TERMINATED, WITHDRAWN, SUSPENDED)
        page_size: 每页大小
        max_pages: 最大页数
        include_types: 仅保留的干预类型
        exclude_types: 排除的干预类型
        also_completed: 是否同时获取 COMPLETED+negative 试验

    Returns:
        (rows_path, summary_path): 试验行数据和药物汇总
    """
    if statuses is None:
        statuses = ["TERMINATED", "WITHDRAWN", "SUSPENDED"]

    ensure_dir(data_dir)

    # Fetch stopped trials (existing behavior)
    rows, n_filtered = _fetch_studies(
        cache, condition, statuses, page_size, max_pages,
        include_types, exclude_types, trial_source="STOPPED",
    )

    n_completed = 0
    # Optionally fetch COMPLETED trials with negative outcomes
    if also_completed:
        completed_rows, n_filt2 = _fetch_studies(
            cache, condition, ["COMPLETED"], page_size, max_pages,
            include_types, exclude_types,
            trial_source="COMPLETED_NEGATIVE", label_completed=True,
        )
        n_filtered += n_filt2
        n_completed = len(set(r.get("nctId") for r in completed_rows if r.get("nctId")))
        rows.extend(completed_rows)
        logger.info("CT.gov COMPLETED+negative: %d 行, %d 个试验",
                     len(completed_rows), n_completed)

    df = pd.DataFrame(rows)
    if "trial_source" not in df.columns:
        df["trial_source"] = "STOPPED"
    n_trials = df["nctId"].nunique() if not df.empty else 0
    n_drugs = df["drug_raw"].dropna().nunique() if not df.empty else 0
    logger.info("CT.gov 获取完成: %d 行, %d 个试验 (STOPPED+COMPLETED_NEG), %d 个药物, %d 个干预被过滤",
                len(df), n_trials, n_drugs, n_filtered)

    # 保存行数据
    rows_path = data_dir / "failed_trials_drug_rows.csv"
    df.to_csv(rows_path, index=False)

    # 生成药物汇总
    d = df[df["drug_raw"].notna()].copy()
    d["drug_normalized"] = d["drug_raw"].fillna("").astype(str).str.strip().str.lower()
    summ = d.groupby("drug_normalized", as_index=False).agg(
        n_trials=("nctId", "nunique"),
        trial_statuses=("overallStatus", lambda x: ",".join(sorted(x.dropna().unique()))),
        trial_source=("trial_source", lambda x: ",".join(sorted(x.dropna().unique()))),
        example_name=("drug_raw", "first"),
        example_condition=("conditions", "first"),
        example_whyStopped=("whyStopped", lambda x: x.dropna().iloc[0] if len(x.dropna()) else ""),
    )
    summ_path = data_dir / "failed_drugs_summary.csv"
    summ.to_csv(summ_path, index=False)

    return rows_path, summ_path
