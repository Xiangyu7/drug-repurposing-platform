"""
Drug Repurposing Ranker — 完整可解释路径排名

在 DTPD 基础路径分数 (dtpd.py) 之上叠加:
  1. FAERS 安全信号惩罚
  2. 失败试验惩罚
  3. 疾病表型加成
  4. Bootstrap CI 不确定性量化

评分公式:
  final_score = mechanism_score (from dtpd)
                * exp(-w1 * safety_penalty - w2 * trial_penalty)
                * (1 + w3 * log1p(n_phenotype))

输出:
  - drug_disease_rank.csv: 排序结果
  - evidence_paths.jsonl: 所有证据
  - evidence_pack/: 每对的完整证据包 JSON
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from ..config import Config, ensure_dir
from ..utils import read_csv, write_jsonl, safe_str
from ..cache import HTTPCache
from .dtpd import run_dtpd

import logging

logger = logging.getLogger(__name__)


def _is_serious_ae(ae_term: str, serious_keywords: list[str]) -> bool:
    """判断是否为严重不良事件"""
    ae_lower = ae_term.lower()
    return any(kw.lower() in ae_lower for kw in serious_keywords)


def _build_drug_target_map(data_dir: Path) -> dict[str, list[dict]]:
    """Build drug → target list mapping from KG data files.

    Returns:
        Dict mapping drug_normalized → list of target dicts, each containing:
            target_chembl_id, target_name, mechanism_of_action, uniprot,
            pdb_ids (experimental), has_alphafold, structure_source
    """
    dt_path = data_dir / "edge_drug_target.csv"
    nt_path = data_dir / "node_target.csv"
    xref_path = data_dir / "target_xref.csv"

    if not dt_path.exists():
        logger.warning("edge_drug_target.csv not found, skipping target enrichment")
        return {}

    dt = pd.read_csv(dt_path, dtype=str).fillna("")

    # target_chembl_id → pref_name
    target_names: dict[str, str] = {}
    if nt_path.exists():
        nt = pd.read_csv(nt_path, dtype=str).fillna("")
        for _, r in nt.iterrows():
            tid = str(r.get("target_chembl_id", "")).strip()
            pname = str(r.get("pref_name", "")).strip()
            if tid and pname:
                target_names[tid] = pname

    # Parse xref for UniProt, PDB, and AlphaFold
    target_uniprot: dict[str, str] = {}
    target_pdb: dict[str, list[str]] = {}  # target → list of PDB IDs
    target_alphafold: dict[str, bool] = {}  # target → has AlphaFold
    if xref_path.exists():
        xref = pd.read_csv(xref_path, dtype=str).fillna("")
        uni_seen: set = set()
        for _, r in xref.iterrows():
            tid = str(r.get("target_chembl_id", "")).strip()
            src = str(r.get("xref_src_db", "")).strip()
            xid = str(r.get("xref_id", "")).strip()
            uni = str(r.get("uniprot_accession", "")).strip()
            if not tid:
                continue
            if uni and tid not in uni_seen:
                target_uniprot[tid] = uni
                uni_seen.add(tid)
            if src == "PDB" and xid:
                target_pdb.setdefault(tid, []).append(xid)
            if src == "AlphaFoldDB":
                target_alphafold[tid] = True

    # Build per-drug target list
    drug_targets: dict[str, list[dict]] = {}
    for _, r in dt.iterrows():
        drug = str(r.get("drug_normalized", "")).strip()
        tid = str(r.get("target_chembl_id", "")).strip()
        moa = str(r.get("mechanism_of_action", "")).strip()
        if not drug or not tid:
            continue
        tname = target_names.get(tid, "")
        uniprot = target_uniprot.get(tid, "")
        pdb_ids = target_pdb.get(tid, [])
        has_af = target_alphafold.get(tid, False)

        # Determine structure source tag
        if pdb_ids and has_af:
            structure_source = "PDB+AlphaFold"
        elif pdb_ids:
            structure_source = "PDB"
        elif has_af:
            structure_source = "AlphaFold_only"
        else:
            structure_source = "none"

        entry = {
            "target_chembl_id": tid,
            "target_name": tname,
            "mechanism_of_action": moa,
            "uniprot": uniprot,
            "pdb_ids": pdb_ids[:5],  # top 5 PDB IDs (can be many)
            "pdb_count": len(pdb_ids),
            "has_alphafold": has_af,
            "structure_source": structure_source,
        }
        drug_targets.setdefault(drug, []).append(entry)

    logger.info("Drug-target map: %d drugs with targets", len(drug_targets))
    return drug_targets


def _format_target_summary(targets: list[dict]) -> str:
    """Render target_details into a concise human-readable summary string."""
    parts: list[str] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        s = f"{safe_str(t.get('target_name'))} ({safe_str(t.get('target_chembl_id'))})"
        uni = safe_str(t.get("uniprot"))
        if uni:
            s += f" [UniProt:{uni}]"
        src = safe_str(t.get("structure_source")) or "none"
        s += f" [{src}]"
        moa = safe_str(t.get("mechanism_of_action"))
        if moa:
            s += f" — {moa}"
        parts.append(s)
    return "; ".join(parts)


def _generate_path_explanation(path_row) -> str:
    """生成路径的自然语言解释"""
    nodes = path_row.get("nodes", [])
    if len(nodes) < 4:
        return ""

    drug = nodes[0].get("id", "") if len(nodes) > 0 else ""
    target = nodes[1].get("id", "") if len(nodes) > 1 else ""
    pathway = nodes[2].get("name", "") or nodes[2].get("id", "") if len(nodes) > 2 else ""
    disease = nodes[3].get("name", "") or nodes[3].get("id", "") if len(nodes) > 3 else ""

    return (
        f"{drug} targets {target}, which participates in the {pathway} pathway. "
        f"This pathway is associated with {disease}."
    )


def run_ranker(cfg: Config) -> dict[str, Path]:
    """
    运行排名: DTPD 基础分 + FAERS 安全 + 表型加成 + Bootstrap CI

    Returns:
        输出文件路径字典
    """
    # 先运行V3获取基础路径
    run_dtpd(cfg)

    output_dir = ensure_dir(cfg.output_dir)
    data_dir = cfg.data_dir
    rank_cfg = cfg.rank

    # 加载配置参数 (范围校验: 权重必须在 [0, 1])
    safety_penalty_w = max(0.0, min(1.0, float(rank_cfg.get("safety_penalty_weight", 0.3))))
    trial_penalty_w = max(0.0, min(1.0, float(rank_cfg.get("trial_failure_penalty", 0.2))))
    phenotype_boost_w = max(0.0, min(1.0, float(rank_cfg.get("phenotype_overlap_boost", 0.1))))
    phenotype_cap = int(cfg.phenotype.get("max_phenotypes_per_disease", 10))
    serious_ae_kw = cfg.serious_ae_keywords
    min_prr = float(cfg.faers.get("min_prr", 0))

    # 加载V3结果
    pair_dtpd = read_csv(output_dir / "dtpd_rank.csv", dtype=str)
    ev_jsonl = output_dir / "dtpd_paths.jsonl"
    if ev_jsonl.exists() and ev_jsonl.stat().st_size > 0:
        paths_dtpd = pd.read_json(ev_jsonl, lines=True)
    else:
        paths_dtpd = pd.DataFrame(columns=["drug", "diseaseId", "path_score", "nodes", "edges"])

    # 加载FAERS数据 (可选)
    ae_df = None
    has_prr = False
    ae_path = data_dir / "edge_drug_ae_faers.csv"
    if ae_path.exists():
        ae_df = read_csv(ae_path, dtype=str)
        ae_df["report_count"] = pd.to_numeric(ae_df["report_count"], errors="coerce").fillna(0)
        if "prr" in ae_df.columns:
            ae_df["prr"] = pd.to_numeric(ae_df["prr"], errors="coerce").fillna(0.0)
            has_prr = True

    # 加载表型数据 (可选)
    phe_df = None
    phe_path = data_dir / "edge_disease_phenotype.csv"
    if phe_path.exists() and phe_path.stat().st_size > 1:
        phe_df = read_csv(phe_path, dtype=str)

    # 加载试验AE数据 (可选)
    trial_ae_df = None
    trial_path = data_dir / "edge_trial_ae.csv"
    if trial_path.exists() and trial_path.stat().st_size > 1:
        trial_ae_df = read_csv(trial_path, dtype=str)

    def calc_safety_penalty(drug: str) -> tuple[float, list[dict]]:
        """计算安全惩罚 (PRR-driven, report_count 仅做置信度加权)

        v2 rationale: Old formula used log1p(report_count) as the penalty
        base, which over-penalised well-studied drugs (aspirin, metformin)
        simply because they had more FAERS exposure years.  PRR already
        normalises for background reporting rate, so it should drive the
        penalty magnitude.  report_count is kept as a *confidence* weight:
        a PRR=5 signal from 3 reports is less trustworthy than from 300.
        """
        if ae_df is None:
            return 0.0, []
        # v2 FIX: lowercase the drug name for FAERS lookup (ae_df stores lowercased names)
        drug_aes = ae_df[ae_df["drug_normalized"] == str(drug).lower().strip()]
        if drug_aes.empty:
            return 0.0, []

        penalty = 0.0
        ae_evidence = []
        # Sort by PRR DESC (strongest signals first) for top-10 selection
        sort_col = "prr" if has_prr else "report_count"
        for _, ae in drug_aes.sort_values(sort_col, ascending=False).head(10).iterrows():
            term = ae.get("ae_term", "")
            count = float(ae.get("report_count", 0))
            prr = float(ae.get("prr", 0)) if has_prr else 0.0
            is_serious = _is_serious_ae(term, serious_ae_kw)

            # PRR 信号门槛: 低于阈值的不视为真实信号，跳过
            if has_prr and min_prr > 0 and prr < min_prr:
                continue

            if has_prr and prr > 0:
                # PRR-driven penalty:
                #   log1p(PRR) / 5  maps  PRR=2 → 0.22,  PRR=10 → 0.48,  PRR=50 → 0.78
                # Confidence weight: saturates quickly so count doesn't dominate
                #   min(1.0, log1p(count) / log1p(100))  →  count=3: 0.30, count=100: 1.0
                confidence = min(1.0, np.log1p(count) / np.log1p(100))
                ae_penalty = (np.log1p(prr) / 5.0) * confidence
            else:
                # Fallback when PRR is unavailable: mild count-based penalty
                ae_penalty = np.log1p(count) / 20.0

            if is_serious:
                ae_penalty *= 2.0
            penalty += ae_penalty

            ae_evidence.append({
                "ae_term": term,
                "report_count": int(count),
                "prr": round(prr, 4),
                "is_serious": is_serious,
            })

        # v2 FIX: Use CUMULATIVE penalty with saturation, NOT averaging.
        # Rationale: averaging masked multi-toxicity profiles — a drug with
        # 10 serious AEs was penalized the same as one with 1 AE of identical PRR.
        # Now: cumulative sum, capped at 1.0 via tanh for smooth saturation.
        # tanh maps: penalty=0.5→0.46, 1.0→0.76, 2.0→0.96, 3.0→0.995
        return min(float(np.tanh(penalty)), 1.0), ae_evidence

    # Target disease condition for filtering disease-relevant efficacy stops
    target_condition = cfg.condition.lower().strip()

    def _trial_condition_matches(conditions_str: str) -> bool:
        """Check if any trial condition is relevant to the target disease.

        v2: Uses word-boundary matching instead of bi-directional substring.
        Old bi-directional substring was too permissive:
        - "heart" matched any cardiac condition
        - "failure" matched any trial with "failure" in its name
        Now: target_condition must appear as a whole-word match within the
        trial condition string, or vice versa for multi-word conditions.
        """
        import re
        if not conditions_str:
            return False
        # Build regex pattern with word boundaries
        target_pattern = re.compile(r'\b' + re.escape(target_condition) + r'\b', re.IGNORECASE)
        for cond in conditions_str.split("|"):
            cond_lower = cond.strip().lower()
            if not cond_lower:
                continue
            # Target condition found as whole word(s) in trial condition
            if target_pattern.search(cond_lower):
                return True
            # Trial condition found as whole word(s) in target condition
            # (for cases like trial="heart failure", target="congestive heart failure")
            cond_pattern = re.compile(r'\b' + re.escape(cond_lower) + r'\b', re.IGNORECASE)
            if len(cond_lower) >= 5 and cond_pattern.search(target_condition):
                return True
        return False

    def calc_trial_penalty(drug: str) -> tuple[float, list[dict]]:
        """计算试验失败惩罚 (v2: efficacy stops 仅计入目标疾病相关)

        Rationale: drug repurposing 的意义就是在新适应症上试，一个药在
        适应症 A 上 efficacy failure 不应该惩罚它在适应症 B 上的得分。
        但 safety stops 是跨适应症通用的 (肝毒性在任何适应症都是风险)。
        """
        if trial_ae_df is None:
            return 0.0, []
        drug_trials = trial_ae_df[trial_ae_df["drug_normalized"] == drug.lower().strip()]
        if drug_trials.empty:
            return 0.0, []

        # Safety stops: count ALL (safety risk is indication-agnostic)
        safety_stops = len(drug_trials[drug_trials["is_safety_stop"].astype(str) == "1"])

        # Efficacy stops: only count trials whose condition matches target disease
        has_conditions = "conditions" in drug_trials.columns
        if has_conditions:
            eff_mask = drug_trials["is_efficacy_stop"].astype(str) == "1"
            relevant_mask = drug_trials["conditions"].fillna("").apply(_trial_condition_matches)
            efficacy_stops = int((eff_mask & relevant_mask).sum())
            efficacy_stops_skipped = int(eff_mask.sum()) - efficacy_stops
        else:
            # Fallback: no conditions column → count all (backward compat)
            efficacy_stops = len(drug_trials[drug_trials["is_efficacy_stop"].astype(str) == "1"])
            efficacy_stops_skipped = 0

        # v2: Use log-saturating penalty to avoid over-penalizing well-studied drugs.
        # Old formula (0.1 * safety + 0.05 * efficacy) was linear — drugs with many
        # historical trials (aspirin, metformin) accumulated penalty proportional to
        # their data volume, not their actual risk.
        # log1p saturation: 1 stop→0.069, 3 stops→0.139, 10 stops→0.240
        penalty = 0.1 * np.log1p(safety_stops) + 0.05 * np.log1p(efficacy_stops)

        trial_evidence = []
        for _, t in drug_trials.head(5).iterrows():
            trial_evidence.append({
                "nctId": t.get("nctId", ""),
                "status": t.get("overallStatus", ""),
                "whyStopped": t.get("whyStopped", ""),
                "conditions": t.get("conditions", ""),
                "is_safety_stop": str(t.get("is_safety_stop", "0")) == "1",
                "is_efficacy_stop": str(t.get("is_efficacy_stop", "0")) == "1",
                "efficacy_relevant": _trial_condition_matches(str(t.get("conditions", ""))) if has_conditions else True,
            })

        if efficacy_stops_skipped > 0:
            logger.debug("Drug %s: skipped %d off-target efficacy stops (target=%s)",
                         drug, efficacy_stops_skipped, target_condition)

        return min(penalty, 1.0), trial_evidence

    def get_phenotypes(disease_id: str) -> list[dict]:
        """获取疾病表型"""
        if phe_df is None:
            return []
        disease_phes = phe_df[phe_df["diseaseId"] == disease_id]
        return [
            {"id": p.get("phenotypeId", ""), "name": p.get("phenotypeName", ""), "score": float(p.get("score", 0))}
            for _, p in disease_phes.head(phenotype_cap).iterrows()
        ]

    # ===== Pass 1: compute scores for ALL pairs =====
    final_rows = []

    # Cache penalties per drug to avoid redundant computation
    _safety_cache: dict[str, tuple[float, list[dict]]] = {}
    _trial_cache: dict[str, tuple[float, list[dict]]] = {}
    _pheno_cache: dict[str, list[dict]] = {}

    for _, pr in tqdm(pair_dtpd.iterrows(), total=len(pair_dtpd), desc="Ranking"):
        drug = safe_str(pr.get("drug_normalized"))
        disease_id = safe_str(pr.get("diseaseId"))
        disease_name = safe_str(pr.get("diseaseName"))
        base_score = float(pd.to_numeric(pr.get("final_score", 0), errors="coerce") or 0)

        # Cached penalty lookups
        if drug not in _safety_cache:
            _safety_cache[drug] = calc_safety_penalty(drug)
        if drug not in _trial_cache:
            _trial_cache[drug] = calc_trial_penalty(drug)
        if disease_id not in _pheno_cache:
            _pheno_cache[disease_id] = get_phenotypes(disease_id)

        safety_pen = _safety_cache[drug][0]
        trial_pen = _trial_cache[drug][0]
        phenotypes = _pheno_cache[disease_id]

        # Risk decay keeps score positive and monotonic w.r.t. penalties.
        risk_multiplier = np.exp(-safety_penalty_w * safety_pen - trial_penalty_w * trial_pen)

        # v2 phenotype boost: use AVERAGE phenotype score (quality), not just count.
        # Old formula rewarded diseases with many phenotypes regardless of relevance.
        # New formula: boost = weight * mean(phenotype_scores) * log1p(n_pheno)
        # This rewards both having many phenotypes AND high-quality associations.
        if phenotypes:
            n_pheno = min(len(phenotypes), phenotype_cap)
            pheno_scores = [float(p.get("score", 0)) for p in phenotypes[:n_pheno]]
            avg_pheno_score = sum(pheno_scores) / max(len(pheno_scores), 1)
            phenotype_boost = phenotype_boost_w * avg_pheno_score * np.log1p(n_pheno)
        else:
            phenotype_boost = 0.0
        phenotype_multiplier = 1.0 + phenotype_boost
        final_score = base_score * risk_multiplier * phenotype_multiplier

        final_rows.append({
            "drug_normalized": drug,
            "diseaseId": disease_id,
            "diseaseName": disease_name,
            "mechanism_score": base_score,
            "safety_penalty": round(safety_pen, 4),
            "trial_penalty": round(trial_pen, 4),
            "risk_multiplier": round(float(risk_multiplier), 4),
            "phenotype_boost": round(float(phenotype_boost), 4),
            "phenotype_multiplier": round(float(phenotype_multiplier), 4),
            "final_score": round(final_score, 4),
        })

    # ===== Top-K filtering =====
    final_df = pd.DataFrame(final_rows)
    final_df = final_df.sort_values(["drug_normalized", "final_score"], ascending=[True, False])
    topk = int(rank_cfg.get("topk_pairs_per_drug", 50))
    final_df = final_df.groupby("drug_normalized", as_index=False).head(topk)

    # ===== Join trial info for user context =====
    summ_path = data_dir / "failed_drugs_summary.csv"
    if summ_path.exists() and summ_path.stat().st_size > 1:
        summ = read_csv(summ_path, dtype=str)
        trial_cols = []
        for col in ["n_trials", "trial_statuses", "trial_source", "example_condition", "example_whyStopped"]:
            if col in summ.columns:
                trial_cols.append(col)
        if trial_cols and "drug_normalized" in summ.columns:
            final_df = final_df.merge(
                summ[["drug_normalized"] + trial_cols],
                on="drug_normalized", how="left",
            )

    # ===== Tag known indications (from drug_from_signature.csv or ChEMBL indications) =====
    ind_path = data_dir / "drug_known_indications.csv"
    if ind_path.exists() and ind_path.stat().st_size > 1:
        ind_df = read_csv(ind_path, dtype=str)
        # Build a set of (drug, efo_id) pairs that are known indications
        # Normalize efo_id: ChEMBL uses "EFO:0000616", OpenTargets uses "EFO_0000616"
        # Store both formats for matching
        known_pairs = set()
        drug_indications = {}  # drug → list of indication names
        for _, r in ind_df.iterrows():
            mol_id = safe_str(r.get("molecule_chembl_id"))
            efo_id = safe_str(r.get("efo_id"))
            indication = safe_str(r.get("indication"))
            if efo_id:
                # Add both colon and underscore formats for matching
                known_pairs.add((mol_id, efo_id))
                known_pairs.add((mol_id, efo_id.replace(":", "_")))
                known_pairs.add((mol_id, efo_id.replace("_", ":")))
            if mol_id not in drug_indications:
                drug_indications[mol_id] = []
            if indication:
                drug_indications[mol_id].append(indication)

        # Map drug_normalized → molecule_chembl_id
        chembl_map_path = data_dir / "drug_chembl_map.csv"
        drug_to_mol = {}
        if chembl_map_path.exists():
            cm = read_csv(chembl_map_path, dtype=str)
            for _, r in cm.iterrows():
                canon = safe_str(r.get("canonical_name"))
                mol = safe_str(r.get("chembl_id"))
                if canon and mol:
                    drug_to_mol[canon] = mol

        is_known = []
        orig_indication = []
        for _, r in final_df.iterrows():
            drug = safe_str(r.get("drug_normalized"))
            disease_id = safe_str(r.get("diseaseId"))
            mol_id = drug_to_mol.get(drug, "")

            # Check if this disease is a known indication for this drug
            if mol_id and (mol_id, disease_id) in known_pairs:
                is_known.append(True)
            else:
                is_known.append(False)

            # Get all known indications for this drug
            inds = drug_indications.get(mol_id, [])
            orig_indication.append("; ".join(sorted(set(inds))[:5]) if inds else "")

        final_df["is_known_indication"] = is_known
        final_df["original_indications"] = orig_indication
        n_known = sum(is_known)
        logger.info("已知适应症标记: %d/%d 对为已知适应症", n_known, len(final_df))
    else:
        # No indication data available, add empty columns
        final_df["is_known_indication"] = False
        final_df["original_indications"] = ""

    # ===== Join signature source info if available =====
    sig_path = data_dir / "drug_from_signature.csv"
    if sig_path.exists() and sig_path.stat().st_size > 1:
        sig_df = read_csv(sig_path, dtype=str)
        if not sig_df.empty and "signature_gene" in sig_df.columns:
            # Aggregate: per drug, list top signature genes
            sig_agg = sig_df.groupby("canonical_name", as_index=False).agg(
                signature_genes=("signature_gene", lambda x: "; ".join(sorted(set(x.dropna()))[:5])),
                n_signature_targets=("target_chembl_id", "nunique"),
            ).rename(columns={"canonical_name": "drug_normalized"})
            final_df = final_df.merge(sig_agg, on="drug_normalized", how="left")

    out_csv = output_dir / "drug_disease_rank.csv"
    final_df.to_csv(out_csv, index=False)

    # ===== G1: Add uncertainty quantification (Bootstrap CI) =====
    try:
        from .uncertainty import add_uncertainty_to_ranking
        ev_records = paths_dtpd[["drug", "diseaseId", "path_score"]].to_dict("records")
        final_df = add_uncertainty_to_ranking(final_df, ev_records)
        final_df.to_csv(out_csv, index=False)
        logger.info("Uncertainty quantification added: %d pairs", len(final_df))
    except Exception as e:
        logger.warning("Uncertainty quantification skipped: %s", e)

    # ===== Pass 2: build evidence packs ONLY for top-K pairs =====
    surviving_pairs = set(
        final_df["drug_normalized"].astype(str) + "||" + final_df["diseaseId"].astype(str)
    )

    evidence_packs = []
    for _, pr in tqdm(final_df.iterrows(), total=len(final_df), desc="Evidence packs"):
        drug = safe_str(pr.get("drug_normalized"))
        disease_id = safe_str(pr.get("diseaseId"))
        disease_name = safe_str(pr.get("diseaseName"))
        base_score = float(pr.get("mechanism_score", 0))
        final_score = float(pr.get("final_score", 0))
        safety_pen = float(pr.get("safety_penalty", 0))
        trial_pen = float(pr.get("trial_penalty", 0))

        ae_evidence = _safety_cache.get(drug, (0.0, []))[1]
        trial_evidence = _trial_cache.get(drug, (0.0, []))[1]
        phenotypes = _pheno_cache.get(disease_id, [])

        sub_paths = paths_dtpd[(paths_dtpd["drug"] == drug) & (paths_dtpd["diseaseId"] == disease_id)]

        pack = {
            "drug": drug,
            "disease": {"id": disease_id, "name": disease_name},
            "scores": {
                "final": round(final_score, 4),
                "mechanism": round(base_score, 4),
                "safety_penalty": round(safety_pen, 4),
                "trial_penalty": round(trial_pen, 4),
            },
            "explainable_paths": [],
            "safety_signals": ae_evidence,
            "trial_evidence": trial_evidence,
            "phenotypes": phenotypes,
        }

        for _, row in sub_paths.head(int(rank_cfg.get("topk_paths_per_pair", 10))).iterrows():
            pack["explainable_paths"].append({
                "type": "DTPD",
                "path_score": float(row.get("path_score", 0)),
                "nodes": row.get("nodes", []),
                "edges": row.get("edges", []),
                "explanation": _generate_path_explanation(row),
            })

        evidence_packs.append(pack)

    ev_path = output_dir / "evidence_paths.jsonl"
    write_jsonl(ev_path, evidence_packs)

    ep_dir = ensure_dir(output_dir / "evidence_pack")
    # Clear stale packs from previous runs to keep directory in sync with current ranking.
    for old_pack in ep_dir.glob("*.json"):
        try:
            old_pack.unlink()
        except OSError as e:
            logger.warning("无法删除旧 evidence pack: %s (%s)", old_pack.name, e)
    for pack in evidence_packs:
        safe = (pack["drug"] + "__" + pack["disease"]["id"]).replace("/", "_").replace(":", "_")
        (ep_dir / f"{safe}.json").write_text(
            json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ===== Generate bridge CSV for LLM+RAG =====
    def _stable_drug_id(name: str) -> str:
        """Same algorithm as LLM+RAG step5: D + md5[:10].upper()"""
        return "D" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10].upper()

    # Per-drug summary: best disease, max score
    drug_best = final_df.sort_values("final_score", ascending=False).groupby(
        "drug_normalized", as_index=False
    ).first()

    bridge_rows = []
    # Load chembl_pref_name mapping (better for PubMed queries)
    chembl_path = data_dir / "drug_chembl_map.csv"
    chembl_map = {}
    if chembl_path.exists():
        cm = read_csv(chembl_path, dtype=str)
        for _, r in cm.iterrows():
            canon = safe_str(r.get("canonical_name"))
            pref = safe_str(r.get("chembl_pref_name"))
            if canon and pref:
                chembl_map[canon] = pref

    # Load drug → target mapping for molecular docking readiness
    drug_target_map = _build_drug_target_map(data_dir)

    for _, r in drug_best.iterrows():
        drug = safe_str(r.get("drug_normalized"))
        targets = drug_target_map.get(drug, [])
        bridge_rows.append({
            "drug_id": _stable_drug_id(drug),
            "canonical_name": drug,
            "chembl_pref_name": chembl_map.get(drug, ""),
            "max_mechanism_score": round(float(r.get("mechanism_score", 0)), 4),
            "top_disease": safe_str(r.get("diseaseName")),
            "final_score": round(float(r.get("final_score", 0)), 4),
            "n_trials": r.get("n_trials", ""),
            "trial_statuses": r.get("trial_statuses", ""),
            "trial_source": r.get("trial_source", ""),
            "example_condition": r.get("example_condition", ""),
            "why_stopped": r.get("example_whyStopped", ""),
            "targets": _format_target_summary(targets) if targets else "",
            "target_details": json.dumps(targets, ensure_ascii=False) if targets else "",
        })

    bridge_df = pd.DataFrame(bridge_rows).sort_values("max_mechanism_score", ascending=False)
    bridge_path = output_dir / "bridge_repurpose_cross.csv"
    bridge_df.to_csv(bridge_path, index=False)

    return {
        "rank_csv": out_csv,
        "evidence_paths": ev_path,
        "evidence_pack_dir": ep_dir,
        "bridge_csv": bridge_path,
    }
