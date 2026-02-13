#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step8 | Candidate Pack from Step7

Reads:
  - --step7_dir/step7_cards.json   (必需)
  - --neg poolA_negative_drug_level.csv (推荐)

Writes:
  - <outdir>/step8_shortlist_topK.csv
  - <outdir>/step8_candidate_pack_from_step7.xlsx
  - <outdir>/step8_one_pagers_topK.md
  - <outdir>/step8_manifest.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dr.common.provenance import build_manifest, write_manifest
from src.dr.contracts import (
    STEP7_CARDS_SCHEMA,
    STEP7_CARDS_VERSION,
    STEP8_SHORTLIST_SCHEMA,
    STEP8_SHORTLIST_VERSION,
    validate_step7_cards,
    validate_step8_shortlist_columns,
)
from src.dr.contracts_enforcer import ContractEnforcer
from src.dr.scoring.release_gate import ReleaseGate, ReleaseGateConfig

STOP_WORDS = {
    "tablet","tablets","capsule","capsules","injection","injectable","infusion","oral",
    "iv","intravenous","sc","subcutaneous","im","intramuscular","po",
    "qd","bid","tid","qod","qhs",
    "sustained","extended","release","er","sr","xr",
    "solution","suspension","gel","cream","patch","spray","drops","drop",
    "mg","g","mcg","ug","iu","ml",
}

def normalize_basic(x: str) -> str:
    s = str(x).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canonicalize_name(x: str) -> str:
    s = normalize_basic(x)
    if not s:
        return ""
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)
    toks = [t for t in re.split(r"\s+", s) if t]
    toks = [t for t in toks if t not in STOP_WORDS]
    joined = " ".join(toks).replace("α","alpha").replace("β","beta")
    return re.sub(r"\s+", " ", joined).strip()

def safe_sheet_name(s: str, used: Set[str]) -> str:
    s = re.sub(r"[\[\]\*:/\\\?]", "_", str(s)).strip() or "candidate"
    s = s[:31]
    base = s
    i = 2
    while s in used:
        suffix = f"_{i}"
        s = (base[:31-len(suffix)] + suffix)
        i += 1
    used.add(s)
    return s

