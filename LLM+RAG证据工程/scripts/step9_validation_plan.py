#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step9: Build executable validation plan from Step8 shortlist.

Reads:
  - Step8 shortlist CSV (step8_shortlist_top*.csv)
  - Optional Step7 validation plan + gating summary

Writes:
  - step9_validation_plan.csv
  - step9_validation_plan.md
  - step9_manifest.json
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Any, Optional, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dr.common.provenance import build_manifest, write_manifest
from src.dr.contracts import (
    STEP8_SHORTLIST_SCHEMA,
    STEP8_SHORTLIST_VERSION,
    STEP9_PLAN_SCHEMA,
    STEP9_PLAN_VERSION,
    validate_contract_version_values,
    validate_step8_shortlist_columns,
    validate_step9_plan_columns,
)
from src.dr.contracts_enforcer import ContractEnforcer


def _pick_shortlist(step8_dir: Path, shortlist: str = "") -> Path:
    if shortlist:
        p = Path(shortlist).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Shortlist not found: {p}")
        return p

    candidates = sorted(
        step8_dir.glob("step8_shortlist_top*.csv"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No step8 shortlist found in {step8_dir}. Expected step8_shortlist_top*.csv"
        )
    return candidates[0]


def _endpoint_readouts(endpoint_type: str) -> str:
    endpoint = (endpoint_type or "OTHER").upper()
    mapping = {
        "PLAQUE_IMAGING": "plaque_area_pct_change; lesion_burden; inflammatory_marker_panel",
        "CV_EVENTS": "MACE_surrogate; CV_event_composite; mortality_signal",
        "PAD_FUNCTION": "walking_distance; ABI_change; claudication_score",
        "BIOMARKER": "LDL_C; hsCRP; IL6; endothelial_function_panel",
        "OTHER": "disease_relevant_primary_readout; orthogonal_biomarker_panel",
    }
    return mapping.get(endpoint, mapping["OTHER"])


def _priority_tier(gate: str, total_score: float, decision_channel: str = "exploit") -> str:
    gate_u = (gate or "").upper()
    channel = (decision_channel or "exploit").lower()
    if gate_u == "MAYBE" and channel == "explore":
        return "P2E"
    if gate_u == "GO" and total_score >= 70:
        return "P1"
    if gate_u in {"GO", "MAYBE"}:
        return "P2"
    return "P3"


def _default_stage(gate: str, decision_channel: str = "exploit") -> str:
    gate_u = (gate or "").upper()
    channel = (decision_channel or "exploit").lower()
    if gate_u == "MAYBE" and channel == "explore":
        return "explore_mechanism_screen"
    if gate_u == "GO":
        return "in_vitro_and_ex_vivo"
    if gate_u == "MAYBE":
        return "fast_fail_in_vitro"
    return "archive_or_reframe"


def _default_timeline_weeks(priority_tier: str) -> int:
    if priority_tier == "P1":
        return 4
    if priority_tier == "P2E":
        return 5
    if priority_tier == "P2":
        return 6
    return 2


def _stop_go_criteria(gate: str, priority_tier: str, decision_channel: str = "exploit") -> str:
    gate_u = (gate or "").upper()
    channel = (decision_channel or "exploit").lower()
    if gate_u == "MAYBE" and channel == "explore":
        return (
            "Advance to exploit only after >=2 orthogonal assays reproduce benefit "
            "and no severe harm signal emerges."
        )
    if gate_u == "GO":
        return (
            "Advance only if >=20% improvement vs control on primary readout "
            "and no major safety signal."
        )
    if priority_tier == "P2":
        return (
            "Advance only if reproducible trend across >=2 orthogonal assays "
            "with no harm signal."
        )
    return "Do not advance unless new independent evidence materially changes risk/benefit."


def _index_step7_plan(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if df.empty:
        return out
    for _, row in df.iterrows():
        key_drug_id = str(row.get("drug_id", "")).strip()
        key_name = str(row.get("canonical_name", "")).strip().lower()
        payload = row.to_dict()
        if key_drug_id:
            out[f"id:{key_drug_id}"] = payload
        if key_name:
            out[f"name:{key_name}"] = payload
    return out


def _lookup_step7_plan(
    index: Dict[str, Dict[str, Any]],
    drug_id: str,
    canonical_name: str,
) -> Optional[Dict[str, Any]]:
    return index.get(f"id:{drug_id}") or index.get(f"name:{canonical_name.lower()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Step9: Validation plan generator")
    ap.add_argument("--step8_dir", default="output/step8", help="Step8 output directory")
    ap.add_argument("--step7_dir", default="output/step7", help="Step7 output directory")
    ap.add_argument("--shortlist", default="", help="Optional explicit shortlist CSV path")
    ap.add_argument("--outdir", default="output/step9", help="Step9 output directory")
    ap.add_argument("--target_disease", default="atherosclerosis")
    ap.add_argument(
        "--strict_contract",
        type=int,
        default=1,
        help="1=fail on Step8 contract mismatch, 0=warn only",
    )
    args = ap.parse_args()

    step8_dir = Path(args.step8_dir).resolve()
    step7_dir = Path(args.step7_dir).resolve()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    shortlist_path = _pick_shortlist(step8_dir, args.shortlist)
    shortlist_df = pd.read_csv(shortlist_path)
    if shortlist_df.empty:
        raise ValueError(f"Shortlist is empty: {shortlist_path}")
    strict_contract = bool(args.strict_contract)
    enforcer = ContractEnforcer(strict=strict_contract)
    enforcer.check_step8_shortlist(shortlist_df)
    if "contract_version" in shortlist_df.columns:
        version_issues = validate_contract_version_values(
            shortlist_df["contract_version"].tolist(),
            expected_version=STEP8_SHORTLIST_VERSION,
            label="step8.contract_version",
        )
        if version_issues:
            msg = (
                f"Step8 shortlist contract mismatch "
                f"({STEP8_SHORTLIST_SCHEMA}@{STEP8_SHORTLIST_VERSION}): {version_issues}"
            )
            if strict_contract:
                raise ValueError(msg)
            print(f"[WARN] {msg}")

    step7_plan_path = step7_dir / "step7_validation_plan.csv"
    step7_gating_path = step7_dir / "step7_gating_decision.csv"
    step7_plan_df = pd.read_csv(step7_plan_path) if step7_plan_path.exists() else pd.DataFrame()
    step7_gating_df = (
        pd.read_csv(step7_gating_path) if step7_gating_path.exists() else pd.DataFrame()
    )
    step7_index = _index_step7_plan(step7_plan_df)

    rows: List[Dict[str, Any]] = []
    sort_by = ["rank_key"] if "rank_key" in shortlist_df.columns else ["total_score_0_100"]
    ascending = [False]
    if "canonical_name" in shortlist_df.columns:
        sort_by.append("canonical_name")
        ascending.append(True)
    shortlist_sorted = shortlist_df.sort_values(by=sort_by, ascending=ascending).reset_index(drop=True)

    for i, row in shortlist_sorted.iterrows():
        drug_id = str(row.get("drug_id", "")).strip()
        canonical_name = str(row.get("canonical_name", "")).strip()
        gate = str(row.get("gate", row.get("gate_decision", "MAYBE"))).strip()
        decision_channel = str(row.get("decision_channel", "exploit")).strip() or "exploit"
        endpoint_type = str(row.get("endpoint_type", "OTHER")).strip()

        try:
            total_score = float(row.get("total_score_0_100", 0.0) or 0.0)
        except Exception:
            total_score = 0.0

        p_tier = _priority_tier(gate, total_score, decision_channel)
        stage = _default_stage(gate, decision_channel)
        timeline_weeks = _default_timeline_weeks(p_tier)

        step7_plan = _lookup_step7_plan(step7_index, drug_id, canonical_name)
        if step7_plan:
            stage = str(step7_plan.get("validation_stage", stage))
            try:
                timeline_weeks = int(step7_plan.get("timeline_weeks", timeline_weeks))
            except Exception:
                pass

        try:
            support_pmids = int(row.get("unique_supporting_pmids_count", 0) or 0)
        except Exception:
            support_pmids = 0
        try:
            harm_sentences = int(row.get("harm_or_neutral_sentence_count", 0) or 0)
        except Exception:
            harm_sentences = 0

        evidence_gap = (
            "Increase orthogonal evidence depth"
            if support_pmids < 3
            else "Strengthen translational bridge to human endpoints"
        )
        if harm_sentences > support_pmids:
            evidence_gap = "Resolve harm/neutral imbalance before progression"

        rows.append(
            {
                "rank": i + 1,
                "drug_id": drug_id,
                "canonical_name": canonical_name,
                "gate": gate,
                "decision_channel": decision_channel,
                "priority_tier": p_tier,
                "recommended_stage": stage,
                "timeline_weeks": timeline_weeks,
                "target_disease": args.target_disease,
                "endpoint_type": endpoint_type,
                "total_score_0_100": round(total_score, 2),
                "novelty_score": round(float(row.get("novelty_score", 0.0) or 0.0), 4),
                "uncertainty_score": round(float(row.get("uncertainty_score", 0.0) or 0.0), 4),
                "primary_readouts": _endpoint_readouts(endpoint_type),
                "stop_go_criteria": _stop_go_criteria(gate, p_tier, decision_channel),
                "evidence_gap": evidence_gap,
                "owner": "HUMAN_REQUIRED",
                "shortlist_source": str(shortlist_path),
            }
        )

    plan_df = pd.DataFrame(rows)
    plan_df["contract_version"] = STEP9_PLAN_VERSION
    enforcer.check_step9_plan(plan_df)
    plan_csv = outdir / "step9_validation_plan.csv"
    plan_df.to_csv(plan_csv, index=False, encoding="utf-8-sig")

    md_lines = [
        f"# Step9 Validation Plan ({args.target_disease})",
        "",
        f"- Source shortlist: `{shortlist_path}`",
        f"- Step7 plan available: {'yes' if not step7_plan_df.empty else 'no'}",
        f"- Step7 gating available: {'yes' if not step7_gating_df.empty else 'no'}",
        "",
    ]
    for _, r in plan_df.iterrows():
        md_lines.extend(
            [
                f"## {int(r['rank'])}. {r['canonical_name']} ({r['drug_id']})",
                f"- Gate: **{r['gate']}** ({r.get('decision_channel', 'exploit')}) | Priority: **{r['priority_tier']}** | Score: {r['total_score_0_100']}",
                f"- Recommended stage: `{r['recommended_stage']}` | Timeline: {int(r['timeline_weeks'])} weeks",
                f"- Novelty: {r.get('novelty_score', 0.0)} | Uncertainty: {r.get('uncertainty_score', 0.0)}",
                f"- Primary readouts: {r['primary_readouts']}",
                f"- Stop/Go criteria: {r['stop_go_criteria']}",
                f"- Evidence gap: {r['evidence_gap']}",
                f"- Owner: {r['owner']}",
                "",
            ]
        )

    plan_md = outdir / "step9_validation_plan.md"
    plan_md.write_text("\n".join(md_lines), encoding="utf-8")

    input_files = [shortlist_path]
    if step7_plan_path.exists():
        input_files.append(step7_plan_path)
    if step7_gating_path.exists():
        input_files.append(step7_gating_path)

    manifest = build_manifest(
        pipeline="step9_validation_plan",
        repo_root=Path(__file__).resolve().parent.parent,
        input_files=input_files,
        output_files=[plan_csv, plan_md],
        config={
            "step8_dir": str(step8_dir),
            "step7_dir": str(step7_dir),
            "shortlist": str(shortlist_path),
            "outdir": str(outdir),
            "target_disease": args.target_disease,
            "strict_contract": strict_contract,
        },
        summary={
            "candidates": int(len(plan_df)),
            "p1_count": int((plan_df["priority_tier"] == "P1").sum()),
            "p2_count": int((plan_df["priority_tier"] == "P2").sum()),
            "p2e_count": int((plan_df["priority_tier"] == "P2E").sum()),
            "p3_count": int((plan_df["priority_tier"] == "P3").sum()),
        },
        contracts={
            STEP8_SHORTLIST_SCHEMA: STEP8_SHORTLIST_VERSION,
            STEP9_PLAN_SCHEMA: STEP9_PLAN_VERSION,
        },
    )
    manifest_path = outdir / "step9_manifest.json"
    write_manifest(manifest_path, manifest)

    print("DONE Step9:", outdir)
    print(" -", plan_csv.name)
    print(" -", plan_md.name)
    print(" -", manifest_path.name)


if __name__ == "__main__":
    main()
