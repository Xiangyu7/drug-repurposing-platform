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

from src.dr.scoring import (
    DrugScorer,
    ScoringConfig,
    GatingEngine,
    GatingConfig,
    HypothesisCardBuilder,
    ValidationPlanner
)
from src.dr.common.file_io import read_json
from src.dr.logger import setup_logger

logger = setup_logger(__name__, log_file="step7_score_and_gate.log")


def load_dossiers(dossier_dir: Path) -> List[Dict[str, Any]]:
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
            dossiers.append(dossier)
            dossier_paths.append(str(dossier_file))
            logger.debug("Loaded: %s", dossier_file.name)
        except Exception as e:
            logger.error("Failed to load %s: %s", dossier_file.name, e)

    logger.info("Successfully loaded %d dossiers", len(dossiers))
    return dossiers, dossier_paths


def main():
    """Step7 main pipeline"""
    parser = argparse.ArgumentParser(description="Step7: Score and Gate Drugs")
    parser.add_argument("--input", required=True, help="Input directory (Step6 output)")
    parser.add_argument("--out", default="output/step7", help="Output directory")
    args = parser.parse_args()

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
    dossiers, dossier_paths = load_dossiers(dossier_dir)

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
    scores_csv = out_dir / "step7_scores.csv"
    scores_df.to_csv(scores_csv, index=False, encoding="utf-8-sig")
    logger.info("Saved scores: %s", scores_csv)

    # 2. Gating decision CSV
    gating_df = pd.DataFrame([
        {
            "drug_id": dossier["drug_id"],
            "canonical_name": dossier["canonical_name"],
            "gate_decision": decision.decision.value,
            "gate_reasons": "; ".join(decision.gate_reasons) if decision.gate_reasons else "",
            "total_score": decision.scores.get("total_score_0_100", 0),
            "benefit": decision.metrics.get("benefit", 0),
            "harm": decision.metrics.get("harm", 0),
            "neutral": decision.metrics.get("neutral", 0),
            "total_pmids": decision.metrics.get("total_pmids", 0)
        }
        for dossier, decision in zip(dossiers, all_decisions)
    ])
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

    logger.info("  Gating Results:")
    logger.info("    ✅ GO: %d drugs", go_count)
    logger.info("    ⚠️  MAYBE: %d drugs", maybe_count)
    logger.info("    ❌ NO-GO: %d drugs", no_go_count)

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


if __name__ == "__main__":
    main()
