#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step7: Score and Gate drugs from Step6 dossiers

Applies Phase 4 Scoring Layer to:
1. Calculate multi-dimensional scores for each drug
2. Apply gating rules (GO/MAYBE/NO-GO)
3. Generate hypothesis cards
4. Create validation plans

Input: Step6 dossiers (JSON files)
Output:
    - step7_scores.csv - Scores and metrics
    - step7_gating_decision.csv - Gating decisions
    - step7_cards.json - Hypothesis cards (structured)
    - step7_hypothesis_cards.md - Hypothesis cards (human-readable)
    - step7_validation_plan.csv - Validation plans

Usage:
    python scripts/step7_score_and_gate.py --input output/step6_simple
    python scripts/step7_score_and_gate.py --input output/step6_simple --out output/step7
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
from typing import List, Dict, Any
import pandas as pd
from contextlib import contextmanager

from src.dr.scoring import (
    DrugScorer,
    ScoringConfig,
    GatingEngine,
    GatingConfig,
    HypothesisCardBuilder,
    ValidationPlanner
)
from src.dr.common.file_io import read_json
from src.dr.common.provenance import build_manifest, write_manifest
from src.dr.contracts import (
    STEP6_DOSSIER_SCHEMA,
    STEP6_DOSSIER_VERSION,
    STEP7_SCORES_SCHEMA,
    STEP7_SCORES_VERSION,
    STEP7_GATING_SCHEMA,
    STEP7_GATING_VERSION,
    validate_step6_dossier,
    validate_step7_scores_columns,
    validate_step7_gating_columns,
)
from src.dr.contracts_enforcer import ContractEnforcer
from src.dr.logger import setup_logger
try:
    from src.dr.monitoring import track_pipeline_execution
except Exception:  # pragma: no cover - monitoring is optional at runtime
    @contextmanager
    def track_pipeline_execution(pipeline: str):
        yield

logger = setup_logger(__name__, log_file="step7_score_and_gate.log")


def adapt_dossier(dossier: Dict[str, Any]) -> Dict[str, Any]:
    """Bridge Step6 dossier structure to what DrugScorer expects.

    Step6 stores evidence in:
        llm_structured.supporting_evidence  (list of items with 'direction')
        llm_structured.harm_or_neutral_evidence
        llm_structured.counts.unique_supporting_pmids_count
        pubmed_rag.top_abstracts
    DrugScorer expects:
        evidence_count = {benefit, harm, neutral, unknown}
        total_pmids = int
    """
    ls = dossier.get("llm_structured") or {}
    all_items = (ls.get("supporting_evidence") or []) + (ls.get("harm_or_neutral_evidence") or [])

    direction_counts: Dict[str, int] = {}
    for item in all_items:
        d = (item.get("direction") or "unknown").lower()
        direction_counts[d] = direction_counts.get(d, 0) + 1

    counts = ls.get("counts") or {}
    pr = dossier.get("pubmed_rag") or {}
    total_pmids = counts.get("unique_supporting_pmids_count", 0)
    # Also count total abstracts retrieved as coverage indicator
    total_abstracts = len(pr.get("top_abstracts") or [])
    # Use whichever is larger as total_pmids
    total_pmids = max(total_pmids, total_abstracts)

    dossier["evidence_count"] = {
        "benefit": direction_counts.get("benefit", 0),
        "harm": direction_counts.get("harm", 0),
        "neutral": direction_counts.get("neutral", 0),
        # Extractor may emit either "unknown" (legacy) or "unclear" (current schema).
        "unknown": direction_counts.get("unknown", 0) + direction_counts.get("unclear", 0),
    }
    dossier["total_pmids"] = total_pmids

    return dossier


def load_dossiers(dossier_dir: Path, strict_contract: bool = True) -> List[Dict[str, Any]]:
    """Load all dossiers from directory

    Args:
        dossier_dir: Directory containing dossier JSON files

    Returns:
        List of dossier dictionaries
    """
    dossier_files = sorted(dossier_dir.glob("*.json"))
    logger.info("Found %d dossier files in %s", len(dossier_files), dossier_dir)

    dossiers = []
    dossier_paths = []

    for dossier_file in dossier_files:
        try:
            dossier = read_json(dossier_file)
            issues = validate_step6_dossier(
                dossier,
                require_contract=bool(strict_contract),
            )
            if issues:
                msg = (
                    f"Dossier contract mismatch for {dossier_file.name}: "
                    f"expected {STEP6_DOSSIER_SCHEMA}@{STEP6_DOSSIER_VERSION}; issues={issues}"
                )
                if strict_contract:
                    raise ValueError(msg)
                logger.warning(msg)
            dossier = adapt_dossier(dossier)
            dossiers.append(dossier)
            dossier_paths.append(str(dossier_file))
            logger.debug("Loaded: %s  evidence_count=%s total_pmids=%d",
                        dossier_file.name, dossier.get("evidence_count"), dossier.get("total_pmids", 0))
        except Exception as e:
            logger.error("Failed to load %s: %s", dossier_file.name, e)

    logger.info("Successfully loaded %d dossiers", len(dossiers))
    return dossiers, dossier_paths


