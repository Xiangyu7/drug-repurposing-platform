#!/usr/bin/env python3
"""screen_drugs.py — 从 ClinicalTrials.gov 筛选失败/完成的临床试验药物

用法:
    # 按疾病筛选（最常用）
    python scripts/screen_drugs.py --disease atherosclerosis

    # 指定阶段 + 状态
    python scripts/screen_drugs.py --disease atherosclerosis --phases PHASE2 PHASE3 --statuses COMPLETED TERMINATED

    # 按药物名筛选（已知药物）
    python scripts/screen_drugs.py --drug colchicine

    # 按疾病 + 药物联合筛选
    python scripts/screen_drugs.py --disease atherosclerosis --drug darapladib

    # 限制拉取数量（测试用）
    python scripts/screen_drugs.py --disease atherosclerosis --max-studies 50

    # 手动追加你自己的未公开药物
    python scripts/screen_drugs.py --disease atherosclerosis --append-csv data/my_private_drugs.csv

    # 指定输出目录
    python scripts/screen_drugs.py --disease atherosclerosis --outdir output/screen

输出:
    {outdir}/
    ├── poolA_trials.csv               全部试验级别记录
    ├── poolA_drug_level.csv           药物级别记录（去除安慰剂/对照）
    ├── drug_master.csv                去重后的药物主表（drug_id + canonical_name）
    ├── manual_review_queue.csv        需要人工复核的试验
    └── screen_manifest.json           运行参数 + 统计摘要
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

# ───────────────────────── 常量 ─────────────────────────

CTGOV_API = "https://clinicaltrials.gov/api/v2"
PAGE_SIZE = 100
SLEEP_BETWEEN_PAGES = 1.3     # 列表翻页间隔
SLEEP_BETWEEN_DETAILS = 0.4   # 详情请求间隔
TIMEOUT = 30
MAX_RETRIES = 3

# 结果标签正则
NEGATIVE_HINTS = [
    r"did not meet (the )?primary endpoint",
    r"failed to meet (the )?primary endpoint",
    r"no significant (difference|benefit)",
    r"not statistically significant",
    r"lack of efficacy",
    r"futility",
    r"no efficacy",
    r"not superior to placebo",
    r"terminated.*futility",
    r"stopped early.*(futility|lack)",
]
POSITIVE_HINTS = [
    r"met (the )?primary endpoint",
    r"statistically significant (improvement|reduction|benefit)",
    r"superior(ity)? to placebo",
    r"significant(ly)? (reduced|improved|decreased)",
]

# 安慰剂/对照识别
PLACEBO_PATTERNS = [
    r"\bplacebo\b", r"\bmatching\s+placebo\b", r"\bvehicle\b",
    r"\bsham\b", r"\bdummy\b", r"\bsaline\b",
    r"\bstandard\s+of\s+care\b", r"\busual\s+care\b",
]

# ───────────────────────── 工具函数 ─────────────────────────

def safe_get(d: Any, *keys: str, default: Any = "") -> Any:
    """安全嵌套取值"""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def stable_drug_id(canonical_name: str) -> str:
    """生成稳定的药物ID: D + SHA1前10位"""
    h = hashlib.sha1(canonical_name.encode("utf-8")).hexdigest()
    return "D" + h[:10].upper()


def normalize_drug_name(name: str) -> str:
    """药物名归一化: 小写 + 去标点 + 压缩空格"""
    if not name:
        return ""
    x = name.lower().strip()
    x = re.sub(r"[\(\)\[\]\{\},;:/\\\"']", " ", x)
    # 去除剂型和剂量信息
    x = re.sub(r"\b\d+\s*(mg|mcg|ug|ml|g|%|units?|iu)\b", "", x, flags=re.I)
    x = re.sub(r"\b(tablet|capsule|injection|infusion|cream|ointment|solution|suspension|patch|spray)\b",
               "", x, flags=re.I)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def is_placebo(name: str, intervention_type: str = "") -> bool:
    """判断是否为安慰剂/对照"""
    if not name:
        return False
    if (intervention_type or "").upper() == "PLACEBO":
        return True
    x = name.lower().strip()
    for p in PLACEBO_PATTERNS:
        if re.search(p, x):
            return True
    if x in {"control", "no intervention", "none", "observation", "best supportive care"}:
        return True
    return False


def flatten_text(obj: Any) -> str:
    """把嵌套结构中的所有字符串拼成一段文本"""
    texts = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str):
            texts.append(cur)
    return "\n".join(texts)


def label_outcome(results_section: Optional[Dict]) -> Tuple[str, str]:
    """
    从 resultsSection 粗判结果标签
    Returns: (label, confidence)
        label: NEGATIVE / POSITIVE / MIXED / UNCLEAR / NO_RESULTS
    """
    if not results_section:
        return ("NO_RESULTS", "HIGH")
    text = flatten_text(results_section).lower()
    neg = any(re.search(p, text) for p in NEGATIVE_HINTS)
    pos = any(re.search(p, text) for p in POSITIVE_HINTS)
    if neg and not pos:
        return ("NEGATIVE", "MED")
    if pos and not neg:
        return ("POSITIVE", "MED")
    if neg and pos:
        return ("MIXED", "LOW")
    return ("UNCLEAR", "LOW")


def extract_pvalues(results_section: Optional[Dict]) -> str:
    """从 resultsSection 提取 p 值"""
    if not results_section:
        return ""
    text = flatten_text(results_section)
    pvals = re.findall(r"[pP]\s*[=<>≤≥]\s*0?\.\d+", text)
    if not pvals:
        return ""
    return "; ".join(sorted(set(pvals))[:5])

# ───────────────────────── HTTP ─────────────────────────

def http_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """带重试和限流处理的 GET 请求"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "10"))
                print(f"  [429 限流] 等待 {retry_after + 1}s ...")
                time.sleep(retry_after + 1)
                r = requests.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            wait = 2.0 * (attempt + 1)
            print(f"  [重试 {attempt+1}/{MAX_RETRIES}] {e} — 等待 {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"请求失败 ({url}): {last_err}")

# ───────────────────────── CT.gov 搜索 ─────────────────────────

def search_studies(
    disease: Optional[str] = None,
    drug: Optional[str] = None,
    phases: Optional[List[str]] = None,
    statuses: Optional[List[str]] = None,
    max_studies: int = 500,
) -> List[Dict[str, Any]]:
    """
    Step 1: 从 CT.gov API v2 搜索临床试验

    Args:
        disease:     疾病关键词 (condition query)
        drug:        药物关键词 (intervention query)
        phases:      试验阶段过滤 ["PHASE2", "PHASE3"]
        statuses:    试验状态过滤 ["COMPLETED", "TERMINATED"]
        max_studies: 最大拉取数量
    """
    out = []
    page_token = None
    pulled = 0

    if not phases:
        phases = ["PHASE2", "PHASE3"]
    if not statuses:
        statuses = ["COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"]

    print(f"\n{'='*60}")
    print(f"ClinicalTrials.gov 搜索")
    print(f"  疾病:   {disease or '(不限)'}")
    print(f"  药物:   {drug or '(不限)'}")
    print(f"  阶段:   {', '.join(phases)}")
    print(f"  状态:   {', '.join(statuses)}")
    print(f"  上限:   {max_studies}")
    print(f"{'='*60}\n")

    while pulled < max_studies:
        params = {
            "pageSize": PAGE_SIZE,
            "format": "json",
            "countTotal": "true",
            "sort": "LastUpdatePostDate:desc",
            "filter.overallStatus": ",".join(statuses),
            "filter.phase": ",".join(phases),
        }
        if disease:
            params["query.cond"] = disease
        if drug:
            params["query.intr"] = drug
        if page_token:
            params["pageToken"] = page_token

        data = http_get(f"{CTGOV_API}/studies", params)
        total = data.get("totalCount", "?")
        studies = data.get("studies", [])

        if pulled == 0:
            print(f"  CT.gov 返回总计 {total} 条匹配试验")

        out.extend(studies)
        pulled += len(studies)
        print(f"  已拉取: {pulled}/{max_studies} (本页 {len(studies)} 条)")

        page_token = data.get("nextPageToken")
        if not page_token or not studies:
            break
        time.sleep(SLEEP_BETWEEN_PAGES)

    result = out[:max_studies]
    print(f"\n  最终保留 {len(result)} 条试验")
    return result


def fetch_study_detail(nct_id: str) -> Dict[str, Any]:
    """拉取单个试验详情"""
    data = http_get(f"{CTGOV_API}/studies/{nct_id}", {"format": "json"})
    time.sleep(SLEEP_BETWEEN_DETAILS)
    return data

# ───────────────────────── 药物提取 ─────────────────────────

def extract_drugs_from_study(study: Dict[str, Any]) -> Tuple[Dict, List[Dict], Optional[Dict]]:
    """
    从一条试验中提取:
    1. trial_row:   试验级别记录
    2. drug_rows:   药物级别记录 (一个试验可能有多个药物)
    3. review_row:  如果需要人工复核, 返回一条; 否则 None
    """
    ps = study.get("protocolSection", {}) or {}
    rs = study.get("resultsSection") or {}

    nct = safe_get(ps, "identificationModule", "nctId")
    title = safe_get(ps, "identificationModule", "briefTitle") or safe_get(ps, "identificationModule", "officialTitle")
    overall = safe_get(ps, "statusModule", "overallStatus")
    study_type = safe_get(ps, "designModule", "studyType")
    phases_raw = safe_get(ps, "designModule", "phases", default=[])
    phase_str = ",".join(phases_raw) if isinstance(phases_raw, list) else str(phases_raw or "")
    sponsor = safe_get(ps, "sponsorCollaboratorsModule", "leadSponsor", "name")

    conditions_raw = safe_get(ps, "conditionsModule", "conditions", default=[])
    conditions = " | ".join(conditions_raw[:10]) if isinstance(conditions_raw, list) else str(conditions_raw)

    enrollment = safe_get(ps, "designModule", "enrollmentInfo", "count", default="")

    # 结果标签
    out_label, out_conf = label_outcome(rs if rs else None)
    pvalues = extract_pvalues(rs if rs else None)
    has_results = 1 if bool(rs) else 0

    # 主要终点
    primary_outcomes = safe_get(ps, "outcomesModule", "primaryOutcomes", default=[])
    po_titles = []
    po_timeframes = []
    for po in (primary_outcomes if isinstance(primary_outcomes, list) else []):
        if isinstance(po, dict):
            po_titles.append(po.get("measure", ""))
            po_timeframes.append(po.get("timeFrame", ""))

    ctgov_url = f"https://clinicaltrials.gov/study/{nct}"

    trial_row = {
        "nctId": nct,
        "briefTitle": title,
        "overallStatus": overall,
        "phase": phase_str,
        "studyType": study_type,
        "leadSponsor": sponsor,
        "conditions": conditions,
        "enrollmentCount": enrollment,
        "ctgov_url": ctgov_url,
        "has_resultsSection": has_results,
        "outcome_label_mvp": out_label,
        "outcome_confidence_mvp": out_conf,
        "primary_outcome_title": " | ".join(po_titles[:3]),
        "primary_outcome_timeframe": " | ".join(po_timeframes[:3]),
        "primary_outcome_pvalues": pvalues,
    }

    # 提取药物
    interventions = safe_get(ps, "armsInterventionsModule", "interventions", default=[])
    if not isinstance(interventions, list):
        interventions = []

    # arm 信息
    arm_groups = safe_get(ps, "armsInterventionsModule", "armGroups", default=[])
    if not isinstance(arm_groups, list):
        arm_groups = []

    arm_map = {}
    for ag in arm_groups:
        label = ag.get("label", "")
        atype = ag.get("type", "")
        for iname in (ag.get("interventionNames") or []):
            norm = normalize_drug_name(iname)
            if norm and not is_placebo(iname, ""):
                arm_map[norm] = (label, atype)

    drug_rows = []
    seen_drugs = set()
    is_combo = len([iv for iv in interventions
                    if (iv.get("type", "").upper() in {"DRUG", "BIOLOGICAL"})
                    and not is_placebo(iv.get("name", ""), iv.get("type", ""))]) > 1

    for iv in interventions:
        raw_name = (iv.get("name") or "").strip()
        iv_type = (iv.get("type") or "").strip()
        if not raw_name:
            continue
        if iv_type.upper() not in {"DRUG", "BIOLOGICAL", "GENETIC"}:
            continue
        if is_placebo(raw_name, iv_type):
            continue

        norm_name = normalize_drug_name(raw_name)
        if norm_name in seen_drugs:
            continue
        seen_drugs.add(norm_name)

        arm_label, arm_type = arm_map.get(norm_name, ("", ""))

        drug_rows.append({
            "nctId": nct,
            "briefTitle": title,
            "overallStatus": overall,
            "phase": phase_str,
            "studyType": study_type,
            "leadSponsor": sponsor,
            "conditions": conditions,
            "drug_raw": raw_name,
            "drug_normalized": norm_name,
            "intervention_type": iv_type,
            "arm_label": arm_label,
            "arm_type": arm_type,
            "role": "CANDIDATE",
            "is_candidate_drug": 1,
            "is_combo": 1 if is_combo else 0,
            "outcome_label_mvp": out_label,
            "outcome_confidence_mvp": out_conf,
            "has_resultsSection": has_results,
            "primary_endpoint_met_final": "N" if out_label == "NEGATIVE" else ("Y" if out_label == "POSITIVE" else ""),
            "outcome_label_final": out_label,
            "confidence_final": out_conf,
            "evidence_source": "CTGOV_STRUCTURED" if has_results else "CTGOV_PROTOCOL",
            "primary_outcome_title": " | ".join(po_titles[:3]),
            "primary_outcome_timeframe": " | ".join(po_timeframes[:3]),
            "primary_outcome_pvalues": pvalues,
            "notes_ctgov": "",
            "pubmed_pmids": "",
            "notes_pubmed": "",
        })

    # 人工复核: NEGATIVE/UNCLEAR/MIXED + 有结果
    review_row = None
    if out_label in {"NEGATIVE", "UNCLEAR", "MIXED"} and has_results:
        review_row = {
            "nctId": nct,
            "briefTitle": title,
            "phase": phase_str,
            "overallStatus": overall,
            "conditions": conditions,
            "outcome_label_mvp": out_label,
            "outcome_confidence_mvp": out_conf,
            "primary_outcome_pvalues": pvalues,
            "ctgov_url": ctgov_url,
            "reason": "自动判定为负/不确定结果，建议人工确认主要终点结论",
        }

    return trial_row, drug_rows, review_row


def append_private_drugs(csv_path: str) -> List[Dict]:
    """
    读取用户自定义的私有药物 CSV，合并到 drug_rows。

    CSV 最小格式:
        drug_name,phase,conditions
        MyDrug-001,PHASE2,Atherosclerosis
        MyDrug-002,PHASE3,Coronary Artery Disease

    也可以提供更多列（会直接合并）。
    """
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    required = {"drug_name"}
    if not required.issubset(set(df.columns)):
        print(f"  [错误] --append-csv 文件必须包含列: {required}")
        print(f"  [错误] 当前列: {list(df.columns)}")
        sys.exit(1)

    rows = []
    for _, r in df.iterrows():
        raw = r["drug_name"].strip()
        norm = normalize_drug_name(raw)
        nct = r.get("nctId", r.get("nct_id", "")).strip() or f"PRIV{hashlib.md5(raw.encode()).hexdigest()[:6].upper()}"
        rows.append({
            "nctId": nct,
            "briefTitle": r.get("briefTitle", r.get("title", f"Private trial of {raw}")),
            "overallStatus": r.get("overallStatus", r.get("status", "UNKNOWN")),
            "phase": r.get("phase", ""),
            "studyType": r.get("studyType", "INTERVENTIONAL"),
            "leadSponsor": r.get("leadSponsor", r.get("sponsor", "")),
            "conditions": r.get("conditions", r.get("disease", "")),
            "drug_raw": raw,
            "drug_normalized": norm,
            "intervention_type": r.get("intervention_type", "DRUG"),
            "arm_label": "",
            "arm_type": "",
            "role": "CANDIDATE",
            "is_candidate_drug": 1,
            "is_combo": 0,
            "outcome_label_mvp": r.get("outcome", "UNCLEAR"),
            "outcome_confidence_mvp": r.get("confidence", "LOW"),
            "has_resultsSection": 0,
            "primary_endpoint_met_final": r.get("primary_endpoint_met", ""),
            "outcome_label_final": r.get("outcome", "UNCLEAR"),
            "confidence_final": r.get("confidence", "LOW"),
            "evidence_source": "MANUAL_ENTRY",
            "primary_outcome_title": r.get("primary_outcome", ""),
            "primary_outcome_timeframe": r.get("timeframe", ""),
            "primary_outcome_pvalues": r.get("pvalue", ""),
            "notes_ctgov": r.get("notes", ""),
            "pubmed_pmids": r.get("pmids", ""),
            "notes_pubmed": "",
        })
    return rows

# ───────────────────────── 主函数 ─────────────────────────

def build_drug_master(drug_df: pd.DataFrame) -> pd.DataFrame:
    """从 drug_level 构建去重后的 drug_master"""
    if drug_df.empty:
        return pd.DataFrame(columns=["drug_id", "canonical_name"])

    unique = drug_df["drug_normalized"].dropna().unique()
    rows = []
    for name in sorted(unique):
        if name.strip():
            rows.append({
                "drug_id": stable_drug_id(name),
                "canonical_name": name,
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(
        description="从 ClinicalTrials.gov 筛选临床试验药物",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 筛选动脉粥样硬化 Phase 2/3 失败试验
  python scripts/screen_drugs.py --disease atherosclerosis

  # 筛选特定药物的所有试验
  python scripts/screen_drugs.py --drug colchicine

  # 限制为 TERMINATED 状态 + Phase 3
  python scripts/screen_drugs.py --disease "heart failure" --phases PHASE3 --statuses TERMINATED

  # 追加你自己的未公开药物
  python scripts/screen_drugs.py --disease atherosclerosis --append-csv data/my_private_drugs.csv
        """,
    )
    parser.add_argument("--disease", type=str, default=None, help="疾病关键词 (CT.gov condition query)")
    parser.add_argument("--drug", type=str, default=None, help="药物关键词 (CT.gov intervention query)")
    parser.add_argument("--phases", nargs="+", default=None,
                        help="试验阶段 (默认: PHASE2 PHASE3). 可选: PHASE1 PHASE2 PHASE3 PHASE4")
    parser.add_argument("--statuses", nargs="+", default=None,
                        help="试验状态 (默认: COMPLETED TERMINATED WITHDRAWN SUSPENDED)")
    parser.add_argument("--max-studies", type=int, default=500, help="最大拉取试验数 (默认 500)")
    parser.add_argument("--append-csv", type=str, default=None,
                        help="追加自定义药物 CSV (最少需要 drug_name 列)")
    parser.add_argument("--outdir", type=str, default="data", help="输出目录 (默认 data/)")
    parser.add_argument("--skip-details", action="store_true",
                        help="跳过详情拉取（仅用列表结果，更快但信息不完整）")

    args = parser.parse_args()

    if not args.disease and not args.drug:
        print("[错误] 必须至少指定 --disease 或 --drug 之一")
        parser.print_help()
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()

    # ─── Step 1: 搜索 ───
    studies = search_studies(
        disease=args.disease,
        drug=args.drug,
        phases=args.phases,
        statuses=args.statuses,
        max_studies=args.max_studies,
    )

    if not studies:
        print("\n[警告] 未找到匹配的试验。请检查搜索条件。")
        sys.exit(0)

    # ─── Step 2: 拉详情 + 提取药物 ───
    trial_rows = []
    drug_rows = []
    review_rows = []
    errors = []

    print(f"\n正在拉取 {len(studies)} 条试验的详情...\n")

    for i, s in enumerate(studies):
        # 从列表结果中取 NCT ID
        nct = (safe_get(s, "protocolSection", "identificationModule", "nctId")
               or s.get("nctId") or s.get("NCTId"))
        if not nct:
            continue

        try:
            if args.skip_details:
                study_detail = s
            else:
                study_detail = fetch_study_detail(nct)

            trial_row, drugs, review_row = extract_drugs_from_study(study_detail)
            trial_rows.append(trial_row)
            drug_rows.extend(drugs)
            if review_row:
                review_rows.append(review_row)

            status = trial_row.get("outcome_label_mvp", "?")
            n_drugs = len(drugs)
            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [{i+1}/{len(studies)}] {nct} → {status}, {n_drugs} 个药物")

        except Exception as e:
            errors.append({"nctId": nct, "error": str(e)})
            print(f"  [{i+1}/{len(studies)}] {nct} → [错误] {e}")

    # ─── Step 3: 追加自定义药物 ───
    n_private = 0
    if args.append_csv:
        print(f"\n正在追加自定义药物: {args.append_csv}")
        private_rows = append_private_drugs(args.append_csv)
        drug_rows.extend(private_rows)
        n_private = len(private_rows)
        print(f"  追加 {n_private} 条私有药物记录")

    # ─── Step 4: 构建输出 ───
    trial_df = pd.DataFrame(trial_rows)
    drug_df = pd.DataFrame(drug_rows)
    master_df = build_drug_master(drug_df)
    review_df = pd.DataFrame(review_rows)

    # 保存
    trial_df.to_csv(outdir / "poolA_trials.csv", index=False, encoding="utf-8-sig")
    drug_df.to_csv(outdir / "poolA_drug_level.csv", index=False, encoding="utf-8-sig")
    master_df.to_csv(outdir / "drug_master.csv", index=False, encoding="utf-8-sig")
    review_df.to_csv(outdir / "manual_review_queue.csv", index=False, encoding="utf-8-sig")

    # 也生成 step6 直接可用的输入
    if not master_df.empty:
        master_df[["drug_id", "canonical_name"]].to_csv(
            outdir / "step6_rank.csv", index=False, encoding="utf-8-sig"
        )

    elapsed = time.time() - start_time

    # ─── Manifest ───
    manifest = {
        "script": "screen_drugs.py",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "args": {
            "disease": args.disease,
            "drug": args.drug,
            "phases": args.phases or ["PHASE2", "PHASE3"],
            "statuses": args.statuses or ["COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"],
            "max_studies": args.max_studies,
            "append_csv": args.append_csv,
            "skip_details": args.skip_details,
        },
        "stats": {
            "studies_searched": len(studies),
            "studies_processed": len(trial_rows),
            "studies_failed": len(errors),
            "drug_rows": len(drug_rows),
            "unique_drugs": len(master_df),
            "private_drugs_appended": n_private,
            "manual_review_count": len(review_rows),
            "elapsed_seconds": round(elapsed, 1),
        },
    }
    with open(outdir / "screen_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ─── 摘要 ───
    print(f"\n{'='*60}")
    print(f"筛选完成!")
    print(f"{'='*60}")
    print(f"  试验总数:       {len(trial_rows)}")
    print(f"  药物记录:       {len(drug_rows)} (含 {n_private} 条私有)")
    print(f"  去重药物数:     {len(master_df)}")
    print(f"  需人工复核:     {len(review_rows)}")
    print(f"  失败请求:       {len(errors)}")
    print(f"  耗时:           {elapsed:.1f}s")
    print(f"\n输出目录: {outdir.resolve()}")
    print(f"  poolA_trials.csv         — 试验级别 ({len(trial_rows)} 行)")
    print(f"  poolA_drug_level.csv     — 药物级别 ({len(drug_rows)} 行)")
    print(f"  drug_master.csv          — 药物主表 ({len(master_df)} 行)")
    print(f"  step6_rank.csv           — Step6 直接可用输入")
    print(f"  manual_review_queue.csv  — 人工复核队列 ({len(review_rows)} 行)")
    print(f"  screen_manifest.json     — 运行参数+统计")

    if review_rows:
        print(f"\n{'='*60}")
        print(f"⚠ 以下试验需要人工复核 (查看 manual_review_queue.csv):")
        print(f"{'='*60}")
        for r in review_rows[:10]:
            print(f"  {r['nctId']}  {r['outcome_label_mvp']:10s}  {r['briefTitle'][:60]}")
        if len(review_rows) > 10:
            print(f"  ... 还有 {len(review_rows) - 10} 条")

    if not master_df.empty:
        print(f"\n{'='*60}")
        print(f"药物列表预览 (前 20):")
        print(f"{'='*60}")
        for _, r in master_df.head(20).iterrows():
            print(f"  {r['drug_id']}  {r['canonical_name']}")

    print(f"\n下一步:")
    print(f"  1. 检查 manual_review_queue.csv，人工确认结果标签")
    print(f"  2. 运行 Step6: python scripts/step6_evidence_extraction.py --rank_in {outdir}/step6_rank.csv")
    print()


if __name__ == "__main__":
    main()