def resolve_path(base_dir: str, p: str) -> str:
    p = str(p or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    # Try relative to cwd first (step7 cards store paths relative to project root)
    cwd_resolved = os.path.abspath(p)
    if os.path.exists(cwd_resolved):
        return cwd_resolved
    # Fallback: relative to base_dir
    return os.path.join(base_dir, p)

def read_json(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def extract_pmids(text: str) -> List[str]:
    return re.findall(r"\b\d{6,9}\b", str(text or ""))

def dossier_metrics(dossier: Dict[str, Any]) -> Dict[str, Any]:
    llm = (dossier or {}).get("llm_structured") or {}
    se = llm.get("supporting_evidence") or []
    hn = llm.get("harm_or_neutral_evidence") or []
    se_pmids = [p for e in se for p in extract_pmids(e.get("pmid",""))]
    hn_pmids = [p for e in hn for p in extract_pmids(e.get("pmid",""))]
    uniq_se = sorted(set(se_pmids))
    uniq_hn = sorted(set(hn_pmids))

    qc = llm.get("qc_summary") or {}
    topic_ratio = qc.get("topic_match_ratio", (dossier or {}).get("topic_match_ratio", None))
    try:
        topic_ratio = float(topic_ratio) if topic_ratio is not None else None
    except Exception:
        topic_ratio = None

    return {
        "supporting_sentence_count": len(se),
        "unique_supporting_pmids_count": len(uniq_se),
        "unique_supporting_pmids": uniq_se,
        "harm_or_neutral_sentence_count": len(hn),
        "unique_harm_pmids_count": len(uniq_hn),
        "unique_harm_pmids": uniq_hn,
        "topic_match_ratio": topic_ratio,
    }

def top_evidence_lines(items: List[Dict[str, Any]], n: int = 10) -> List[str]:
    out = []
    for e in (items or [])[:n]:
        pmid = ";".join(extract_pmids(e.get("pmid",""))) or ""
        claim = str(e.get("claim","")).strip().replace("\n"," ")
        if claim:
            out.append(f"PMID:{pmid} | {claim[:220]}")
    return out


def _handle_contract_issues(issues: List[str], strict_contract: bool, context: str) -> None:
    if not issues:
        return
    message = f"{context}: {issues}"
    if strict_contract:
        raise ValueError(message)
    print(f"[WARN] {message}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step7_dir", default="output/step7")
    ap.add_argument("--neg", default="data/poolA_negative_drug_level.csv")
    ap.add_argument("--outdir", default="output/step8")
    ap.add_argument("--target_disease", default="atherosclerosis")
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--prefer_go", type=int, default=1, help="1: GO优先；0: 纯按rank_key排序")
    ap.add_argument(
        "--include_explore",
        type=int,
        default=1,
        help="1=shortlist预留探索通道候选位, 0=不预留",
    )
    ap.add_argument(
        "--min_explore_slots",
        type=int,
        default=1,
        help="当 include_explore=1 时保留的最小探索位",
    )
    ap.add_argument(
        "--strict_contract",
        type=int,
        default=1,
        help="1=fail on contract mismatch, 0=warn only",
    )
    args = ap.parse_args()

    strict_contract = bool(args.strict_contract)
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    step7_dir = Path(args.step7_dir).resolve()
    step7_cards_path = step7_dir / "step7_cards.json"
    if not step7_cards_path.exists():
        raise FileNotFoundError(f"Missing {step7_cards_path}. Please run Step7 first.")

    with step7_cards_path.open("r", encoding="utf-8") as f:
        cards = json.load(f)
    card_contract_issues = validate_step7_cards(cards)
    _handle_contract_issues(
        card_contract_issues,
        strict_contract,
        f"Step7 cards contract mismatch ({STEP7_CARDS_SCHEMA}@{STEP7_CARDS_VERSION})",
    )

    base_dir = str(step7_dir)

    # neg trials
    neg_path = Path(args.neg).resolve() if args.neg else None
    neg = pd.read_csv(neg_path) if neg_path and neg_path.exists() else pd.DataFrame()
    if len(neg):
        if "drug_raw" in neg.columns:
            neg["_canon"] = neg["drug_raw"].astype(str).apply(canonicalize_name)
        elif "drug_normalized" in neg.columns:
            neg["_canon"] = neg["drug_normalized"].astype(str).apply(canonicalize_name)
        else:
            neg["_canon"] = ""

    rows = []
    for c in cards:
        canon = str(c.get("canonical_name","")).strip()
        # Cards may use "dossier_path" or "dossier_json" depending on version
        raw_dossier_path = c.get("dossier_json") or c.get("dossier_path") or ""
        dossier_json = resolve_path(base_dir, raw_dossier_path)
        raw_md_path = c.get("dossier_md") or ""
        if not raw_md_path and dossier_json:
            raw_md_path = dossier_json.replace(".json", ".md")
        dossier_md = raw_md_path
        dossier = read_json(dossier_json) or {}
        m = dossier_metrics(dossier)

        total = (c.get("scores") or {}).get("total_score_0_100", c.get("total_score_0_100", 0.0))
        try:
            total = float(total)
        except Exception:
            total = 0.0

        neg_trials_n = 0
        neg_trial_summary = ""
        if len(neg):
            tr = neg[neg["_canon"].str.lower() == canonicalize_name(canon).lower()].copy()
            neg_trials_n = len(tr)
            if neg_trials_n:
                neg_trial_summary = " | ".join(
                    [f"{r.get('nctId','')}: {str(r.get('primary_outcome_pvalues',''))}"
                     for _, r in tr.head(3).iterrows()]
                )

        rows.append({
            "canonical_name": canon,
            "drug_id": c.get("drug_id",""),
            "gate": c.get("gate") or c.get("gate_decision",""),
            "decision_channel": c.get("decision_channel", "exploit"),
            "endpoint_type": c.get("endpoint_type",""),
            "total_score_0_100": total,
            "novelty_score": float(c.get("novelty_score", 0.0) or 0.0),
            "uncertainty_score": float(c.get("uncertainty_score", 0.0) or 0.0),
            "safety_blacklist_hit": bool(c.get("safety_blacklist_hit", False)),
            **m,
            "neg_trials_n": neg_trials_n,
            "neg_trial_summary": neg_trial_summary,
            "dossier_json": dossier_json,
            "dossier_md": dossier_md,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("No candidates found in step7_cards.json")

    # 自动排序（更像真实平台）：unique PMIDs > topic > total > (harm/neg trials/safety)惩罚
    df["topic_match_ratio_filled"] = df["topic_match_ratio"].fillna(0.0).astype(float)
    df["rank_key"] = (
        df["unique_supporting_pmids_count"].fillna(0).astype(float) * 10.0
        + df["topic_match_ratio_filled"] * 5.0
        + df["total_score_0_100"].fillna(0).astype(float)
        + df["novelty_score"].fillna(0).astype(float) * 8.0
        - df["uncertainty_score"].fillna(0).astype(float) * 4.0
        - df["harm_or_neutral_sentence_count"].fillna(0).astype(float) * 0.5
        - df["neg_trials_n"].fillna(0).astype(float) * 1.0
        - df["safety_blacklist_hit"].astype(int) * 8.0
    )

    sorted_all = df.sort_values(["rank_key", "canonical_name"], ascending=[False, True]).copy()
    if args.prefer_go == 1 and (df["gate"] == "GO").any():
        go_pool = sorted_all[sorted_all["gate"] == "GO"].copy()
        explore_pool = sorted_all[
            (sorted_all["gate"] == "MAYBE") & (sorted_all["decision_channel"] == "explore")
        ].copy()

        reserve = 0
        if args.include_explore == 1 and not explore_pool.empty:
            reserve = max(0, min(int(args.min_explore_slots), int(args.topk)))

        go_take = max(0, int(args.topk) - reserve)
        shortlist_parts = [go_pool.head(go_take)]
        if reserve > 0:
            shortlist_parts.append(explore_pool.head(reserve))
        shortlist = (
            pd.concat(shortlist_parts, ignore_index=True)
            .drop_duplicates(subset=["drug_id"], keep="first")
            .copy()
        )
        if len(shortlist) < args.topk:
            fill = sorted_all[~sorted_all["drug_id"].isin(shortlist["drug_id"])].head(args.topk - len(shortlist))
            shortlist = pd.concat([shortlist, fill], ignore_index=True)
    else:
        shortlist = sorted_all.head(args.topk).copy()

    # ===== Release Gate: block if NO-GO drugs in shortlist =====
    try:
        gate_cfg = ReleaseGateConfig(block_nogo=True, min_go_ratio=0.0, strict=True)
        release_gate = ReleaseGate(gate_cfg)
        gate_result = release_gate.check_shortlist_composition(shortlist)
        if not gate_result.passed:
            print(f"[RELEASE GATE BLOCKED] {gate_result.summary()}")
            nogo_drugs = shortlist[shortlist["gate"] == "NO-GO"]["canonical_name"].tolist()
            shortlist = shortlist[shortlist["gate"] != "NO-GO"].copy()
            print(f"  Removed {len(nogo_drugs)} NO-GO drug(s): {nogo_drugs}")
            if shortlist.empty:
                raise ValueError("Release gate: all candidates were NO-GO. Cannot produce shortlist.")
        else:
            print(f"[RELEASE GATE PASSED] {gate_result.summary()}")
    except ImportError:
        print("[WARN] Release gate not available, skipping")

    shortlist["contract_version"] = STEP8_SHORTLIST_VERSION
    enforcer = ContractEnforcer(strict=strict_contract)
    enforcer.check_step8_shortlist(shortlist)

    # outputs
    shortlist_csv = outdir / f"step8_shortlist_top{args.topk}.csv"
    shortlist.to_csv(shortlist_csv, index=False, encoding="utf-8-sig")

    xlsx_path = outdir / "step8_candidate_pack_from_step7.xlsx"
    md_path = outdir / f"step8_one_pagers_top{args.topk}.md"

    used_sheets = set()
    md_lines = [f"# Step8 Candidate One-Pagers (target={args.target_disease})", ""]

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        # summary
        cols = [
            "canonical_name","drug_id","gate","decision_channel","endpoint_type","total_score_0_100","rank_key",
            "novelty_score","uncertainty_score",
            "unique_supporting_pmids_count","supporting_sentence_count",
            "unique_harm_pmids_count","harm_or_neutral_sentence_count",
            "topic_match_ratio","neg_trials_n","neg_trial_summary","safety_blacklist_hit","dossier_md"
        ]
        s = shortlist.copy()
        s["rank_key"] = s["rank_key"].round(2)
        s.to_excel(writer, sheet_name="Shortlist", index=False, columns=[c for c in cols if c in s.columns])

        for _, r in shortlist.iterrows():
            name = r["canonical_name"]
            sheet = safe_sheet_name(name, used_sheets)

            dossier = read_json(r["dossier_json"]) or {}
            llm = dossier.get("llm_structured") or {}
            se = llm.get("supporting_evidence") or []
            hn = llm.get("harm_or_neutral_evidence") or []
            mech = llm.get("proposed_mechanisms") or []
            risks = llm.get("key_risks") or []

            se_lines = top_evidence_lines(se, n=10)
            hn_lines = top_evidence_lines(hn, n=10)

            # CT.gov neg trials detail
            trials_df = pd.DataFrame()
            if len(neg):
                tr = neg[neg["_canon"].str.lower() == canonicalize_name(name).lower()].copy()
                tr_cols = [c for c in ["nctId","overallStatus","phase","conditions","primary_outcome_title","primary_outcome_pvalues","whyStopped"] if c in tr.columns]
                trials_df = tr[tr_cols].copy() if len(tr) else pd.DataFrame(columns=tr_cols)

            meta_df = pd.DataFrame([{
                "drug": name,
                "gate": r.get("gate",""),
                "decision_channel": r.get("decision_channel", "exploit"),
                "endpoint_type": r.get("endpoint_type",""),
                "total_score_0_100": r.get("total_score_0_100",""),
                "rank_key": round(float(r.get("rank_key",0.0)), 2),
                "novelty_score": round(float(r.get("novelty_score", 0.0) or 0.0), 4),
                "uncertainty_score": round(float(r.get("uncertainty_score", 0.0) or 0.0), 4),
                "topic_match_ratio": r.get("topic_match_ratio",""),
                "unique_supporting_pmids_count": r.get("unique_supporting_pmids_count",""),
                "harm_or_neutral_sentence_count": r.get("harm_or_neutral_sentence_count",""),
                "neg_trials_n": r.get("neg_trials_n",""),
                "safety_blacklist_hit": r.get("safety_blacklist_hit",""),
                "dossier_md": r.get("dossier_md",""),
            }])
            meta_df.to_excel(writer, sheet_name=sheet, index=False, startrow=0)

            start = len(meta_df) + 2
            pd.DataFrame({"Proposed mechanisms": mech[:20]}).to_excel(writer, sheet_name=sheet, index=False, startrow=start)
            start += max(4, min(24, len(mech)+3))
            pd.DataFrame({"Key risks": risks[:20]}).to_excel(writer, sheet_name=sheet, index=False, startrow=start)
            start += max(4, min(24, len(risks)+3))
            pd.DataFrame({"Supporting evidence (top10)": se_lines}).to_excel(writer, sheet_name=sheet, index=False, startrow=start)
            start += max(14, len(se_lines)+4)
            pd.DataFrame({"Harm/neutral evidence (top10)": hn_lines}).to_excel(writer, sheet_name=sheet, index=False, startrow=start)
            start += max(14, len(hn_lines)+4)
            if len(trials_df):
                trials_df.to_excel(writer, sheet_name=sheet, index=False, startrow=start)

            # MD one-pager
            md_lines += [
                f"## {name}",
                f"- Gate: **{r.get('gate','')}** ({r.get('decision_channel','exploit')}) | Score: {r.get('total_score_0_100','')} | Endpoint: {r.get('endpoint_type','')}",
                f"- Unique supporting PMIDs: **{r.get('unique_supporting_pmids_count','')}** | Harm sentences: {r.get('harm_or_neutral_sentence_count','')}",
                f"- Topic match ratio: {r.get('topic_match_ratio','')} | Novelty: {r.get('novelty_score', 0.0)} | Uncertainty: {r.get('uncertainty_score', 0.0)}",
                "",
                "### Mechanism hypotheses (auto)",
                *( [f"- {x}" for x in mech[:6]] if mech else ["- (missing; see dossier)"] ),
                "",
                "### Supporting evidence (top)",
                *( [f"- {x}" for x in se_lines[:6]] if se_lines else ["- (none)"] ),
                "",
                "### Negative / risk evidence (top)",
                *( [f"- {x}" for x in hn_lines[:6]] if hn_lines else ["- (none)"] ),
                "",
                "### Negative trials (CT.gov)",
            ]
            if len(trials_df):
                for _, t in trials_df.head(5).iterrows():
                    md_lines.append(f"- {t.get('nctId','')} | {t.get('phase','')} | {str(t.get('primary_outcome_pvalues',''))[:80]} | {str(t.get('whyStopped',''))[:120]}")
            else:
                md_lines.append("- (no negative trial row found)")
            md_lines += [
                "",
                "### Step9 next actions",
                "- Define 2–3 primary readouts and stop/go thresholds (**HUMAN 必须设定**).",
                "- Execute minimal in-vitro/ex-vivo validation (**HUMAN 执行**).",
                "- Only then consider structure work if needed.",
                ""
            ]

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    input_files = [step7_cards_path]
    if neg_path and neg_path.exists():
        input_files.append(neg_path)
    input_files.extend(
        Path(p).resolve()
        for p in df["dossier_json"].astype(str).tolist()
        if p and Path(p).exists()
    )
    output_files = [shortlist_csv, xlsx_path, md_path]

    manifest = build_manifest(
        pipeline="step8_candidate_pack",
        repo_root=Path(__file__).resolve().parent.parent,
        input_files=input_files,
        output_files=output_files,
        config={
            "step7_dir": str(step7_dir),
            "neg": str(neg_path) if neg_path else "",
            "outdir": str(outdir),
            "target_disease": args.target_disease,
            "topk": int(args.topk),
            "prefer_go": int(args.prefer_go),
            "include_explore": int(args.include_explore),
            "min_explore_slots": int(args.min_explore_slots),
            "strict_contract": strict_contract,
        },
        summary={
            "candidates_total": int(len(df)),
            "shortlist_count": int(len(shortlist)),
            "go_candidates": int((df["gate"] == "GO").sum()),
            "explore_candidates": int(((df["gate"] == "MAYBE") & (df["decision_channel"] == "explore")).sum()),
        },
        contracts={
            STEP7_CARDS_SCHEMA: STEP7_CARDS_VERSION,
            STEP8_SHORTLIST_SCHEMA: STEP8_SHORTLIST_VERSION,
        },
    )
    manifest_path = outdir / "step8_manifest.json"
    write_manifest(manifest_path, manifest)

    print("DONE Step8:", args.outdir)
    print(" -", shortlist_csv.name)
    print(" -", xlsx_path.name)
    print(" -", md_path.name)
    print(" -", manifest_path.name)

if __name__ == "__main__":
    main()
