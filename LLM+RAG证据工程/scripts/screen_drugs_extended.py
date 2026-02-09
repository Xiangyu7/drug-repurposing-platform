#!/usr/bin/env python3
"""screen_drugs_extended.py — 多源药物筛选

从 6 个公开数据源筛选候选药物，按 7 维证据交叉评分。

数据源:
  1. ClinicalTrials.gov  — 失败/完成的临床试验药物
  2. ChEMBL              — 已知靶点活性的药物（靶点→药物反查）
  3. OpenTargets          — 靶点-疾病遗传关联（GWAS/基因组学证据）
  4. DrugBank (via ChEMBL)— 已批准药物的适应症 + 靶点
  5. repoDB               — 药物重定位金标准（已验证的正/负样本）
  6. TTD                  — 靶点可药性 + 靶点-药物-疾病三元组
  7. 用户自定义 CSV       — 你自己的未公开药物

策略:
  - 先从 OpenTargets 找到疾病的 top 靶点（有遗传证据）
  - 再从 ChEMBL 找打这些靶点的已有药物
  - 同时从 CT.gov 找该疾病的失败试验药物
  - 交叉打分: 同时出现在多个来源的药物得分更高
  - 混入用户自定义药物

用法:
    # 标准用法（疾病名）
    python scripts/screen_drugs_extended.py --disease atherosclerosis

    # 用 OpenTargets disease ID（更精确）
    python scripts/screen_drugs_extended.py --disease atherosclerosis --disease-id EFO_0003914

    # 追加私有药物
    python scripts/screen_drugs_extended.py --disease atherosclerosis --append-csv data/my_drugs.csv

    # 只跑部分数据源（快速测试）
    python scripts/screen_drugs_extended.py --disease atherosclerosis --sources ctgov,chembl

    # 指定输出
    python scripts/screen_drugs_extended.py --disease atherosclerosis --outdir output/screen_extended
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests

# ═══════════════════════ 常量 ═══════════════════════

CTGOV_API = "https://clinicaltrials.gov/api/v2"
CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
OT_API = "https://api.platform.opentargets.org/api/v4/graphql"
REPODB_URL = "https://ndownloader.figshare.com/files/7341422"
TTD_TARGET_URL = "https://ttd.idrblab.cn/files/download/P1-01-TTD_target_download.txt"
TTD_DRUG_DISEASE_URL = "https://ttd.idrblab.cn/files/download/P1-05-Drug_disease.txt"

TIMEOUT = 30
MAX_RETRIES = 3
SLEEP = 0.5

PLACEBO_PATTERNS = [
    r"\bplacebo\b", r"\bvehicle\b", r"\bsham\b", r"\bsaline\b",
    r"\bstandard\s+of\s+care\b", r"\busual\s+care\b",
]

NEGATIVE_HINTS = [
    r"did not meet (the )?primary endpoint",
    r"failed to meet (the )?primary endpoint",
    r"no significant (difference|benefit)",
    r"not statistically significant",
    r"lack of efficacy", r"futility",
]

# ═══════════════════════ 工具 ═══════════════════════

def http_get(url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None) -> Any:
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "10")) + 1
                print(f"    [429] 等待 {wait}s")
                time.sleep(wait)
                r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"请求失败: {last}")


def http_post(url: str, json_body: Dict, headers: Optional[Dict] = None) -> Any:
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(url, json=json_body, headers=headers, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "10")) + 1
                time.sleep(wait)
                r = requests.post(url, json=json_body, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"POST 失败: {last}")


def stable_drug_id(name: str) -> str:
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()
    return "D" + h[:10].upper()


def normalize(name: str) -> str:
    if not name:
        return ""
    x = name.lower().strip()
    x = re.sub(r"[\(\)\[\]\{\},;:/\\\"']", " ", x)
    x = re.sub(r"\b\d+\s*(mg|mcg|ug|ml|g|%|units?|iu)\b", "", x, flags=re.I)
    x = re.sub(r"\b(tablet|capsule|injection|infusion|hydrochloride|sodium|potassium|mesylate|besylate|maleate|fumarate|tartrate|succinate|citrate)\b",
               "", x, flags=re.I)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def is_placebo(name: str) -> bool:
    x = (name or "").lower()
    return any(re.search(p, x) for p in PLACEBO_PATTERNS) or x in {"control", "none", "no intervention"}


def flatten_text(obj: Any) -> str:
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


def safe_get(d: Any, *keys: str, default: Any = "") -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default

# ═══════════════════════ 数据源 1: ClinicalTrials.gov ═══════════════════════

def fetch_ctgov(disease: str, max_studies: int = 300) -> List[Dict]:
    """从 CT.gov 搜索失败/完成的 Phase 2/3 试验"""
    print(f"\n[1/6] ClinicalTrials.gov — 搜索 '{disease}'")

    studies = []
    page_token = None
    pulled = 0

    while pulled < max_studies:
        params = {
            "pageSize": 100,
            "format": "json",
            "sort": "LastUpdatePostDate:desc",
            "query.cond": disease,
            "filter.overallStatus": "COMPLETED,TERMINATED,WITHDRAWN,SUSPENDED",
            "filter.phase": "PHASE2,PHASE3",
        }
        if page_token:
            params["pageToken"] = page_token

        data = http_get(f"{CTGOV_API}/studies", params)
        batch = data.get("studies", [])
        studies.extend(batch)
        pulled += len(batch)

        if pulled == len(batch):
            total = data.get("totalCount", "?")
            print(f"  CT.gov 匹配 {total} 条，拉取上限 {max_studies}")

        page_token = data.get("nextPageToken")
        if not page_token or not batch:
            break
        time.sleep(1.3)

    drugs = {}  # canonical_name → info dict
    for s in studies[:max_studies]:
        ps = s.get("protocolSection", {}) or {}
        nct = safe_get(ps, "identificationModule", "nctId")
        title = safe_get(ps, "identificationModule", "briefTitle")
        overall = safe_get(ps, "statusModule", "overallStatus")
        phases = safe_get(ps, "designModule", "phases", default=[])
        phase_str = ",".join(phases) if isinstance(phases, list) else str(phases or "")
        conditions = safe_get(ps, "conditionsModule", "conditions", default=[])
        cond_str = " | ".join(conditions[:5]) if isinstance(conditions, list) else str(conditions)

        interventions = safe_get(ps, "armsInterventionsModule", "interventions", default=[])
        if not isinstance(interventions, list):
            continue

        for iv in interventions:
            raw = (iv.get("name") or "").strip()
            iv_type = (iv.get("type") or "").upper()
            if not raw or iv_type not in {"DRUG", "BIOLOGICAL"}:
                continue
            if is_placebo(raw):
                continue
            canon = normalize(raw)
            if not canon:
                continue

            if canon not in drugs:
                drugs[canon] = {
                    "canonical_name": canon,
                    "drug_raw": raw,
                    "source_ctgov": True,
                    "ctgov_nct_ids": [],
                    "ctgov_phases": set(),
                    "ctgov_statuses": set(),
                    "ctgov_conditions": set(),
                    "ctgov_trial_count": 0,
                    "max_phase": 0,
                }
            d = drugs[canon]
            d["ctgov_nct_ids"].append(nct)
            d["ctgov_phases"].add(phase_str)
            d["ctgov_statuses"].add(overall)
            d["ctgov_conditions"].add(cond_str)
            d["ctgov_trial_count"] += 1
            for ph in (phases if isinstance(phases, list) else []):
                if "3" in ph:
                    d["max_phase"] = max(d["max_phase"], 3)
                elif "2" in ph:
                    d["max_phase"] = max(d["max_phase"], 2)

    print(f"  提取 {len(drugs)} 个独立药物 (从 {len(studies)} 条试验)")
    return list(drugs.values())

# ═══════════════════════ 数据源 2: OpenTargets ═══════════════════════

def resolve_disease_id(disease_name: str) -> Optional[str]:
    """通过 OpenTargets 搜索获取疾病 EFO ID"""
    query = """
    query SearchDisease($q: String!) {
      search(queryString: $q, entityNames: ["disease"], page: {size: 5, index: 0}) {
        hits {
          id
          name
          entity
        }
      }
    }
    """
    try:
        data = http_post(OT_API, {"query": query, "variables": {"q": disease_name}})
        hits = data.get("data", {}).get("search", {}).get("hits", [])
        for h in hits:
            if h.get("entity") == "disease":
                return h["id"]
    except Exception as e:
        print(f"  [警告] OpenTargets 疾病搜索失败: {e}")
    return None


def fetch_opentargets_targets(disease_id: str, top_n: int = 50) -> List[Dict]:
    """从 OpenTargets 获取疾病的 top 靶点（按遗传关联评分）"""
    print(f"\n[2/6] OpenTargets — 疾病靶点 (disease_id={disease_id})")

    query = """
    query DiseaseTargets($diseaseId: String!, $size: Int!) {
      disease(efoId: $diseaseId) {
        id
        name
        associatedTargets(page: {size: $size, index: 0}) {
          count
          rows {
            target {
              id
              approvedSymbol
              approvedName
            }
            score
            datatypeScores {
              id
              score
            }
          }
        }
      }
    }
    """
    try:
        data = http_post(OT_API, {
            "query": query,
            "variables": {"diseaseId": disease_id, "size": top_n}
        })
        disease_data = data.get("data", {}).get("disease", {})
        if not disease_data:
            print(f"  [警告] 未找到疾病 {disease_id}")
            return []

        rows = disease_data.get("associatedTargets", {}).get("rows", [])
        targets = []
        for r in rows:
            tgt = r.get("target", {})
            # 提取遗传证据评分
            genetic_score = 0.0
            for ds in (r.get("datatypeScores") or []):
                if ds.get("id") in ("genetic_association", "ot_genetics_portal"):
                    genetic_score = max(genetic_score, ds.get("score", 0))

            targets.append({
                "ensembl_id": tgt.get("id", ""),
                "gene_symbol": tgt.get("approvedSymbol", ""),
                "gene_name": tgt.get("approvedName", ""),
                "overall_score": r.get("score", 0),
                "genetic_score": genetic_score,
            })

        # 按遗传证据排序
        targets.sort(key=lambda x: x["genetic_score"], reverse=True)
        n_genetic = sum(1 for t in targets if t["genetic_score"] > 0)
        print(f"  获取 {len(targets)} 个靶点，其中 {n_genetic} 个有遗传证据")
        return targets

    except Exception as e:
        print(f"  [错误] OpenTargets 查询失败: {e}")
        return []

# ═══════════════════════ 数据源 3: ChEMBL ═══════════════════════

def fetch_chembl_drugs_for_targets(targets: List[Dict], max_targets: int = 30) -> List[Dict]:
    """从 ChEMBL 查找打指定靶点的已知药物"""
    print(f"\n[3/6] ChEMBL — 靶点药物反查 (top {max_targets} 靶点)")

    drugs = {}
    targets_queried = 0

    for tgt in targets[:max_targets]:
        gene = tgt["gene_symbol"]
        if not gene:
            continue

        # 搜索 ChEMBL target by gene symbol
        try:
            url = f"{CHEMBL_API}/target/search.json"
            data = http_get(url, params={"q": gene, "limit": 5, "format": "json"})
            time.sleep(SLEEP)

            chembl_targets = data.get("targets", [])
            target_ids = []
            for ct in chembl_targets:
                # 优先人类靶点
                organism = (ct.get("organism") or "").lower()
                if "homo sapiens" in organism:
                    target_ids.append(ct["target_chembl_id"])

            if not target_ids:
                continue

            targets_queried += 1

            # 查找打这些靶点的已知药物 (mechanism of action)
            for tid in target_ids[:2]:
                try:
                    moa_url = f"{CHEMBL_API}/mechanism.json"
                    moa_data = http_get(moa_url, params={
                        "target_chembl_id": tid,
                        "limit": 50,
                        "format": "json",
                    })
                    time.sleep(SLEEP)

                    for mech in moa_data.get("mechanisms", []):
                        mol_id = mech.get("molecule_chembl_id", "")
                        moa_desc = mech.get("mechanism_of_action", "")
                        action = mech.get("action_type", "")

                        if not mol_id:
                            continue

                        # 获取药物名称
                        try:
                            mol_url = f"{CHEMBL_API}/molecule/{mol_id}.json"
                            mol_data = http_get(mol_url, params={"format": "json"})
                            time.sleep(SLEEP)

                            pref_name = (mol_data.get("pref_name") or "").strip()
                            if not pref_name:
                                continue

                            canon = normalize(pref_name)
                            max_phase = mol_data.get("max_phase") or 0

                            # 只保留进入过临床的药物 (max_phase >= 1)
                            if max_phase < 1:
                                continue

                            if canon not in drugs:
                                drugs[canon] = {
                                    "canonical_name": canon,
                                    "drug_raw": pref_name,
                                    "source_chembl": True,
                                    "chembl_id": mol_id,
                                    "chembl_max_phase": max_phase,
                                    "chembl_targets": [],
                                    "chembl_mechanisms": [],
                                    "chembl_target_genes": [],
                                    "has_genetic_evidence": False,
                                    "genetic_score": 0.0,
                                }
                            d = drugs[canon]
                            if tid not in d["chembl_targets"]:
                                d["chembl_targets"].append(tid)
                            if moa_desc and moa_desc not in d["chembl_mechanisms"]:
                                d["chembl_mechanisms"].append(moa_desc)
                            if gene not in d["chembl_target_genes"]:
                                d["chembl_target_genes"].append(gene)
                            if tgt["genetic_score"] > 0:
                                d["has_genetic_evidence"] = True
                                d["genetic_score"] = max(d["genetic_score"], tgt["genetic_score"])

                        except Exception:
                            continue

                except Exception:
                    continue

        except Exception as e:
            print(f"    {gene}: 查询失败 ({e})")
            continue

        if targets_queried % 5 == 0:
            print(f"    已查 {targets_queried}/{min(max_targets, len(targets))} 个靶点, 发现 {len(drugs)} 个药物")

    n_approved = sum(1 for d in drugs.values() if d.get("chembl_max_phase", 0) >= 4)
    n_genetic = sum(1 for d in drugs.values() if d.get("has_genetic_evidence"))
    print(f"  共发现 {len(drugs)} 个药物 ({n_approved} 个已批准, {n_genetic} 个有遗传靶点证据)")
    return list(drugs.values())

# ═══════════════════════ 数据源 4: 已批准药物跨适应症 ═══════════════════════

def fetch_approved_drugs_for_disease(disease_id: str) -> List[Dict]:
    """从 OpenTargets 获取已有该疾病靶点的已批准药物"""
    print(f"\n[4/6] OpenTargets — 已知药物 (disease_id={disease_id})")

    query = """
    query KnownDrugs($diseaseId: String!, $size: Int!) {
      disease(efoId: $diseaseId) {
        knownDrugs(size: $size) {
          count
          rows {
            drug {
              id
              name
              drugType
              maximumClinicalTrialPhase
              isApproved
              mechanismsOfAction {
                rows {
                  mechanismOfAction
                  targets {
                    approvedSymbol
                  }
                }
              }
            }
            phase
            status
            urls {
              url
              name
            }
          }
        }
      }
    }
    """
    try:
        data = http_post(OT_API, {
            "query": query,
            "variables": {"diseaseId": disease_id, "size": 200}
        })
        known = data.get("data", {}).get("disease", {}).get("knownDrugs", {})
        rows = known.get("rows", [])

        drugs = {}
        for r in rows:
            drug_info = r.get("drug", {})
            name = (drug_info.get("name") or "").strip()
            if not name:
                continue
            canon = normalize(name)
            if not canon:
                continue

            is_approved = drug_info.get("isApproved", False)
            max_phase = drug_info.get("maximumClinicalTrialPhase") or 0
            trial_phase = r.get("phase") or 0
            trial_status = r.get("status") or ""

            # 提取 MOA
            moas = []
            target_genes = []
            for moa_row in (drug_info.get("mechanismsOfAction", {}).get("rows") or []):
                moa_text = moa_row.get("mechanismOfAction", "")
                if moa_text:
                    moas.append(moa_text)
                for tgt in (moa_row.get("targets") or []):
                    sym = tgt.get("approvedSymbol", "")
                    if sym:
                        target_genes.append(sym)

            if canon not in drugs:
                drugs[canon] = {
                    "canonical_name": canon,
                    "drug_raw": name,
                    "source_opentargets_known": True,
                    "ot_drug_id": drug_info.get("id", ""),
                    "ot_is_approved": is_approved,
                    "ot_max_phase": max_phase,
                    "ot_trial_phases": [],
                    "ot_trial_statuses": [],
                    "ot_mechanisms": [],
                    "ot_target_genes": [],
                }
            d = drugs[canon]
            if trial_phase:
                d["ot_trial_phases"].append(trial_phase)
            if trial_status:
                d["ot_trial_statuses"].append(trial_status)
            for m in moas:
                if m not in d["ot_mechanisms"]:
                    d["ot_mechanisms"].append(m)
            for g in target_genes:
                if g not in d["ot_target_genes"]:
                    d["ot_target_genes"].append(g)

        n_approved = sum(1 for d in drugs.values() if d.get("ot_is_approved"))
        print(f"  获取 {len(drugs)} 个已知药物 ({n_approved} 个已批准)")
        return list(drugs.values())

    except Exception as e:
        print(f"  [错误] OpenTargets 已知药物查询失败: {e}")
        return []

# ═══════════════════════ 数据源 5: repoDB 金标准 ═══════════════════════

def fetch_repodb(disease_keyword: str, cache_dir: Optional[Path] = None) -> Tuple[List[Dict], List[Dict]]:
    """
    从 repoDB 获取药物重定位的金标准正/负样本

    Returns:
        (approved_drugs, failed_drugs): 两个 list
        - approved_drugs: 已批准的 drug-indication 对（正样本）
        - failed_drugs: 失败的 drug-indication 对（负样本）
    """
    print(f"\n[5/6] repoDB — 金标准正/负样本 (keyword='{disease_keyword}')")

    # 下载或读取缓存
    cache_path = (cache_dir or Path("data/cache")) / "repodb_full.csv"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        print(f"  使用缓存: {cache_path}")
        df = pd.read_csv(cache_path, dtype=str).fillna("")
    else:
        print(f"  下载 repoDB 数据...")
        try:
            r = requests.get(REPODB_URL, timeout=60)
            r.raise_for_status()
            cache_path.write_bytes(r.content)
            df = pd.read_csv(cache_path, dtype=str).fillna("")
            print(f"  下载完成: {len(df)} 条 drug-indication 对")
        except Exception as e:
            print(f"  [错误] repoDB 下载失败: {e}")
            return [], []

    # 按疾病关键词过滤
    kw = disease_keyword.lower()
    mask = df["ind_name"].str.lower().str.contains(kw, na=False)
    matched = df[mask].copy()
    print(f"  匹配 '{disease_keyword}': {len(matched)} 条 (总共 {len(df)} 条)")

    approved = []
    failed = []
    for _, row in matched.iterrows():
        canon = normalize(row.get("drug_name", ""))
        if not canon:
            continue
        entry = {
            "canonical_name": canon,
            "drug_raw": row.get("drug_name", ""),
            "repodb_drugbank_id": row.get("drug_id", ""),
            "repodb_indication": row.get("ind_name", ""),
            "repodb_status": row.get("status", ""),
            "repodb_phase": row.get("phase", ""),
            "repodb_detail": row.get("DetailedStatus", ""),
        }
        if row.get("status", "").lower() == "approved":
            entry["source_repodb_approved"] = True
            approved.append(entry)
        elif row.get("status", "").lower() in {"terminated", "withdrawn", "suspended"}:
            entry["source_repodb_failed"] = True
            failed.append(entry)

    print(f"  正样本(Approved): {len(approved)}, 负样本(Failed): {len(failed)}")
    return approved, failed


# ═══════════════════════ 数据源 6: TTD 靶点可药性 ═══════════════════════

def _parse_ttd_target_file(text: str) -> Dict[str, Dict]:
    """解析 TTD P1-01 的多行记录格式"""
    targets = {}
    current_id = None

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            current_id = None
            continue

        parts = line.split("\t")
        if len(parts) < 3:
            continue

        tid = parts[0].strip()
        field = parts[1].strip()
        value = parts[2].strip() if len(parts) > 2 else ""

        if field == "TARGETID":
            current_id = value
            if current_id not in targets:
                targets[current_id] = {
                    "ttd_id": current_id,
                    "target_name": "",
                    "gene_name": "",
                    "target_type": "",
                    "bioclass": "",
                    "drugs": [],
                }
        elif current_id and tid == current_id:
            t = targets[current_id]
            if field == "TARGNAME":
                t["target_name"] = value
            elif field == "GENENAME":
                t["gene_name"] = value
            elif field == "TARGTYPE":
                t["target_type"] = value  # Successful / Clinical trial / Literature-reported
            elif field == "BIOCLASS":
                t["bioclass"] = value
            elif field == "DRUGINFO":
                # DRUGINFO 格式: TTD_Drug_ID \t Drug_Name \t Status
                drug_parts = parts[2:]
                if len(drug_parts) >= 2:
                    drug_name = drug_parts[1].strip() if len(drug_parts) > 1 else ""
                    drug_status = drug_parts[2].strip() if len(drug_parts) > 2 else ""
                    t["drugs"].append({
                        "ttd_drug_id": drug_parts[0].strip(),
                        "drug_name": drug_name,
                        "status": drug_status,
                    })

    return targets


def _parse_ttd_drug_disease(text: str) -> Dict[str, List[Dict]]:
    """解析 TTD P1-05 Drug-Disease 映射"""
    drug_diseases = {}  # drug_name → [{"disease": ..., "status": ...}]
    current_drug = None

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            current_drug = None
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            continue

        field = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""

        if field == "DRUGNAME":
            current_drug = value.lower().strip()
            if current_drug not in drug_diseases:
                drug_diseases[current_drug] = []
        elif field == "INDICATI" and current_drug:
            # 格式: Disease_name \t ICD-11:xxx \t Status
            disease_name = value
            status = parts[-1].strip() if len(parts) > 2 else ""
            drug_diseases[current_drug].append({
                "disease": disease_name,
                "status": status,
            })

    return drug_diseases


def fetch_ttd(disease_keyword: str, cache_dir: Optional[Path] = None) -> List[Dict]:
    """
    从 TTD 获取靶点可药性信息和 drug-target-disease 三元组

    策略:
    1. 下载 P1-01 (靶点+药物)
    2. 下载 P1-05 (药物+疾病)
    3. 找到与疾病相关的药物，标注靶点可药性
    """
    print(f"\n[6/6] TTD — 靶点可药性 + Drug-Target-Disease (keyword='{disease_keyword}')")

    cache_base = (cache_dir or Path("data/cache"))
    cache_base.mkdir(parents=True, exist_ok=True)

    # 下载 P1-01
    target_cache = cache_base / "ttd_P1-01_targets.txt"
    if target_cache.exists():
        print(f"  使用缓存: P1-01")
        target_text = target_cache.read_text(encoding="utf-8", errors="replace")
    else:
        print(f"  下载 TTD P1-01 (靶点数据)...")
        try:
            r = requests.get(TTD_TARGET_URL, timeout=120)
            r.raise_for_status()
            target_text = r.text
            target_cache.write_text(target_text, encoding="utf-8")
        except Exception as e:
            print(f"  [错误] TTD P1-01 下载失败: {e}")
            return []

    # 下载 P1-05
    dd_cache = cache_base / "ttd_P1-05_drug_disease.txt"
    if dd_cache.exists():
        print(f"  使用缓存: P1-05")
        dd_text = dd_cache.read_text(encoding="utf-8", errors="replace")
    else:
        print(f"  下载 TTD P1-05 (Drug-Disease)...")
        try:
            r = requests.get(TTD_DRUG_DISEASE_URL, timeout=120)
            r.raise_for_status()
            dd_text = r.text
            dd_cache.write_text(dd_text, encoding="utf-8")
        except Exception as e:
            print(f"  [错误] TTD P1-05 下载失败: {e}")
            return []

    # 解析
    print(f"  解析靶点数据...")
    targets = _parse_ttd_target_file(target_text)
    print(f"  解析 Drug-Disease 映射...")
    drug_diseases = _parse_ttd_drug_disease(dd_text)

    print(f"  TTD 共 {len(targets)} 个靶点, {len(drug_diseases)} 个药物")

    # 方法 1: 从 drug-disease 找与疾病相关的药物
    kw = disease_keyword.lower()
    disease_drugs = set()
    for drug_name, diseases in drug_diseases.items():
        for dd in diseases:
            if kw in dd.get("disease", "").lower():
                disease_drugs.add(drug_name)

    # 方法 2: 从靶点的 DRUGINFO 中收集所有药物，与 drug-disease 交叉
    # 构建 drug_name → target_info 反查
    drug_target_map = {}  # normalized_drug_name → [{target_info}]
    for tid, tgt in targets.items():
        for drug in tgt.get("drugs", []):
            dname = normalize(drug.get("drug_name", ""))
            if not dname:
                continue
            if dname not in drug_target_map:
                drug_target_map[dname] = []
            drug_target_map[dname].append({
                "ttd_target_id": tid,
                "target_name": tgt["target_name"],
                "gene_name": tgt["gene_name"],
                "target_type": tgt["target_type"],
                "bioclass": tgt["bioclass"],
                "drug_status_in_ttd": drug.get("status", ""),
            })

    # 构建结果: 与疾病相关的药物 + 靶点可药性
    results = {}
    for drug_name in disease_drugs:
        canon = normalize(drug_name)
        if not canon:
            continue
        target_infos = drug_target_map.get(canon, [])

        # 靶点类型统计
        target_types = set(t["target_type"] for t in target_infos if t.get("target_type"))
        gene_names = [t["gene_name"] for t in target_infos if t.get("gene_name")]
        bioclasses = set(t["bioclass"] for t in target_infos if t.get("bioclass"))

        # 可药性: Successful target = 已有批准药物的靶点
        has_successful_target = "Successful" in target_types
        has_clinical_target = "Clinical trial" in target_types

        # drug-disease 中该药物的疾病适应症
        dd_entries = drug_diseases.get(drug_name, [])
        relevant_indications = [dd for dd in dd_entries if kw in dd.get("disease", "").lower()]

        results[canon] = {
            "canonical_name": canon,
            "drug_raw": drug_name,
            "source_ttd": True,
            "ttd_target_genes": gene_names[:5],
            "ttd_target_types": list(target_types),
            "ttd_bioclasses": list(bioclasses),
            "ttd_has_successful_target": has_successful_target,
            "ttd_has_clinical_target": has_clinical_target,
            "ttd_n_targets": len(target_infos),
            "ttd_indications": [d["disease"] for d in relevant_indications[:3]],
        }

    n_druggable = sum(1 for d in results.values() if d.get("ttd_has_successful_target"))
    print(f"  疾病相关药物: {len(results)} ({n_druggable} 个有'Successful target')")
    return list(results.values())


# ═══════════════════════ 交叉评分 ═══════════════════════

def cross_score(merged: pd.DataFrame) -> pd.DataFrame:
    """多维度交叉评分（7 个维度，满分 100）"""
    scores = []
    for _, row in merged.iterrows():
        s = 0.0
        reasons = []

        # 维度 1: 数据源覆盖度 (最高 20 分)
        n_sources = sum([
            bool(row.get("source_ctgov")),
            bool(row.get("source_chembl")),
            bool(row.get("source_opentargets_known")),
            bool(row.get("source_ttd")),
            bool(row.get("source_repodb_approved")),
            bool(row.get("source_private")),
        ])
        s += min(20, n_sources * 5)
        if n_sources >= 3:
            reasons.append(f"多源验证({n_sources}源)")

        # 维度 2: 遗传证据 (最高 20 分)
        genetic = row.get("genetic_score", 0)
        if isinstance(genetic, str):
            try:
                genetic = float(genetic)
            except (ValueError, TypeError):
                genetic = 0
        if genetic > 0:
            s += min(20, genetic * 20)
            reasons.append(f"遗传证据({genetic:.2f})")

        # 维度 3: 临床阶段 (最高 15 分)
        max_phase = max(
            row.get("chembl_max_phase", 0) or 0,
            row.get("ot_max_phase", 0) or 0,
            row.get("max_phase", 0) or 0,
        )
        if isinstance(max_phase, str):
            try:
                max_phase = float(max_phase)
            except (ValueError, TypeError):
                max_phase = 0
        if max_phase >= 4:
            s += 15
            reasons.append("已批准药物")
        elif max_phase >= 3:
            s += 12
            reasons.append("Phase 3")
        elif max_phase >= 2:
            s += 8
            reasons.append("Phase 2")
        elif max_phase >= 1:
            s += 4

        # 维度 4: repoDB 金标准 (最高 15 分)
        if row.get("source_repodb_approved"):
            s += 15
            reasons.append("repoDB正样本(已批准适应症)")
        elif row.get("source_repodb_failed"):
            s -= 10  # 负样本扣分
            reasons.append("repoDB负样本(已失败)")

        # 维度 5: TTD 靶点可药性 (最高 15 分)
        if row.get("ttd_has_successful_target"):
            s += 15
            reasons.append("TTD成功靶点")
        elif row.get("ttd_has_clinical_target"):
            s += 8
            reasons.append("TTD临床靶点")
        elif row.get("source_ttd"):
            s += 3

        # 维度 6: 试验数量 (最高 10 分)
        n_trials = row.get("ctgov_trial_count", 0) or 0
        if isinstance(n_trials, str):
            try:
                n_trials = int(float(n_trials))
            except (ValueError, TypeError):
                n_trials = 0
        s += min(10, n_trials * 2)
        if n_trials >= 3:
            reasons.append(f"{n_trials}个试验")

        # 维度 7: 已知机制 (最高 5 分)
        has_moa = bool(row.get("chembl_mechanisms")) or bool(row.get("ot_mechanisms"))
        if has_moa:
            s += 5
            reasons.append("机制已知")

        scores.append({
            "cross_score": round(max(0, min(100, s)), 1),
            "n_sources": n_sources,
            "priority_reasons": "; ".join(reasons) if reasons else "单源",
        })

    score_df = pd.DataFrame(scores)
    merged = merged.reset_index(drop=True)
    score_df = score_df.reset_index(drop=True)
    return pd.concat([merged, score_df], axis=1)

# ═══════════════════════ 合并 ═══════════════════════

def merge_all_sources(
    ctgov_drugs: List[Dict],
    chembl_drugs: List[Dict],
    ot_known_drugs: List[Dict],
    repodb_approved: List[Dict],
    repodb_failed: List[Dict],
    ttd_drugs: List[Dict],
    private_drugs: List[Dict],
) -> pd.DataFrame:
    """把所有来源的药物合并到一张表

    Args:
        ctgov_drugs: ClinicalTrials.gov 药物
        chembl_drugs: ChEMBL 靶点反查药物
        ot_known_drugs: OpenTargets 已知药物
        repodb_approved: repoDB 正样本（已批准适应症）
        repodb_failed: repoDB 负样本（失败适应症）
        ttd_drugs: TTD 靶点可药性药物
        private_drugs: 用户自定义药物
    """
    merged = {}

    def _add(drug_dict: Dict):
        canon = drug_dict.get("canonical_name", "")
        if not canon:
            return
        if canon not in merged:
            merged[canon] = {"canonical_name": canon}
        # 对于 repoDB failed，不要覆盖 approved 标记
        existing = merged[canon]
        if drug_dict.get("source_repodb_failed") and existing.get("source_repodb_approved"):
            # 同一药物既有 approved 又有 failed 记录 → 保留 approved，标记 mixed
            existing["repodb_mixed"] = True
            return
        merged[canon].update(drug_dict)

    for d in ctgov_drugs:
        _add(d)
    for d in chembl_drugs:
        _add(d)
    for d in ot_known_drugs:
        _add(d)
    for d in repodb_approved:
        _add(d)
    for d in repodb_failed:
        _add(d)
    for d in ttd_drugs:
        _add(d)
    for d in private_drugs:
        d["source_private"] = True
        _add(d)

    rows = list(merged.values())
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # 添加 drug_id
    df["drug_id"] = df["canonical_name"].apply(stable_drug_id)

    # 序列化 list/set 列
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, (list, set))).any():
            df[col] = df[col].apply(lambda x: "; ".join(str(i) for i in x) if isinstance(x, (list, set)) else x)

    return df

# ═══════════════════════ 主函数 ═══════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="多源药物筛选 + 交叉评分",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--disease", required=True, help="疾病名称")
    parser.add_argument("--disease-id", default=None, help="OpenTargets 疾病 EFO ID (可选，不填则自动搜索)")
    parser.add_argument("--max-studies", type=int, default=300, help="CT.gov 最大拉取数")
    parser.add_argument("--max-targets", type=int, default=30, help="ChEMBL 查询的靶点数")
    parser.add_argument("--append-csv", default=None, help="追加自定义药物 CSV")
    parser.add_argument("--outdir", default="output/screen_extended", help="输出目录")
    parser.add_argument("--sources", default="ctgov,opentargets,chembl,repodb,ttd",
                        help="启用的数据源 (逗号分隔): ctgov,opentargets,chembl,repodb,ttd")

    args = parser.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sources = set(args.sources.lower().split(","))
    start_time = time.time()

    print(f"\n{'='*60}")
    print(f"多源药物筛选")
    print(f"  疾病:     {args.disease}")
    print(f"  数据源:   {', '.join(sorted(sources))}")
    print(f"{'='*60}")

    # ─── 解析疾病 ID ───
    disease_id = args.disease_id
    needs_ot = "opentargets" in sources or "chembl" in sources
    if not disease_id and needs_ot:
        print(f"\n正在搜索 OpenTargets 疾病 ID...")
        disease_id = resolve_disease_id(args.disease)
        if disease_id:
            print(f"  找到: {disease_id}")
        else:
            print(f"  [警告] 未找到疾病 ID，将跳过 OpenTargets/ChEMBL 靶点查询")

    # ─── 数据源 1: CT.gov ───
    ctgov_drugs = []
    if "ctgov" in sources:
        ctgov_drugs = fetch_ctgov(args.disease, max_studies=args.max_studies)

    # ─── 数据源 2-3: OpenTargets 靶点 → ChEMBL 药物 ───
    ot_targets = []
    chembl_drugs = []
    if disease_id and "opentargets" in sources:
        ot_targets = fetch_opentargets_targets(disease_id)

    if ot_targets and "chembl" in sources:
        chembl_drugs = fetch_chembl_drugs_for_targets(ot_targets, max_targets=args.max_targets)

    # ─── 数据源 4: OpenTargets 已知药物 ───
    ot_known_drugs = []
    if disease_id and "opentargets" in sources:
        ot_known_drugs = fetch_approved_drugs_for_disease(disease_id)

    # ─── 数据源 5: repoDB 金标准 ───
    repodb_approved = []
    repodb_failed = []
    if "repodb" in sources:
        repodb_approved, repodb_failed = fetch_repodb(
            args.disease, cache_dir=outdir / "cache"
        )

    # ─── 数据源 6: TTD 靶点可药性 ───
    ttd_drugs = []
    if "ttd" in sources:
        ttd_drugs = fetch_ttd(
            args.disease, cache_dir=outdir / "cache"
        )

    # ─── 用户自定义 ───
    private_drugs = []
    if args.append_csv:
        print(f"\n正在读取自定义药物: {args.append_csv}")
        df_priv = pd.read_csv(args.append_csv, dtype=str).fillna("")
        if "drug_name" not in df_priv.columns:
            print(f"  [错误] CSV 必须包含 drug_name 列")
            sys.exit(1)
        for _, r in df_priv.iterrows():
            private_drugs.append({
                "canonical_name": normalize(r["drug_name"]),
                "drug_raw": r["drug_name"],
                "source_private": True,
                "private_phase": r.get("phase", ""),
                "private_conditions": r.get("conditions", r.get("disease", "")),
                "private_outcome": r.get("outcome", ""),
                "private_notes": r.get("notes", ""),
            })
        print(f"  读入 {len(private_drugs)} 个自定义药物")

    # ─── 合并 + 评分 ───
    print(f"\n{'='*60}")
    print(f"合并所有数据源...")
    merged = merge_all_sources(
        ctgov_drugs, chembl_drugs, ot_known_drugs,
        repodb_approved, repodb_failed, ttd_drugs,
        private_drugs,
    )

    if merged.empty:
        print("[警告] 未找到任何药物。请检查搜索条件。")
        sys.exit(0)

    merged = cross_score(merged)
    merged = merged.sort_values("cross_score", ascending=False).reset_index(drop=True)

    # ─── 保存 ───
    # 完整结果
    merged.to_csv(outdir / "drugs_all_sources.csv", index=False, encoding="utf-8-sig")

    # Step6 可直接使用的输入
    step6 = merged[["drug_id", "canonical_name"]].drop_duplicates()
    step6.to_csv(outdir / "step6_rank.csv", index=False, encoding="utf-8-sig")

    # 精选列: 给人看的摘要
    summary_cols = ["drug_id", "canonical_name", "cross_score", "n_sources", "priority_reasons"]
    for col in ["source_ctgov", "source_chembl", "source_opentargets_known",
                "source_repodb_approved", "source_repodb_failed", "source_ttd", "source_private",
                "has_genetic_evidence", "genetic_score", "chembl_max_phase", "ctgov_trial_count",
                "chembl_mechanisms", "ot_mechanisms", "chembl_target_genes", "ot_target_genes",
                "repodb_status", "repodb_indication",
                "ttd_has_successful_target", "ttd_has_clinical_target", "ttd_target_genes"]:
        if col in merged.columns:
            summary_cols.append(col)
    summary = merged[[c for c in summary_cols if c in merged.columns]].copy()
    summary.to_csv(outdir / "drugs_ranked_summary.csv", index=False, encoding="utf-8-sig")

    # 靶点信息
    if ot_targets:
        pd.DataFrame(ot_targets).to_csv(outdir / "disease_targets.csv", index=False, encoding="utf-8-sig")

    # Manifest
    elapsed = time.time() - start_time
    manifest = {
        "script": "screen_drugs_extended.py",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "disease_id": disease_id,
        "stats": {
            "ctgov_drugs": len(ctgov_drugs),
            "chembl_drugs": len(chembl_drugs),
            "opentargets_known_drugs": len(ot_known_drugs),
            "repodb_approved": len(repodb_approved),
            "repodb_failed": len(repodb_failed),
            "ttd_drugs": len(ttd_drugs),
            "private_drugs": len(private_drugs),
            "total_merged": len(merged),
            "n_with_genetic_evidence": int(merged.get("has_genetic_evidence", pd.Series([False])).sum()),
            "n_multi_source": int((merged.get("n_sources", pd.Series([0])) >= 2).sum()),
            "ot_targets_queried": len(ot_targets),
            "elapsed_seconds": round(elapsed, 1),
        },
    }
    with open(outdir / "screen_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ─── 摘要 ───
    n_genetic = int(merged.get("has_genetic_evidence", pd.Series([False])).sum()) if "has_genetic_evidence" in merged.columns else 0
    n_multi = int((merged.get("n_sources", pd.Series([0])) >= 2).sum())
    n_approved = 0
    if "chembl_max_phase" in merged.columns:
        n_approved = int((merged["chembl_max_phase"].fillna(0).astype(float) >= 4).sum())
    if "ot_is_approved" in merged.columns:
        n_approved = max(n_approved, int(merged["ot_is_approved"].fillna(False).sum()))

    # repoDB / TTD 统计
    n_repodb_pos = int(merged.get("source_repodb_approved", pd.Series([False])).fillna(False).sum()) if "source_repodb_approved" in merged.columns else 0
    n_repodb_neg = int(merged.get("source_repodb_failed", pd.Series([False])).fillna(False).sum()) if "source_repodb_failed" in merged.columns else 0
    n_ttd = int(merged.get("source_ttd", pd.Series([False])).fillna(False).sum()) if "source_ttd" in merged.columns else 0
    n_ttd_druggable = int(merged.get("ttd_has_successful_target", pd.Series([False])).fillna(False).sum()) if "ttd_has_successful_target" in merged.columns else 0

    print(f"\n{'='*60}")
    print(f"筛选完成!")
    print(f"{'='*60}")
    print(f"  数据源:")
    print(f"    CT.gov:              {len(ctgov_drugs)} 个药物")
    print(f"    ChEMBL (靶点反查):   {len(chembl_drugs)} 个药物")
    print(f"    OpenTargets (已知):   {len(ot_known_drugs)} 个药物")
    print(f"    repoDB (正/负样本):   {len(repodb_approved)}/{len(repodb_failed)} 个药物")
    print(f"    TTD (靶点可药性):    {len(ttd_drugs)} 个药物")
    print(f"    自定义:              {len(private_drugs)} 个药物")
    print(f"  合并后:")
    print(f"    总药物数:            {len(merged)}")
    print(f"    多源交叉验证:        {n_multi} ({n_multi*100//max(1,len(merged))}%)")
    print(f"    有遗传证据:          {n_genetic} ({n_genetic*100//max(1,len(merged))}%)")
    print(f"    已批准药物:          {n_approved}")
    print(f"    repoDB 正样本:       {n_repodb_pos}")
    print(f"    repoDB 负样本:       {n_repodb_neg}")
    print(f"    TTD 可药性靶点:      {n_ttd_druggable}/{n_ttd}")
    print(f"  耗时:                  {elapsed:.1f}s")
    print(f"\n输出: {outdir.resolve()}")
    print(f"  drugs_ranked_summary.csv — 排序后的药物摘要 (人类可读)")
    print(f"  drugs_all_sources.csv    — 完整合并数据")
    print(f"  step6_rank.csv           — Step6 直接输入")
    if ot_targets:
        print(f"  disease_targets.csv      — 疾病靶点列表")

    # Top 15 预览
    print(f"\n{'='*60}")
    print(f"Top 15 候选药物:")
    print(f"{'='*60}")
    print(f"{'排名':>4s}  {'评分':>5s}  {'源':>3s}  {'遗传':>4s}  {'repoDB':>6s}  {'药物名':<28s}  {'理由'}")
    print(f"{'-'*4}  {'-'*5}  {'-'*3}  {'-'*4}  {'-'*6}  {'-'*28}  {'-'*30}")
    for i, (_, row) in enumerate(merged.head(15).iterrows()):
        gen = "Y" if row.get("has_genetic_evidence") else ""
        repo = "+" if row.get("source_repodb_approved") else ("-" if row.get("source_repodb_failed") else "")
        name = str(row["canonical_name"])[:28]
        reasons = str(row.get("priority_reasons", ""))[:40]
        print(f"{i+1:>4d}  {row['cross_score']:>5.1f}  {row['n_sources']:>3.0f}  {gen:>4s}  {repo:>6s}  {name:<28s}  {reasons}")

    print(f"\n下一步:")
    print(f"  1. 查看 drugs_ranked_summary.csv，确认候选列表")
    print(f"  2. 运行 Step6: python scripts/step6_evidence_extraction.py --rank_in {outdir}/step6_rank.csv")
    print()


if __name__ == "__main__":
    main()
