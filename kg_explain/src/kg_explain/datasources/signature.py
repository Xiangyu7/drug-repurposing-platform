"""
基因签名驱动的药物反查模块 (Signature-driven Drug Discovery)

用途:
  从疾病基因签名 (dsmeta_signature_pipeline 输出) 出发,
  通过 ChEMBL 反查作用于这些基因靶点的已批准药物,
  实现真正的跨疾病 drug repurposing.

流程:
  Gene Symbol → UniProt accession (via UniProt REST)
  → ChEMBL target_chembl_id (via ChEMBL /target.json)
  → Drug molecules (via ChEMBL /mechanism.json)
  → 过滤 max_phase >= threshold
  → 输出兼容现有 pipeline 的 CSV

输出文件:
  - drug_from_signature.csv: 完整反查结果 (含基因来源信息)
  - drug_chembl_map.csv:     去重药物映射 (兼容 Step 4)
  - edge_drug_target.csv:    药物-靶点边 (兼容 Step 5)
  - drug_canonical.csv:      规范名称映射 (兼容 Step 3)
  - drug_rxnorm_map.csv:     占位文件 (signature模式不需要RxNorm)
  - failed_trials_drug_rows.csv: 空占位 (signature模式无试验数据)
  - failed_drugs_summary.csv:    空占位
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from ..cache import HTTPCache, cached_get_json
from ..utils import concurrent_map, safe_str

logger = logging.getLogger(__name__)

CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
UNIPROT_API = "https://rest.uniprot.org/uniprotkb/search"


# ──────────────────────────────────────────────
#  Step A: Gene Symbol → UniProt accession
# ──────────────────────────────────────────────

def _gene_to_uniprot(cache: HTTPCache, gene_symbol: str) -> list[dict]:
    """
    基因名 → UniProt accessions (reviewed/Swiss-Prot, human only).

    Returns:
        list of {"accession": "P09917", "gene": "ALOX5"}
    """
    url = UNIPROT_API
    params = {
        "query": f"(gene_exact:{gene_symbol}) AND (organism_id:9606) AND (reviewed:true)",
        "format": "json",
        "fields": "accession,gene_names",
        "size": "5",
    }
    try:
        js = cached_get_json(cache, url, params=params)
    except Exception as e:
        logger.warning("UniProt 查询失败, gene=%s: %s", gene_symbol, e)
        return []

    results = []
    for entry in js.get("results", []):
        acc = entry.get("primaryAccession")
        if acc:
            results.append({"accession": acc, "gene": gene_symbol})
    return results


# ──────────────────────────────────────────────
#  Step B: UniProt accession → ChEMBL target
# ──────────────────────────────────────────────

def _uniprot_to_chembl_target(cache: HTTPCache, accession: str) -> list[dict]:
    """
    UniProt accession → ChEMBL target(s).

    Uses ChEMBL /target.json?target_components__accession=ACCESSION

    Returns:
        list of {"target_chembl_id": "CHEMBL215", "pref_name": "...", "target_type": "..."}
    """
    url = f"{CHEMBL_API}/target.json"
    params = {
        "target_components__accession": accession,
        "limit": 10,
    }
    try:
        js = cached_get_json(cache, url, params=params)
    except Exception as e:
        logger.warning("ChEMBL target 查询失败, accession=%s: %s", accession, e)
        return []

    results = []
    for t in js.get("targets", []):
        tid = t.get("target_chembl_id")
        if tid:
            results.append({
                "target_chembl_id": tid,
                "target_pref_name": t.get("pref_name", ""),
                "target_type": t.get("target_type", ""),
                "uniprot_accession": accession,
            })
    return results


# ──────────────────────────────────────────────
#  Step C: ChEMBL target → Drug molecules
# ──────────────────────────────────────────────

def _target_to_drugs(cache: HTTPCache, target_chembl_id: str, max_phase: int = 4) -> list[dict]:
    """
    ChEMBL target → 药物分子 (via mechanism of action).

    Args:
        target_chembl_id: ChEMBL 靶点 ID
        max_phase: 最低临床阶段 (4=已批准)

    Returns:
        list of drug dicts with molecule info
    """
    url = f"{CHEMBL_API}/mechanism.json"
    all_mechs = []
    offset = 0
    limit = 100

    # 分页获取所有 mechanisms
    while True:
        params = {
            "target_chembl_id": target_chembl_id,
            "limit": limit,
            "offset": offset,
        }
        try:
            js = cached_get_json(cache, url, params=params)
        except Exception as e:
            logger.warning("ChEMBL mechanism 查询失败, target=%s: %s", target_chembl_id, e)
            break

        mechs = js.get("mechanisms", [])
        if not mechs:
            break
        all_mechs.extend(mechs)

        # 检查是否有更多页
        if js.get("page_meta", {}).get("next") is None:
            break
        offset += limit
        if offset > 2000:  # 安全上限
            break

    # 去重 molecule_chembl_id
    seen_mols = set()
    unique_mechs = []
    for m in all_mechs:
        mol_id = m.get("molecule_chembl_id")
        if mol_id and mol_id not in seen_mols:
            seen_mols.add(mol_id)
            unique_mechs.append(m)

    # 筛选 max_phase
    results = []
    mol_ids_to_check = [m.get("molecule_chembl_id") for m in unique_mechs if m.get("molecule_chembl_id")]

    for mech in unique_mechs:
        mol_id = mech.get("molecule_chembl_id")
        if not mol_id:
            continue
        results.append({
            "molecule_chembl_id": mol_id,
            "mechanism_of_action": mech.get("mechanism_of_action", ""),
            "target_chembl_id": target_chembl_id,
        })

    return results


def _check_molecule_phase(cache: HTTPCache, molecule_chembl_id: str) -> dict | None:
    """获取分子信息, 包括 max_phase 和 pref_name."""
    url = f"{CHEMBL_API}/molecule/{molecule_chembl_id}.json"
    try:
        js = cached_get_json(cache, url)
    except Exception:
        return None

    return {
        "molecule_chembl_id": molecule_chembl_id,
        "pref_name": js.get("pref_name", ""),
        "max_phase": js.get("max_phase", 0),
        "molecule_type": js.get("molecule_type", ""),
        "first_approval": js.get("first_approval"),
    }


# ──────────────────────────────────────────────
#  Step D: 已知适应症查询
# ──────────────────────────────────────────────

def fetch_known_indications(cache: HTTPCache, molecule_chembl_id: str) -> list[dict]:
    """
    获取药物的已知适应症 (ChEMBL drug_indication).

    Returns:
        list of {"efo_id": "...", "mesh_id": "...", "indication": "...", "max_phase_for_ind": 4}
    """
    url = f"{CHEMBL_API}/drug_indication.json"
    params = {"molecule_chembl_id": molecule_chembl_id, "limit": 100}
    try:
        js = cached_get_json(cache, url, params=params)
    except Exception:
        return []

    results = []
    for ind in js.get("drug_indications", []):
        results.append({
            "molecule_chembl_id": molecule_chembl_id,
            "efo_id": ind.get("efo_id", ""),
            "mesh_id": ind.get("mesh_id", ""),
            "indication": ind.get("mesh_heading", "") or ind.get("efo_term", ""),
            "max_phase_for_ind": ind.get("max_phase_for_ind", 0),
        })
    return results


# ──────────────────────────────────────────────
#  主函数: 基因签名 → 药物列表
# ──────────────────────────────────────────────

def fetch_drugs_from_signature(
    data_dir: Path,
    cache: HTTPCache,
    signature_path: str,
    max_phase: int = 4,
    max_genes: int = 100,
    gene_source: str = "both",
) -> Path:
    """
    从疾病基因签名反查已批准药物.

    Args:
        data_dir: 数据输出目录
        cache: HTTP 缓存
        signature_path: disease_signature_meta.json 路径
        max_phase: 药物最低临床阶段 (4=已批准, 3=Phase3)
        max_genes: 使用签名中前 N 个基因 (按 weight 降序)
        gene_source: "up", "down", "both"

    Returns:
        drug_from_signature.csv 路径
    """
    # ── 1. 读取基因签名 ──
    sig_path = Path(signature_path)
    if not sig_path.exists():
        raise FileNotFoundError(f"签名文件不存在: {sig_path}")

    with open(sig_path, "r", encoding="utf-8") as f:
        sig = json.load(f)

    genes = []
    if gene_source in ("up", "both"):
        for g in sig.get("up_genes", []):
            genes.append({"gene": g["gene"], "direction": "up", "weight": abs(float(g.get("weight", 0)))})
    if gene_source in ("down", "both"):
        for g in sig.get("down_genes", []):
            genes.append({"gene": g["gene"], "direction": "down", "weight": abs(float(g.get("weight", 0)))})

    # 按 weight 降序, 取前 max_genes
    genes.sort(key=lambda x: x["weight"], reverse=True)
    genes = genes[:max_genes]
    logger.info("基因签名: %d 个基因 (source=%s, max_genes=%d)", len(genes), gene_source, max_genes)

    # ── 2. Gene → UniProt ──
    gene_symbols = [g["gene"] for g in genes]
    gene_direction_map = {g["gene"]: g for g in genes}

    def _fetch_uniprot(symbol):
        return symbol, _gene_to_uniprot(cache, symbol)

    uniprot_results = concurrent_map(
        _fetch_uniprot, gene_symbols,
        max_workers=cache.max_workers, desc="Gene→UniProt",
    )

    gene_uniprot: dict[str, list[str]] = {}  # gene → [accessions]
    for item in uniprot_results:
        if item is None:
            continue
        symbol, entries = item
        accs = [e["accession"] for e in entries]
        if accs:
            gene_uniprot[symbol] = accs

    n_mapped = len(gene_uniprot)
    logger.info("Gene→UniProt: %d/%d 基因成功映射", n_mapped, len(gene_symbols))

    # ── 3. UniProt → ChEMBL target ──
    all_accessions = []
    acc_to_gene = {}  # accession → gene_symbol
    for gene, accs in gene_uniprot.items():
        for acc in accs:
            all_accessions.append(acc)
            acc_to_gene[acc] = gene

    unique_accessions = sorted(set(all_accessions))

    def _fetch_target(acc):
        return acc, _uniprot_to_chembl_target(cache, acc)

    target_results = concurrent_map(
        _fetch_target, unique_accessions,
        max_workers=cache.max_workers, desc="UniProt→ChEMBL target",
    )

    acc_targets: dict[str, list[dict]] = {}  # accession → [target_dicts]
    for item in target_results:
        if item is None:
            continue
        acc, targets = item
        if targets:
            acc_targets[acc] = targets

    n_targets = sum(len(v) for v in acc_targets.values())
    logger.info("UniProt→ChEMBL: %d 个 UniProt → %d 个靶点", len(acc_targets), n_targets)

    # ── 4. ChEMBL target → Drug molecules ──
    all_target_ids = set()
    target_to_gene = {}  # target_chembl_id → gene_symbol
    target_to_acc = {}   # target_chembl_id → uniprot_accession
    for acc, targets in acc_targets.items():
        gene = acc_to_gene.get(acc, "")
        for t in targets:
            tid = t["target_chembl_id"]
            all_target_ids.add(tid)
            target_to_gene[tid] = gene
            target_to_acc[tid] = acc

    unique_targets = sorted(all_target_ids)

    def _fetch_drugs(tid):
        return tid, _target_to_drugs(cache, tid, max_phase=max_phase)

    drug_results = concurrent_map(
        _fetch_drugs, unique_targets,
        max_workers=cache.max_workers, desc="ChEMBL target→drugs",
    )

    # 收集所有药物-靶点关系
    drug_target_rows = []
    all_mol_ids = set()
    for item in drug_results:
        if item is None:
            continue
        tid, drugs = item
        gene = target_to_gene.get(tid, "")
        for d in drugs:
            mol_id = d["molecule_chembl_id"]
            all_mol_ids.add(mol_id)
            drug_target_rows.append({
                "molecule_chembl_id": mol_id,
                "target_chembl_id": tid,
                "mechanism_of_action": d.get("mechanism_of_action", ""),
                "signature_gene": gene,
                "gene_direction": gene_direction_map.get(gene, {}).get("direction", ""),
                "gene_weight": gene_direction_map.get(gene, {}).get("weight", 0),
            })

    logger.info("反查到 %d 个唯一分子 (来自 %d 个靶点)", len(all_mol_ids), len(unique_targets))

    # ── 5. 批量检查 max_phase, 过滤已批准药 ──
    def _check_phase(mol_id):
        return _check_molecule_phase(cache, mol_id)

    mol_info_list = concurrent_map(
        _check_phase, sorted(all_mol_ids),
        max_workers=cache.max_workers, desc="检查药物 max_phase",
    )

    mol_info = {}
    for info in mol_info_list:
        if info is not None and info.get("molecule_chembl_id"):
            mol_info[info["molecule_chembl_id"]] = info

    # 过滤
    def _safe_phase(v) -> int:
        try:
            return int(float(v)) if v else 0
        except (ValueError, TypeError):
            return 0

    approved_mols = {
        mid for mid, info in mol_info.items()
        if _safe_phase(info.get("max_phase")) >= max_phase
        and info.get("pref_name")  # 排除无名分子
    }

    logger.info("max_phase >= %d 的已批准药物: %d/%d",
                max_phase, len(approved_mols), len(all_mol_ids))

    # ── 6. 构建输出 DataFrames ──

    # (a) drug_from_signature.csv - 完整反查结果
    sig_rows = []
    for row in drug_target_rows:
        mol_id = row["molecule_chembl_id"]
        if mol_id not in approved_mols:
            continue
        info = mol_info.get(mol_id, {})
        pref_name = info.get("pref_name", "")
        sig_rows.append({
            "drug_raw": pref_name.lower(),
            "canonical_name": pref_name.lower(),
            "chembl_id": mol_id,
            "chembl_pref_name": pref_name,
            "target_chembl_id": row["target_chembl_id"],
            "mechanism_of_action": row["mechanism_of_action"],
            "signature_gene": row["signature_gene"],
            "gene_direction": row["gene_direction"],
            "gene_weight": row["gene_weight"],
            "max_phase": info.get("max_phase", 0),
            "first_approval": info.get("first_approval"),
        })

    sig_df = pd.DataFrame(sig_rows)
    if sig_df.empty:
        logger.warning("没有找到符合条件的已批准药物!")
        sig_df = pd.DataFrame(columns=[
            "drug_raw", "canonical_name", "chembl_id", "chembl_pref_name",
            "target_chembl_id", "mechanism_of_action", "signature_gene",
            "gene_direction", "gene_weight", "max_phase", "first_approval",
        ])

    sig_out = data_dir / "drug_from_signature.csv"
    sig_df.to_csv(sig_out, index=False)
    logger.info("drug_from_signature.csv: %d 行, %d 个唯一药物, %d 个唯一靶点",
                len(sig_df),
                sig_df["chembl_id"].nunique() if not sig_df.empty else 0,
                sig_df["target_chembl_id"].nunique() if not sig_df.empty else 0)

    # (b) drug_chembl_map.csv - 兼容 Step 4 格式
    if not sig_df.empty:
        chembl_map_df = sig_df[["drug_raw", "canonical_name", "chembl_id", "chembl_pref_name"]].drop_duplicates(
            subset=["canonical_name"]
        )
        # 加 rxnorm_term 列 (空值, signature模式不需要)
        chembl_map_df["rxnorm_term"] = ""
    else:
        chembl_map_df = pd.DataFrame(columns=["drug_raw", "canonical_name", "rxnorm_term", "chembl_id", "chembl_pref_name"])

    chembl_map_out = data_dir / "drug_chembl_map.csv"
    chembl_map_df.to_csv(chembl_map_out, index=False)

    # (c) edge_drug_target.csv - 兼容 Step 5 格式
    if not sig_df.empty:
        dt_df = sig_df[["canonical_name", "drug_raw", "chembl_id", "target_chembl_id", "mechanism_of_action"]].copy()
        dt_df = dt_df.rename(columns={
            "canonical_name": "drug_normalized",
            "chembl_id": "molecule_chembl_id",
        })
        dt_df = dt_df.dropna(subset=["drug_normalized", "target_chembl_id"]).drop_duplicates(
            subset=["drug_normalized", "molecule_chembl_id", "target_chembl_id"]
        )
    else:
        dt_df = pd.DataFrame(columns=["drug_normalized", "drug_raw", "molecule_chembl_id", "target_chembl_id", "mechanism_of_action"])

    dt_out = data_dir / "edge_drug_target.csv"
    dt_df.to_csv(dt_out, index=False)

    # (d) drug_canonical.csv - 兼容 Step 3 格式
    if not sig_df.empty:
        canon_df = sig_df[["drug_raw", "canonical_name"]].drop_duplicates()
        canon_df["rxnorm_rxcui"] = ""
    else:
        canon_df = pd.DataFrame(columns=["drug_raw", "canonical_name", "rxnorm_rxcui"])

    canon_out = data_dir / "drug_canonical.csv"
    canon_df.to_csv(canon_out, index=False)

    # (e) drug_rxnorm_map.csv - 占位文件 (signature模式不需要RxNorm)
    if not sig_df.empty:
        rx_df = sig_df[["drug_raw"]].drop_duplicates().copy()
        rx_df["rxnorm_rxcui"] = ""
        rx_df["rxnorm_term"] = ""
        rx_df["rxnorm_score"] = ""
    else:
        rx_df = pd.DataFrame(columns=["drug_raw", "rxnorm_rxcui", "rxnorm_term", "rxnorm_score"])

    rx_out = data_dir / "drug_rxnorm_map.csv"
    rx_df.to_csv(rx_out, index=False)

    # (f) failed_trials_drug_rows.csv - 空占位
    ft_df = pd.DataFrame(columns=[
        "nctId", "drug_raw", "drug_role", "overallStatus", "whyStopped",
        "conditions", "briefTitle",
    ])
    ft_out = data_dir / "failed_trials_drug_rows.csv"
    ft_df.to_csv(ft_out, index=False)

    # (g) failed_drugs_summary.csv - 空占位
    fs_df = pd.DataFrame(columns=[
        "drug_normalized", "n_trials", "trial_statuses", "trial_source",
        "example_condition", "example_whyStopped",
    ])
    fs_out = data_dir / "failed_drugs_summary.csv"
    fs_df.to_csv(fs_out, index=False)

    logger.info("=" * 50)
    logger.info("Signature 药物反查完成!")
    logger.info("  基因签名: %s", sig_path.name)
    logger.info("  输入基因数: %d", len(genes))
    logger.info("  成功映射UniProt: %d", n_mapped)
    logger.info("  ChEMBL靶点数: %d", len(unique_targets))
    logger.info("  已批准药物数: %d", len(approved_mols))
    logger.info("  药物-靶点边数: %d", len(dt_df))
    logger.info("=" * 50)

    return sig_out
