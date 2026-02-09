#!/usr/bin/env python3
"""
End-to-end validation test: 3 drugs with known expected answers.

Runs the full step6 pipeline (PubMed retrieval -> BM25 -> rerank -> LLM extraction)
on 3 representative drugs and checks outputs against expected standards.

Usage:
    python scripts/test_e2e_sample.py

Expected runtime: 2-4 minutes (PubMed API + Ollama LLM).
"""

import sys, json, time
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.step6_evidence_extraction import process_one

# ============================================================
# Test samples with expected answers
# ============================================================

SAMPLES = [
    {
        "drug_id": "D81B744A593",
        "canonical_name": "resveratrol",
        "expect": {
            "confidence": ["HIGH"],              # expect HIGH
            "min_unique_pmids": 6,               # well-studied drug
            "min_supporting_evidence": 5,
            "min_topic_match": 0.4,
            "has_benefit_evidence": True,
        },
    },
    {
        "drug_id": "DD76A1941B2",
        "canonical_name": "dexamethasone",
        "expect": {
            "confidence": ["MED", "HIGH"],       # moderate evidence
            "min_unique_pmids": 3,
            "min_supporting_evidence": 2,
            "min_topic_match": 0.3,
            "has_benefit_evidence": True,
        },
    },
    {
        "drug_id": "D9F9BB8C160",
        "canonical_name": "creatine monohydrate",
        "expect": {
            "confidence": ["LOW"],               # irrelevant to atherosclerosis
            "max_unique_pmids": 1,               # expect 0
            "max_supporting_evidence": 1,
            "expect_empty_evidence": True,
        },
    },
]

TARGET_DISEASE = "atherosclerosis"
OUT_DIR = ROOT / "output" / "test_e2e"
CACHE_DIR = OUT_DIR / "cache" / "pubmed"
ALL_DRUG_NAMES = [s["canonical_name"] for s in SAMPLES]


def check(label, condition, detail=""):
    """Single assertion with pass/fail output."""
    status = "PASS" if condition else "FAIL"
    msg = f"    [{status}] {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    return condition


def validate_structure(dossier):
    """Validate dossier has correct structure (schema check)."""
    ok = True
    ok &= check("has drug_id", "drug_id" in dossier)
    ok &= check("has canonical_name", "canonical_name" in dossier)
    ok &= check("has qc", "qc" in dossier)
    ok &= check("has llm_structured", "llm_structured" in dossier)

    llm = dossier.get("llm_structured", {})
    ok &= check("confidence valid", llm.get("confidence") in ("LOW", "MED", "HIGH"),
                 f"got={llm.get('confidence')}")

    counts = llm.get("counts", {})
    up_count = counts.get("unique_supporting_pmids_count", -1)
    up_list = counts.get("unique_supporting_pmids", [])
    ok &= check("pmid count consistent", up_count == len(up_list),
                 f"count={up_count} vs list_len={len(up_list)}")

    tmr = dossier.get("qc", {}).get("topic_match_ratio", -1)
    ok &= check("topic_match_ratio in [0,1]", 0.0 <= tmr <= 1.0, f"got={tmr}")

    # Validate evidence items
    for ev in llm.get("supporting_evidence", [])[:3]:
        for key in ("pmid", "direction", "model", "claim", "confidence"):
            ok &= check(f"evidence has '{key}'", key in ev)

    return ok


def validate_expected(dossier, expect):
    """Validate drug-specific expected answers."""
    ok = True
    llm = dossier.get("llm_structured", {})
    counts = llm.get("counts", {})
    qc = dossier.get("qc", {})

    conf = llm.get("confidence", "")
    ok &= check("confidence matches",
                 conf in expect.get("confidence", []),
                 f"got={conf}, expect={expect.get('confidence')}")

    up = counts.get("unique_supporting_pmids_count", 0)

    if "min_unique_pmids" in expect:
        ok &= check(f"unique_pmids >= {expect['min_unique_pmids']}",
                     up >= expect["min_unique_pmids"], f"got={up}")

    if "max_unique_pmids" in expect:
        ok &= check(f"unique_pmids <= {expect['max_unique_pmids']}",
                     up <= expect["max_unique_pmids"], f"got={up}")

    se = llm.get("supporting_evidence", [])

    if "min_supporting_evidence" in expect:
        ok &= check(f"supporting_evidence >= {expect['min_supporting_evidence']}",
                     len(se) >= expect["min_supporting_evidence"], f"got={len(se)}")

    if "max_supporting_evidence" in expect:
        ok &= check(f"supporting_evidence <= {expect['max_supporting_evidence']}",
                     len(se) <= expect["max_supporting_evidence"], f"got={len(se)}")

    if "min_topic_match" in expect:
        tmr = qc.get("topic_match_ratio", 0.0)
        ok &= check(f"topic_match > {expect['min_topic_match']}",
                     tmr > expect["min_topic_match"], f"got={tmr:.4f}")

    if expect.get("has_benefit_evidence"):
        has_benefit = any(e.get("direction") == "benefit" for e in se)
        ok &= check("has benefit evidence", has_benefit)

    if expect.get("expect_empty_evidence"):
        ok &= check("evidence is empty", len(se) == 0, f"got={len(se)}")

    return ok


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    total = len(SAMPLES)
    passed = 0

    for i, sample in enumerate(SAMPLES, 1):
        name = sample["canonical_name"]
        print(f"\n{'='*60}")
        print(f"[{i}/{total}] {name} ({sample['drug_id']})")
        print(f"{'='*60}")

        t0 = time.time()
        try:
            json_path, md_path, dossier = process_one(
                drug_id=sample["drug_id"],
                canonical_name=name,
                target_disease=TARGET_DISEASE,
                endpoint_type_hint="OTHER",
                neg_path=None,
                out_dir=OUT_DIR,
                cache_dir=CACHE_DIR,
                all_drug_names=ALL_DRUG_NAMES,
            )
        except Exception as e:
            print(f"    [FAIL] process_one raised: {e}")
            continue
        elapsed = time.time() - t0
        print(f"    Completed in {elapsed:.1f}s")

        # Quick summary
        llm = dossier.get("llm_structured", {})
        counts = llm.get("counts", {})
        print(f"    confidence={llm.get('confidence')}  "
              f"unique_pmids={counts.get('unique_supporting_pmids_count', 0)}  "
              f"supporting={len(llm.get('supporting_evidence', []))}  "
              f"harm_neutral={counts.get('harm_or_neutral_count', 0)}  "
              f"mode={llm.get('mode')}  "
              f"topic_match={dossier.get('qc', {}).get('topic_match_ratio', 0):.4f}")

        # Run checks
        print()
        struct_ok = validate_structure(dossier)
        expect_ok = validate_expected(dossier, sample["expect"])

        if struct_ok and expect_ok:
            print(f"\n    >>> {name}: ALL CHECKS PASSED")
            passed += 1
        else:
            print(f"\n    >>> {name}: SOME CHECKS FAILED")

    # Final summary
    print(f"\n{'='*60}")
    print(f"RESULT: {passed}/{total} drugs passed all checks")
    print(f"{'='*60}")

    if passed == total:
        print("Pipeline validation SUCCEEDED.")
    else:
        print("Pipeline validation has FAILURES - check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
