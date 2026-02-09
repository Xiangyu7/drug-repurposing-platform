#!/usr/bin/env python3
"""
Best-effort Step4 pipeline (most automated, minimal manual review).

Default uses cached versions of CT.gov fetchers to reduce 429s:
  - step4_ai_prefill_ctgov_only_cached.py
  - step4_route2_ctgov_structured_cached.py

Run:
  python step4_label_trials.py --all
  python step4_label_trials.py --all --no-pubmed
  python step4_label_trials.py --all --skip-existing
"""
import argparse
import os
import sys
import subprocess
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"

SCRIPTS = {
    "prefill": "step4_ai_prefill_ctgov_only_cached.py",
    "structured": "step4_route2_ctgov_structured_cached.py",
    "pubmed": "step4_route2_pubmed_supplement.py",
    "finalize": "step4_final_trial_labels.py",
    "merge": "step4_merge_to_poolA.py",
}

OUTPUTS = {
    "prefill": ["data/ai_labels.csv", "data/manual_review_only_low.xlsx"],
    "structured": ["data/ai_labels_ctgov_structured.csv", "data/manual_review_only_low_route2.xlsx"],
    "pubmed": ["data/ai_labels_pubmed_supplement.csv"],
    "finalize": ["data/step4_final_trial_labels.csv", "data/step4_manual_review_minimal.csv"],
    "merge": ["data/poolA_negative_drug_level.csv", "data/qc_step4_merge_summary.csv", "data/manual_review_minimal.csv"],
}

REQUIRED_INPUTS = {
    "prefill": ["data/manual_review_queue.csv"],
    "structured": ["data/ai_labels.csv"],
    "pubmed": ["data/ai_labels_ctgov_structured.csv"],
    "finalize": ["data/ai_labels_ctgov_structured.csv", "data/ai_labels_pubmed_supplement.csv"],
    "merge": ["data/poolA_trials.csv", "data/poolA_drug_level.csv", "data/step4_final_trial_labels.csv"],
}

def run_step(step: str, skip_existing: bool):
    script = THIS_DIR / SCRIPTS[step]
    if not script.exists():
        raise FileNotFoundError(f"Missing script: {script.name}")

    project_root = THIS_DIR.parent
    for req in REQUIRED_INPUTS.get(step, []):
        p = project_root / req
        if not p.exists():
            raise FileNotFoundError(f"[{step}] Missing required input file: {req}")

    outs = [project_root / o for o in OUTPUTS.get(step, [])]
    if skip_existing and outs and all(p.exists() for p in outs):
        print(f"[SKIP] {step}: outputs already exist")
        return

    print(f"[RUN ] {step}: {script.name}")
    subprocess.run([sys.executable, str(script)], cwd=str(project_root), check=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-pubmed", action="store_true")
    ap.add_argument("--prefill", action="store_true")
    ap.add_argument("--structured", action="store_true")
    ap.add_argument("--pubmed", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()

    if args.all:
        steps = ["prefill", "structured"]
        if not args.no_pubmed:
            steps.append("pubmed")
        steps += ["finalize", "merge"]
    else:
        steps = [k for k in ["prefill","structured","pubmed","finalize","merge"] if getattr(args,k)]

    if not steps:
        ap.print_help()
        return

    if ("finalize" in steps) and (("pubmed" not in steps) or args.no_pubmed):
        pubmed_path = DATA_DIR / "ai_labels_pubmed_supplement.csv"
        if not pubmed_path.exists():
            print("[INFO] PubMed step skipped; creating empty ai_labels_pubmed_supplement.csv placeholder.")
            pubmed_path.write_text(
                "nctId,pubmed_pmids,primary_endpoint_met_pubmed,outcome_label_pubmed,ai_confidence_pubmed,notes_pubmed\n",
                encoding="utf-8"
            )

    for step in steps:
        run_step(step, skip_existing=args.skip_existing)

    print("\nDONE. Key outputs:")
    for f in ["data/poolA_negative_drug_level.csv", "data/manual_review_minimal.csv", "data/qc_step4_merge_summary.csv"]:
        if (THIS_DIR.parent / f).exists():
            print(" -", f)

if __name__ == "__main__":
    main()
