"""
DTPD 基础路径评分器: Drug → Target → Pathway → Disease

这是 ranker.py (最终排名器) 的内部构建块，负责计算机制路径分数。

路径类型:
  Drug --[HAS_TARGET]--> Target --[IN_PATHWAY]--> Pathway --[ASSOC_DISEASE]--> Disease

数据来源:
  - Drug-Target: ChEMBL mechanism
  - Target-Pathway: Reactome (via UniProt)
  - Pathway-Disease: OpenTargets (聚合)

评分逻辑:
  path_score = pathway_score * hub_penalty(target_degree)^lambda * (1 + boost * log(support_genes))
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config, ensure_dir
from ..utils import read_csv, require_cols, write_jsonl
from .base import hub_penalty


def run_dtpd(cfg: Config) -> dict[str, Path]:
    """
    运行 DTPD 基础路径评分

    Returns:
        输出文件路径字典
    """
    output_dir = ensure_dir(cfg.output_dir)
    data_dir = cfg.data_dir
    files = cfg.files
    rank_cfg = cfg.rank

    # 加载数据
    dt = read_csv(data_dir / files.get("drug_target", "edge_drug_target.csv"), dtype=str)
    tp = read_csv(data_dir / files.get("target_pathway", "edge_target_pathway_all.csv"), dtype=str)
    pd_edge = read_csv(data_dir / files.get("pathway_disease", "edge_pathway_disease.csv"), dtype=str)

    require_cols(dt, {"drug_normalized", "target_chembl_id"}, "edge_drug_target")
    require_cols(tp, {"target_chembl_id", "reactome_stid", "reactome_name"}, "edge_target_pathway")
    require_cols(pd_edge, {"reactome_stid", "diseaseId", "pathway_score", "support_genes"}, "edge_pathway_disease")

    pd_edge["pathway_score_f"] = pd.to_numeric(pd_edge["pathway_score"], errors="coerce").fillna(0.0)
    pd_edge["support_genes_f"] = pd.to_numeric(pd_edge["support_genes"], errors="coerce").fillna(1.0)

    # v3: Penalize overly broad pathways (e.g., "Signal Transduction" with 500+ genes).
    # A drug hitting ANY kinase in a mega-pathway gets an inflated score.
    # Discount factor: 1.0 for ≤50 genes, decays to ~0.5 for 200 genes, ~0.3 for 500.
    max_pathway_genes = float(rank_cfg.get("max_pathway_genes_soft", 50))
    pd_edge["_pathway_breadth_discount"] = np.where(
        pd_edge["support_genes_f"] <= max_pathway_genes,
        1.0,
        max_pathway_genes / pd_edge["support_genes_f"],
    )
    pd_edge["pathway_score_f"] = pd_edge["pathway_score_f"] * pd_edge["_pathway_breadth_discount"]

    # 合并路径
    # 只保留路径核心列, 避免 drug_raw/mechanism_of_action 等额外列造成假性重复
    dt_core = dt[["drug_normalized", "target_chembl_id"]].drop_duplicates()
    # tp 可能因多个 UniProt accession 导致 (target, pathway) 重复, 先去重
    if "reactome_name" not in tp.columns:
        tp["reactome_name"] = ""
    tp_core = tp[["target_chembl_id", "reactome_stid", "reactome_name"]].drop_duplicates(
        subset=["target_chembl_id", "reactome_stid"]
    )
    dtp = dt_core.merge(tp_core, on="target_chembl_id", how="inner").dropna(
        subset=["drug_normalized", "target_chembl_id", "reactome_stid"]
    ).drop_duplicates()

    # 计算靶点度数
    tdeg = dtp.groupby("target_chembl_id")["drug_normalized"].nunique().rename("target_deg")
    dtp = dtp.merge(tdeg, on="target_chembl_id", how="left")

    # Hub惩罚
    lam = float(rank_cfg.get("hub_penalty_lambda", 1.0))
    dtp["w_hub_target"] = hub_penalty(dtp["target_deg"]).pow(lam)

    # 合并通路-疾病
    # tp 和 pd_edge 都有 reactome_name 列, 用后缀区分后保留 tp 侧的
    paths = dtp.merge(pd_edge, on="reactome_stid", how="inner", suffixes=("", "_pd"))
    # 优先使用 tp 侧的 reactome_name, 缺失时回退到 pd_edge 侧
    if "reactome_name_pd" in paths.columns:
        paths["reactome_name"] = paths["reactome_name"].fillna(paths["reactome_name_pd"])
        paths.drop(columns=["reactome_name_pd"], inplace=True)

    # v3: Load drug-target affinity data if available (pChEMBL values)
    aff_path = data_dir / "edge_drug_target_affinity.csv"
    if aff_path.exists() and aff_path.stat().st_size > 1:
        import logging as _logging
        _dtpd_logger = _logging.getLogger(__name__)
        aff_df = pd.read_csv(aff_path, dtype=str)
        aff_df["pchembl_f"] = pd.to_numeric(aff_df.get("pchembl_value", pd.Series(dtype=float)),
                                              errors="coerce")
        # Affinity weight: pChEMBL 6→1.0 (baseline), 8→1.3, 10→1.6; <6→0.8
        aff_df["_affinity_weight"] = (1.0 + 0.15 * (aff_df["pchembl_f"] - 6.0)).clip(0.7, 1.8)
        aff_merge = aff_df[["drug_normalized", "target_chembl_id", "_affinity_weight"]].drop_duplicates(
            subset=["drug_normalized", "target_chembl_id"]
        )
        paths = paths.merge(aff_merge, on=["drug_normalized", "target_chembl_id"], how="left")
        paths["_affinity_weight"] = paths["_affinity_weight"].fillna(1.0)
        n_with_aff = (paths["_affinity_weight"] != 1.0).sum()
        _dtpd_logger.info("Affinity data applied to %d/%d paths", n_with_aff, len(paths))
    else:
        paths["_affinity_weight"] = 1.0

    # 计算路径分数 (v3: includes affinity weighting)
    sb = float(rank_cfg.get("support_gene_boost", 0.15))
    paths["w_support"] = 1.0 + sb * np.log1p(paths["support_genes_f"])
    paths["path_score"] = (
        paths["pathway_score_f"]
        * paths["w_hub_target"]
        * paths["w_support"]
        * paths["_affinity_weight"]
    )

    # 每对取top K路径
    paths["pair_key"] = paths["drug_normalized"].astype(str) + "||" + paths["diseaseId"].astype(str)
    k = int(rank_cfg.get("topk_paths_per_pair", 10))
    top_paths = paths.sort_values("path_score", ascending=False).groupby("pair_key", as_index=False).head(k).copy()

    # v3 aggregation: max(path_score) + diversity_bonus * log(n_paths)
    #
    # Rationale: The old rank-weighted sum (1/sqrt(rank)) systematically
    # under-scored multi-target drugs.  Example:
    #   Drug A: 1 path  score=1.0  → final = 1.00
    #   Drug B: 10 paths score=0.8 → final ≈ 0.45  (should be HIGHER)
    # Multi-target drugs hitting the same disease through independent pathways
    # are more robust candidates.  The new formula:
    #   mechanism_score = max(path_score) + diversity_bonus * log1p(n_paths - 1)
    # This keeps the strongest single path as the baseline and rewards pathway
    # diversity logarithmically (diminishing returns after ~5 paths).
    diversity_bonus = float(rank_cfg.get("path_diversity_bonus", 0.10))
    top_paths = top_paths.sort_values(["pair_key", "path_score"], ascending=[True, False])

    pair = top_paths.groupby(["drug_normalized", "diseaseId"], as_index=False).agg(
        _max_score=("path_score", "max"),
        _n_paths=("path_score", "count"),
        _unique_targets=("target_chembl_id", "nunique"),
        diseaseName=("diseaseName", lambda x: x.dropna().iloc[0] if len(x.dropna()) else ""),
    )
    # Diversity bonus: log1p(n_paths-1) so 1 path → 0 bonus, 2→0.69*b, 5→1.6*b, 10→2.2*b
    pair["mechanism_score"] = (
        pair["_max_score"]
        + diversity_bonus * np.log1p(pair["_n_paths"] - 1)
        # Extra bonus for hitting disease through independent targets (not just pathways)
        + diversity_bonus * 0.5 * np.log1p(pair["_unique_targets"] - 1)
    )
    pair.drop(columns=["_max_score", "_n_paths", "_unique_targets"], inplace=True)
    # Normalize combo drug scores: "drug_a+drug_b+drug_c" has 3x more targets,
    # so divide mechanism_score by component count to keep scores comparable.
    pair["n_components"] = pair["drug_normalized"].str.count(r"\+") + 1
    pair["mechanism_score"] = pair["mechanism_score"] / pair["n_components"]
    pair["final_score"] = pair["mechanism_score"]
    pair.drop(columns=["n_components"], inplace=True)
    pair = pair.sort_values(["drug_normalized", "final_score"], ascending=[True, False])

    # 输出
    out_csv = output_dir / "dtpd_rank.csv"
    pair.to_csv(out_csv, index=False)

    # 证据路径
    ev_path = output_dir / "dtpd_paths.jsonl"
    write_jsonl(ev_path, [
        {
            "drug": r["drug_normalized"],
            "diseaseId": r["diseaseId"],
            "diseaseName": r.get("diseaseName", ""),
            "path_score": float(r["path_score"]),
            "nodes": [
                {"type": "Drug", "id": r["drug_normalized"]},
                {"type": "Target", "id": r["target_chembl_id"]},
                {"type": "Pathway", "id": r["reactome_stid"], "name": r.get("reactome_name", "")},
                {"type": "Disease", "id": r["diseaseId"], "name": r.get("diseaseName", "")},
            ],
            "edges": [
                {"rel": "DRUG_HAS_TARGET", "src": r["drug_normalized"], "dst": r["target_chembl_id"], "source": "ChEMBL"},
                {"rel": "TARGET_IN_PATHWAY", "src": r["target_chembl_id"], "dst": r["reactome_stid"], "source": "Reactome"},
                {"rel": "PATHWAY_ASSOC_DISEASE", "src": r["reactome_stid"], "dst": r["diseaseId"], "source": "OpenTargets(agg)",
                 "pathway_score": float(r.get("pathway_score_f", 0)), "support_genes": int(float(r["support_genes_f"]))},
            ],
        }
        for _, r in top_paths.iterrows()
    ])

    return {"rank_csv": out_csv, "evidence_paths": ev_path}
