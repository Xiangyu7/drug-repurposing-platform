"""Pipeline data contracts and validators.

This module defines stable schema versions and structural validators for
cross-step payloads. The goal is to catch silent schema drift early.
"""

from typing import Any, Dict, Iterable, List

STEP6_DOSSIER_SCHEMA = "step6_dossier"
STEP6_DOSSIER_VERSION = "1.0.0"

STEP7_SCORES_SCHEMA = "step7_scores_csv"
STEP7_SCORES_VERSION = "1.0.0"

STEP7_GATING_SCHEMA = "step7_gating_csv"
STEP7_GATING_VERSION = "1.0.0"

STEP7_CARDS_SCHEMA = "step7_cards_json"
STEP7_CARDS_VERSION = "1.0.0"

STEP8_SHORTLIST_SCHEMA = "step8_shortlist_csv"
STEP8_SHORTLIST_VERSION = "1.0.0"

STEP9_PLAN_SCHEMA = "step9_validation_plan_csv"
STEP9_PLAN_VERSION = "1.0.0"

STEP6_DOSSIER_REQUIRED_TOP_LEVEL = {
    "drug_id",
    "canonical_name",
    "target_disease",
    "endpoint_type",
    "pubmed_rag",
    "llm_structured",
}

STEP6_DOSSIER_REQUIRED_COUNTS = {
    "supporting_evidence_count",
    "supporting_sentence_count",
    "unique_supporting_pmids_count",
    "harm_or_neutral_count",
}

STEP7_SCORES_REQUIRED_COLUMNS = {
    "drug_id",
    "canonical_name",
    "evidence_strength_0_30",
    "mechanism_plausibility_0_20",
    "translatability_0_20",
    "safety_fit_0_20",
    "practicality_0_10",
    "total_score_0_100",
}

STEP7_GATING_REQUIRED_COLUMNS = {
    "drug_id",
    "canonical_name",
    "gate_decision",
    "gate_reasons",
    "total_score",
    "benefit",
    "harm",
    "neutral",
    "total_pmids",
}

STEP8_SHORTLIST_REQUIRED_COLUMNS = {
    "canonical_name",
    "drug_id",
    "gate",
    "endpoint_type",
    "total_score_0_100",
    "safety_blacklist_hit",
    "supporting_sentence_count",
    "unique_supporting_pmids_count",
    "harm_or_neutral_sentence_count",
    "topic_match_ratio",
    "neg_trials_n",
    "dossier_json",
    "dossier_md",
    "rank_key",
}

STEP9_PLAN_REQUIRED_COLUMNS = {
    "rank",
    "drug_id",
    "canonical_name",
    "gate",
    "priority_tier",
    "recommended_stage",
    "timeline_weeks",
    "target_disease",
    "endpoint_type",
    "total_score_0_100",
    "primary_readouts",
    "stop_go_criteria",
    "evidence_gap",
    "owner",
    "shortlist_source",
    "contract_version",
}


def stamp_step6_dossier_contract(
    dossier: Dict[str, Any],
    producer: str,
) -> Dict[str, Any]:
    """Attach/overwrite contract metadata on a Step6 dossier."""
    contract = {
        "schema": STEP6_DOSSIER_SCHEMA,
        "version": STEP6_DOSSIER_VERSION,
        "producer": producer,
    }
    dossier["_contract"] = contract
    return dossier