def run_pipeline(args: argparse.Namespace) -> None:
    """Execute Step7 scoring and gating pipeline."""
    logger.info("=" * 60)
    logger.info("Step7: Score and Gate Drugs")
    logger.info("=" * 60)
    logger.info("Input: %s", args.input)
    logger.info("Output: %s", args.out)

    # Check input directory
    input_dir = Path(args.input)
    if not input_dir.exists():
        logger.error("Input directory not found: %s", input_dir)
        logger.error("Please run step6 first")
        sys.exit(1)

    dossier_dir = input_dir / "dossiers"
    if not dossier_dir.exists():
        logger.error("Dossiers directory not found: %s", dossier_dir)
        sys.exit(1)

    # Load dossiers
    logger.info("-" * 60)
    logger.info("Loading dossiers...")
    dossiers, dossier_paths = load_dossiers(
        dossier_dir,
        strict_contract=bool(args.strict_contract),
    )

    if not dossiers:
        logger.error("No dossiers loaded. Exiting.")
        sys.exit(1)

    # Initialize Phase 4 components
    logger.info("-" * 60)
    logger.info("Initializing Phase 4 components...")
    scorer = DrugScorer(config=ScoringConfig())
    gating_engine = GatingEngine(config=GatingConfig())
    card_builder = HypothesisCardBuilder()
    validation_planner = ValidationPlanner()
    enforcer = ContractEnforcer(strict=bool(args.strict_contract))

    # Process each drug
    logger.info("-" * 60)
    logger.info("Processing %d drugs...", len(dossiers))

    all_scores = []
    all_decisions = []
    all_cards = []
    all_plans = []

    for i, (dossier, dossier_path) in enumerate(zip(dossiers, dossier_paths), 1):
        drug_id = dossier.get("drug_id", "unknown")
        canonical = dossier.get("canonical_name", "unknown")

        logger.info("[%d/%d] Processing: %s (%s)", i, len(dossiers), canonical, drug_id)

        # Step 1: Score
        scores = scorer.score_drug(dossier)
        all_scores.append(scores)
        logger.info("  Scores: total=%.1f (evidence=%.1f, mechanism=%.1f, trans=%.1f, safety=%.1f, pract=%.1f)",
                   scores["total_score_0_100"],
                   scores["evidence_strength_0_30"],
                   scores["mechanism_plausibility_0_20"],
                   scores["translatability_0_20"],
                   scores["safety_fit_0_20"],
                   scores["practicality_0_10"])

        # Step 2: Gate
        decision = gating_engine.evaluate(dossier, scores, canonical)
        all_decisions.append(decision)
        logger.info("  Decision: %s %s",
                   decision.decision.value,
                   f"({'; '.join(decision.gate_reasons)})" if decision.gate_reasons else "")

        # Step 3: Build card
        card = card_builder.build_card(dossier, scores, decision, dossier_path)
        all_cards.append(card)

        # Step 4: Create validation plan (for GO/MAYBE only)
        if decision.decision.value in ["GO", "MAYBE"]:
            plan = validation_planner.create_plan(card, dossier)
            all_plans.append(plan)
            logger.info("  Validation: %s (priority=%d, timeline=%d weeks)",
                       plan.validation_stage, plan.priority, plan.timeline_weeks)

    # Create output directory
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save outputs
    logger.info("-" * 60)
    logger.info("Saving outputs...")

    # 1. Scores CSV
    scores_df = pd.DataFrame([
        {
            "drug_id": dossier["drug_id"],
            "canonical_name": dossier["canonical_name"],
            **scores
        }
        for dossier, scores in zip(dossiers, all_scores)
    ])
    scores_df["contract_version"] = STEP7_SCORES_VERSION
    enforcer.check_step7_scores(scores_df)
    scores_csv = out_dir / "step7_scores.csv"
    scores_df.to_csv(scores_csv, index=False, encoding="utf-8-sig")
    logger.info("Saved scores: %s", scores_csv)

    # 2. Gating decision CSV
    gating_df = pd.DataFrame([
        {
            "drug_id": dossier["drug_id"],
            "canonical_name": dossier["canonical_name"],
            "gate_decision": decision.decision.value,
            "decision_channel": getattr(decision, "decision_channel", "exploit"),
            "gate_reasons": "; ".join(decision.gate_reasons) if decision.gate_reasons else "",
            "total_score": decision.scores.get("total_score_0_100", 0),
            "benefit": decision.metrics.get("benefit", 0),
            "harm": decision.metrics.get("harm", 0),
            "neutral": decision.metrics.get("neutral", 0),
            "total_pmids": decision.metrics.get("total_pmids", 0),
            "novelty_score": getattr(decision, "novelty_score", 0.0),
            "uncertainty_score": getattr(decision, "uncertainty_score", 0.0),
        }
        for dossier, decision in zip(dossiers, all_decisions)
    ])
    gating_df["contract_version"] = STEP7_GATING_VERSION
    enforcer.check_step7_gating(gating_df)
    gating_csv = out_dir / "step7_gating_decision.csv"
    gating_df.to_csv(gating_csv, index=False, encoding="utf-8-sig")
    logger.info("Saved gating decisions: %s", gating_csv)

    # 3. Hypothesis cards (JSON)
    cards_json = out_dir / "step7_cards.json"
    card_builder.save_cards_json(all_cards, cards_json)
    logger.info("Saved cards JSON: %s", cards_json)

    # 4. Hypothesis cards (Markdown)
    cards_md = out_dir / "step7_hypothesis_cards.md"
    card_builder.save_cards_markdown(all_cards, cards_md)
    logger.info("Saved cards Markdown: %s", cards_md)

    # 5. Validation plans CSV
    if all_plans:
        validation_csv = out_dir / "step7_validation_plan.csv"
        validation_planner.save_plans_csv(all_plans, validation_csv)
        logger.info("Saved validation plans: %s", validation_csv)
    else:
        logger.warning("No drugs passed gating - no validation plans created")

    # Summary
    logger.info("=" * 60)
    logger.info("Step7 Complete!")
    logger.info("  Processed: %d drugs", len(dossiers))

    go_count = sum(1 for d in all_decisions if d.decision.value == "GO")
    maybe_count = sum(1 for d in all_decisions if d.decision.value == "MAYBE")
    no_go_count = sum(1 for d in all_decisions if d.decision.value == "NO-GO")
    explore_count = sum(1 for d in all_decisions if getattr(d, "decision_channel", "exploit") == "explore")
    maybe_explore_count = sum(
        1
        for d in all_decisions
        if d.decision.value == "MAYBE" and getattr(d, "decision_channel", "explore") == "explore"
    )

    logger.info("  Gating Results:")
    logger.info("    ✅ GO: %d drugs", go_count)
    logger.info("    ⚠️  MAYBE: %d drugs", maybe_count)
    logger.info("    ❌ NO-GO: %d drugs", no_go_count)
    logger.info("    🔎 Explore track: %d drugs (MAYBE=%d)", explore_count, maybe_explore_count)

    if go_count > 0:
        go_drugs = [
            dossier["canonical_name"]
            for dossier, decision in zip(dossiers, all_decisions)
            if decision.decision.value == "GO"
        ]
        logger.info("  GO drugs: %s", ", ".join(go_drugs))

    logger.info("  Outputs:")
    logger.info("    - %s", scores_csv)
    logger.info("    - %s", gating_csv)
    logger.info("    - %s", cards_json)
    logger.info("    - %s", cards_md)
    if all_plans:
        logger.info("    - %s", validation_csv)
    logger.info("=" * 60)

    output_files = [scores_csv, gating_csv, cards_json, cards_md]
    if all_plans:
        output_files.append(validation_csv)

    manifest = build_manifest(
        pipeline="step7_score_and_gate",
        repo_root=Path(__file__).resolve().parent.parent,
        input_files=[Path(p).resolve() for p in dossier_paths],
        output_files=output_files,
        config={
            "input_dir": str(input_dir.resolve()),
            "output_dir": str(out_dir.resolve()),
            "strict_contract": bool(args.strict_contract),
            "scorer_config": ScoringConfig().__dict__,
            "gating_config": GatingConfig().__dict__,
        },
        summary={
            "drugs_processed": int(len(dossiers)),
            "go_count": int(go_count),
            "maybe_count": int(maybe_count),
            "no_go_count": int(no_go_count),
            "explore_count": int(explore_count),
            "maybe_explore_count": int(maybe_explore_count),
        },
        contracts={
            STEP6_DOSSIER_SCHEMA: STEP6_DOSSIER_VERSION,
            STEP7_SCORES_SCHEMA: STEP7_SCORES_VERSION,
            STEP7_GATING_SCHEMA: STEP7_GATING_VERSION,
        },
    )
    manifest_path = out_dir / "step7_manifest.json"
    write_manifest(manifest_path, manifest)
    logger.info("Saved manifest: %s", manifest_path)


def main():
    """Step7 main pipeline"""
    parser = argparse.ArgumentParser(description="Step7: Score and Gate Drugs")
    parser.add_argument("--input", required=True, help="Input directory (Step6 output)")
    parser.add_argument("--out", default="output/step7", help="Output directory")
    parser.add_argument(
        "--strict_contract",
        type=int,
        default=1,
        help="1=fail on Step6 dossier contract mismatch, 0=warn only",
    )
    args = parser.parse_args()

    with track_pipeline_execution("step7_score_and_gate"):
        run_pipeline(args)


if __name__ == "__main__":
    main()
