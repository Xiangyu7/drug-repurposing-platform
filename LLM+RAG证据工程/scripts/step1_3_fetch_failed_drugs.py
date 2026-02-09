import time
import re
import json
from typing import Dict, Any, List, Optional, Tuple
import requests
import pandas as pd

BASE = "https://clinicaltrials.gov/api/v2"

# ====== 你只需要改这里（人工干预点 #1：检索范围）======
CONDITION_QUERY = "atherosclerosis"      # 例如: "atherosclerosis" / "coronary artery disease" / "cardiovascular"
INTERVENTION_QUERY = None                # 例如: "colchicine"；不用就填 None
MAX_STUDIES_TO_PULL = 800                # 先别太大，避免429；后面可逐步加
# ========================================================

PAGE_SIZE = 100
SLEEP_SECONDS = 1.3   # 保守点，降低触发 429 风险（官方有频率限制；建议缓存）:contentReference[oaicite:3]{index=3}
TIMEOUT = 30

# 初筛参数：主池A=COMPLETED（Phase2/3我们后面从详情里再判定更稳）
LIST_FIELDS = "NCTId,BriefTitle,OverallStatus,HasResults,Phase,StudyType,LastUpdatePostDate"
SORT = "LastUpdatePostDate:desc"

NEGATIVE_HINTS = [
    r"did not meet (the )?primary endpoint",
    r"failed to meet (the )?primary endpoint",
    r"no significant (difference|benefit)",
    r"not statistically significant",
    r"lack of efficacy",
    r"futility",
    r"no efficacy",
    r"not superior to placebo",
]
POSITIVE_HINTS = [
    r"met (the )?primary endpoint",
    r"statistically significant improvement",
    r"superior to placebo",
]

def http_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 429:
        # 触发限流：退避
        retry = int(r.headers.get("Retry-After", "10"))
        time.sleep(retry + 1)
        r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def list_studies() -> List[Dict[str, Any]]:
    """Step 1: 拉主池A候选（COMPLETED）"""
    out = []
    page_token = None
    pulled = 0

    while pulled < MAX_STUDIES_TO_PULL:
        params = {
            "pageSize": PAGE_SIZE,
            "format": "json",
            "countTotal": "true",
            "fields": LIST_FIELDS,
            "sort": SORT,
            "filter.overallStatus": "COMPLETED",
        }
        if CONDITION_QUERY:
            params["query.cond"] = CONDITION_QUERY
        if INTERVENTION_QUERY:
            params["query.intr"] = INTERVENTION_QUERY
        if page_token:
            params["pageToken"] = page_token

        data = http_get(f"{BASE}/studies", params)
        studies = data.get("studies", [])
        out.extend(studies)
        pulled += len(studies)

        page_token = data.get("nextPageToken")
        if not page_token or not studies:
            break

        time.sleep(SLEEP_SECONDS)

    return out[:MAX_STUDIES_TO_PULL]

def get_study(nct_id: str) -> Dict[str, Any]:
    """Step 2: 拉详情"""
    data = http_get(f"{BASE}/studies/{nct_id}", params={"format": "json"})
    time.sleep(SLEEP_SECONDS)
    return data