def validate_step6_dossier(
    dossier: Dict[str, Any],
    require_contract: bool = False,
) -> List[str]:
    """Validate Step6 dossier structure; return issues list."""
    issues: List[str] = []
    if not isinstance(dossier, dict):
        return ["dossier is not a dict"]

    for key in sorted(STEP6_DOSSIER_REQUIRED_TOP_LEVEL):
        if key not in dossier:
            issues.append(f"missing top-level key: {key}")

    pubmed_rag = dossier.get("pubmed_rag")
    if isinstance(pubmed_rag, dict):
        if not isinstance(pubmed_rag.get("top_abstracts", []), list):
            issues.append("pubmed_rag.top_abstracts must be a list")
        if not isinstance(pubmed_rag.get("top_sentences", []), list):
            issues.append("pubmed_rag.top_sentences must be a list")
    else:
        issues.append("pubmed_rag must be a dict")

    llm_structured = dossier.get("llm_structured")
    if isinstance(llm_structured, dict):
        if not isinstance(llm_structured.get("supporting_evidence", []), list):
            issues.append("llm_structured.supporting_evidence must be a list")
        if not isinstance(llm_structured.get("harm_or_neutral_evidence", []), list):
            issues.append("llm_structured.harm_or_neutral_evidence must be a list")
        counts = llm_structured.get("counts")
        if isinstance(counts, dict):
            for key in sorted(STEP6_DOSSIER_REQUIRED_COUNTS):
                if key not in counts:
                    issues.append(f"missing llm_structured.counts key: {key}")
        else:
            issues.append("llm_structured.counts must be a dict")
    else:
        issues.append("llm_structured must be a dict")

    contract = dossier.get("_contract")
    if require_contract and contract is None:
        issues.append("missing _contract metadata")
    if contract is not None:
        if not isinstance(contract, dict):
            issues.append("_contract must be a dict")
        else:
            schema = contract.get("schema")
            version = contract.get("version")
            if schema != STEP6_DOSSIER_SCHEMA:
                issues.append(
                    f"invalid _contract.schema: expected {STEP6_DOSSIER_SCHEMA}, got {schema}"
                )
            if version != STEP6_DOSSIER_VERSION:
                issues.append(
                    f"invalid _contract.version: expected {STEP6_DOSSIER_VERSION}, got {version}"
                )

    return issues


def validate_step7_scores_columns(columns: Iterable[str]) -> List[str]:
    """Validate Step7 scores CSV columns."""
    col_set = set(columns)
    missing = sorted(STEP7_SCORES_REQUIRED_COLUMNS - col_set)
    return [f"missing column: {c}" for c in missing]


def validate_step7_gating_columns(columns: Iterable[str]) -> List[str]:
    """Validate Step7 gating CSV columns."""
    col_set = set(columns)
    missing = sorted(STEP7_GATING_REQUIRED_COLUMNS - col_set)
    return [f"missing column: {c}" for c in missing]


def validate_step7_cards(cards: Any) -> List[str]:
    """Validate Step7 cards JSON payload shape."""
    issues: List[str] = []
    if not isinstance(cards, list):
        return ["cards payload must be a list"]

    for i, card in enumerate(cards):
        prefix = f"cards[{i}]"
        if not isinstance(card, dict):
            issues.append(f"{prefix} must be a dict")
            continue

        for key in ("drug_id", "canonical_name", "scores"):
            if key not in card:
                issues.append(f"{prefix} missing key: {key}")

        if "gate_decision" not in card and "gate" not in card:
            issues.append(f"{prefix} missing key: gate_decision")
        if "dossier_path" not in card and "dossier_json" not in card:
            issues.append(f"{prefix} missing key: dossier_path")

        scores = card.get("scores")
        if "scores" in card and not isinstance(scores, dict):
            issues.append(f"{prefix}.scores must be a dict")

    return issues


def validate_step8_shortlist_columns(
    columns: Iterable[str],
    require_contract_version: bool = False,
) -> List[str]:
    """Validate Step8 shortlist CSV columns."""
    col_set = set(columns)
    missing = sorted(STEP8_SHORTLIST_REQUIRED_COLUMNS - col_set)
    issues = [f"missing column: {c}" for c in missing]
    if require_contract_version and "contract_version" not in col_set:
        issues.append("missing column: contract_version")
    return issues


def validate_step9_plan_columns(columns: Iterable[str]) -> List[str]:
    """Validate Step9 validation-plan CSV columns."""
    col_set = set(columns)
    missing = sorted(STEP9_PLAN_REQUIRED_COLUMNS - col_set)
    return [f"missing column: {c}" for c in missing]


def validate_contract_version_values(
    values: Iterable[Any],
    expected_version: str,
    label: str = "contract_version",
) -> List[str]:
    """Validate that all non-empty version values match the expected contract."""
    non_empty = {str(v).strip() for v in values if str(v).strip()}
    if not non_empty:
        return [f"{label} has no non-empty values"]
    if non_empty != {expected_version}:
        got = ", ".join(sorted(non_empty))
        return [f"{label} mismatch: expected {expected_version}, got [{got}]"]
    return []