def safe_get(d: Dict[str, Any], path: List[str], default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def normalize_drug_name(name: str) -> str:
    if not name:
        return ""
    x = name.lower().strip()
    x = re.sub(r"\s+", " ", x)
    x = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x

PLACEBO_LIKE_PATTERNS = [
    r"\bplacebo\b",
    r"\bmatching\s+placebo\b",
    r"\bvehicle\b",
    r"\bsham\b",
    r"\bdummy\b",
    r"\bsaline\b",                 # 有些写作“normal saline”当对照
    r"\bstandard\s+of\s+care\b",   # SOC 不是药物本体
    r"\busual\s+care\b",
]

def is_placebo_like(name: str, intervention_type: str = "") -> bool:
    """
    判断某个 intervention 是否属于 placebo/vehicle/sham/SOC 等“非药物候选”
    规则刻意保守：主要靠 placebo/vehicle/sham 等强信号词
    """
    if not name:
        return False
    x = name.lower().strip()

    # 有些 trial 直接给 type=PLACEBO（如果存在）
    if (intervention_type or "").upper() == "PLACEBO":
        return True

    for p in PLACEBO_LIKE_PATTERNS:
        if re.search(p, x):
            return True

    # 一些常见的“对照描述”但不一定包含 placebo 字样（保守处理）
    # 只有在 name 基本等于 control 才认为是非药物，避免误伤“Active control: DrugX”
    if x in {"control", "no intervention", "none"}:
        return True

    return False

def flatten_strings(obj: Any) -> str:
    """把 resultsSection 里所有字符串拼成一个大文本，做粗判（MVP）"""
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

def label_outcome_from_results(results_section: Dict[str, Any]) -> Tuple[str, str]:
    """
    Step 3: 粗判结果标签
    return: (label, confidence)  label in {NEGATIVE, POSITIVE, UNCLEAR}
    """
    if not results_section:
        return ("NO_RESULTS", "HIGH")

    text = flatten_strings(results_section).lower()
    neg = any(re.search(p, text) for p in NEGATIVE_HINTS)
    pos = any(re.search(p, text) for p in POSITIVE_HINTS)

    if neg and not pos:
        return ("NEGATIVE", "MED")
    if pos and not neg:
        return ("POSITIVE", "MED")
    if neg and pos:
        return ("MIXED", "LOW")
    return ("UNCLEAR", "LOW")

def extract_drug_level_rows(study: Dict[str, Any]) -> List[Dict[str, Any]]:
    ps = study.get("protocolSection", {}) or {}
    rs = study.get("resultsSection", {}) or {}

    # ✅ outcome label 只算一次，而且保证一定有定义
    out_label, out_conf = label_outcome_from_results(rs)

    nct = safe_get(ps, ["identificationModule", "nctId"], "")
    title = safe_get(ps, ["identificationModule", "briefTitle"], "") or safe_get(ps, ["identificationModule", "officialTitle"], "")
    overall = safe_get(ps, ["statusModule", "overallStatus"], "")
    phase = safe_get(ps, ["designModule", "phases"], None)
    study_type = safe_get(ps, ["designModule", "studyType"], "")
    sponsor = safe_get(ps, ["sponsorCollaboratorsModule", "leadSponsor", "name"], "")

    conditions = safe_get(ps, ["conditionsModule", "conditions"], []) or []
    cond_str = " | ".join(conditions[:10])

    interventions = safe_get(ps, ["armsInterventionsModule", "interventions"], []) or []
    drugs = []
    for it in interventions:
        it_type = (it.get("type") or "").upper()
        nm = (it.get("name") or "").strip()
        if not nm:
            continue

        # ✅ 过滤 placebo/vehicle/sham/SOC 等
        if is_placebo_like(nm, it_type):
            continue

        if it_type in {"DRUG", "BIOLOGICAL"}:
            drugs.append({
                "drug_raw": nm,
                "drug_normalized": normalize_drug_name(nm),
                "intervention_type": it_type
            })

    # arm 信息（如果有）
    arms = safe_get(ps, ["armsInterventionsModule", "armGroups"], []) or []
    arm_map = []
    for a in arms:
        arm_label = a.get("label") or ""
        arm_type = a.get("type") or ""
        arm_inters = a.get("interventionNames") or []
        for nm in arm_inters:
            nm = (nm or "").strip()
            if not nm:
                continue
            if is_placebo_like(nm, ""):
                continue
            arm_map.append((arm_label, arm_type, normalize_drug_name(nm), nm))

    rows = []
    if drugs:
        is_combo = 1 if len(drugs) > 1 else 0  # ✅ 放外面，少算很多次

        for d in drugs:
            matched_arms = [(al, at) for (al, at, dn, raw) in arm_map if dn == d["drug_normalized"]]
            if not matched_arms:
                matched_arms = [("", "")]

            for (arm_label, arm_type) in matched_arms:
                rows.append({
                    "nctId": nct,
                    "briefTitle": title,
                    "overallStatus": overall,
                    "phase": ",".join(phase) if isinstance(phase, list) else (phase or ""),
                    "studyType": study_type,
                    "leadSponsor": sponsor,
                    "conditions": cond_str,
                    "drug_raw": d["drug_raw"],
                    "drug_normalized": d["drug_normalized"],
                    "intervention_type": d["intervention_type"],
                    "arm_label": arm_label,
                    "arm_type": arm_type,
                    "is_combo": is_combo,
                    "outcome_label_mvp": out_label,
                    "outcome_confidence_mvp": out_conf,
                    "has_resultsSection": 1 if bool(rs) else 0,
                })
    return rows

def main():
    # Step 1
    candidates = list_studies()
    # 只先保留 HasResults=TRUE 的（进一步减少抓详情的数量）
    # 注意：HasResults 字段来自 listStudies 的 fields 选择 :contentReference[oaicite:4]{index=4}
    filtered = []
    for s in candidates:
        # v2 字段可能在 derivedSection 或直接扁平返回；这里做宽容取值
        has_results = s.get("hasResults")
        if has_results is None:
            has_results = s.get("HasResults")
        if str(has_results).lower() in {"true", "1", "yes"}:
            filtered.append(s)
        if not filtered:
            print("WARN: HasResults filter returned 0 studies; falling back to candidates without HasResults prefilter.")
            filtered = candidates
    # Step 2 & 3
    trial_rows = []
    drug_rows = []
    manual_rows = []

    for s in filtered:
        nct = s.get("protocolSection", {}).get("identificationModule", {}).get("nctId") or s.get("nctId") or s.get("NCTId")
        if not nct:
            continue

        study = get_study(nct)
        ps = study.get("protocolSection", {}) or {}
        rs = study.get("resultsSection", {}) or {}

        overall = safe_get(ps, ["statusModule", "overallStatus"], "")
        phases = safe_get(ps, ["designModule", "phases"], []) or []
        study_type = safe_get(ps, ["designModule", "studyType"], "")

        # 主池A再加一道：Interventional + Phase2/3（你也可放宽）
        is_interventional = (study_type or "").upper() == "INTERVENTIONAL"
        has_phase23 = any(p in {"PHASE2", "PHASE3", "PHASE2_PHASE3", "PHASE4"} for p in (phases or []))

        out_label, out_conf = label_outcome_from_results(rs)

        trial_rows.append({
            "nctId": safe_get(ps, ["identificationModule", "nctId"], nct),
            "briefTitle": safe_get(ps, ["identificationModule", "briefTitle"], ""),
            "overallStatus": overall,
            "phases": ",".join(phases) if isinstance(phases, list) else (phases or ""),
            "studyType": study_type,
            "has_resultsSection": 1 if bool(rs) else 0,
            "mvp_outcome_label": out_label,
            "mvp_outcome_confidence": out_conf,
            "pass_interventional": 1 if is_interventional else 0,
            "pass_phase23": 1 if has_phase23 else 0,
        })

        rows = extract_drug_level_rows(study)
        drug_rows.extend(rows)

        # 人工复核队列：NEGATIVE/UNCLEAR/MIXED 且 Phase2/3 且 Interventional
        if is_interventional and has_phase23 and out_label in {"NEGATIVE", "UNCLEAR", "MIXED"}:
            manual_rows.append({
                "nctId": safe_get(ps, ["identificationModule", "nctId"], nct),
                "briefTitle": safe_get(ps, ["identificationModule", "briefTitle"], ""),
                "phases": ",".join(phases) if isinstance(phases, list) else (phases or ""),
                "mvp_outcome_label": out_label,
                "mvp_outcome_confidence": out_conf,
                "why_manual": "MVP判定不确定/或可能负结果，需要你快速看一眼主要终点结论",
            })

    pd.DataFrame(trial_rows).to_csv("poolA_trials.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(drug_rows).to_csv("poolA_drug_level.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(manual_rows).to_csv("manual_review_queue.csv", index=False, encoding="utf-8-sig")

    print("DONE:")
    print(" - poolA_trials.csv")
    print(" - poolA_drug_level.csv")
    print(" - manual_review_queue.csv")

if __name__ == "__main__":
    main()
