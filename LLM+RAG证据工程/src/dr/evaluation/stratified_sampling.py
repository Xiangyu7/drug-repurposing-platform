"""Stratified sampling and coverage analysis for gold-standard sets.

Ensures gold-standard datasets are balanced across key dimensions
(direction, model, endpoint, drug) to prevent evaluation bias.

Features:
- Multi-dimensional stratified sampling
- Gap identification (under-represented strata)
- Coverage reports for audit
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .gold_standard import GoldStandardRecord
from ..logger import get_logger

logger = get_logger(__name__)

# Fields available for stratification
STRATIFIABLE_FIELDS = {"direction", "model", "endpoint", "confidence", "source", "drug_name"}


def compute_stratum_coverage(
    records: List[GoldStandardRecord],
) -> Dict[str, Dict[str, int]]:
    """Compute coverage per stratum dimension.

    Args:
        records: Gold-standard records to analyze

    Returns:
        Nested dict: {field: {value: count}}
        e.g., {"direction": {"benefit": 45, "harm": 12, ...}}
    """
    coverage: Dict[str, Dict[str, int]] = {}

    for fld in ("direction", "model", "endpoint", "confidence"):
        counts: Dict[str, int] = defaultdict(int)
        for r in records:
            val = getattr(r, fld, "").lower().strip()
            if val:
                counts[val] += 1
        coverage[fld] = dict(sorted(counts.items()))

    # Drug-level coverage
    drug_counts: Dict[str, int] = defaultdict(int)
    for r in records:
        drug_counts[r.drug_name.lower().strip()] += 1
    coverage["drug_name"] = dict(sorted(drug_counts.items()))

    return coverage


def identify_gaps(
    records: List[GoldStandardRecord],
    min_per_stratum: int = 5,
) -> List[str]:
    """Identify under-represented strata in the gold standard.

    Args:
        records: Gold-standard records
        min_per_stratum: Minimum required records per stratum value

    Returns:
        Human-readable list of gap descriptions
    """
    coverage = compute_stratum_coverage(records)
    gaps: List[str] = []

    # Check direction balance
    direction_counts = coverage.get("direction", {})
    for expected in ("benefit", "harm", "neutral", "unclear"):
        count = direction_counts.get(expected, 0)
        if count < min_per_stratum:
            gaps.append(
                f"direction='{expected}': {count}/{min_per_stratum} "
                f"(need {min_per_stratum - count} more)"
            )

    # Check model balance
    model_counts = coverage.get("model", {})
    for expected in ("human", "animal", "cell"):
        count = model_counts.get(expected, 0)
        if count < min_per_stratum:
            gaps.append(
                f"model='{expected}': {count}/{min_per_stratum} "
                f"(need {min_per_stratum - count} more)"
            )

    # Check endpoint balance
    endpoint_counts = coverage.get("endpoint", {})
    for expected in ("PLAQUE_IMAGING", "CV_EVENTS", "BIOMARKER", "OTHER"):
        count = endpoint_counts.get(expected.lower(), 0) + endpoint_counts.get(expected, 0)
        if count < min_per_stratum:
            gaps.append(
                f"endpoint='{expected}': {count}/{min_per_stratum} "
                f"(need {min_per_stratum - count} more)"
            )

    # Check drug diversity (at least 10 unique drugs)
    drug_counts = coverage.get("drug_name", {})
    n_drugs = len(drug_counts)
    min_drugs = 10
    if n_drugs < min_drugs:
        gaps.append(
            f"drug diversity: {n_drugs}/{min_drugs} unique drugs "
            f"(need {min_drugs - n_drugs} more)"
        )

    # Check drug dominance (no single drug > 30% of total)
    total = len(records) if records else 1
    for drug, count in drug_counts.items():
        if count / total > 0.30:
            gaps.append(
                f"drug '{drug}' over-represented: {count}/{total} "
                f"({count / total:.0%}, should be <30%)"
            )

    if gaps:
        logger.warning("Gold standard has %d coverage gaps", len(gaps))
    else:
        logger.info("Gold standard coverage is adequate (min=%d per stratum)", min_per_stratum)

    return gaps


def stratified_sample(
    records: List[GoldStandardRecord],
    n: int,
    strata: Optional[Dict[str, List[str]]] = None,
    seed: int = 42,
) -> List[GoldStandardRecord]:
    """Sample n records with stratified balancing across dimensions.

    Ensures each stratum value gets proportional representation,
    preventing over-sampling of dominant categories.

    Args:
        records: Full record pool to sample from
        n: Number of records to sample
        strata: {field: [target_values]} for balancing.
                Default: {"direction": ["benefit", "harm", "neutral", "unclear"]}
        seed: Random seed for reproducibility

    Returns:
        Balanced subset of records (may be < n if pool is insufficient)
    """
    if not records:
        return []

    if strata is None:
        strata = {"direction": ["benefit", "harm", "neutral", "unclear"]}

    rng = random.Random(seed)

    # Group records by primary stratum
    # Use first stratum field for primary bucketing
    primary_field = list(strata.keys())[0]
    target_values = strata[primary_field]

    buckets: Dict[str, List[GoldStandardRecord]] = defaultdict(list)
    overflow: List[GoldStandardRecord] = []

    for r in records:
        val = getattr(r, primary_field, "").lower().strip()
        if val in [v.lower() for v in target_values]:
            buckets[val].append(r)
        else:
            overflow.append(r)

    # Shuffle each bucket
    for bucket in buckets.values():
        rng.shuffle(bucket)
    rng.shuffle(overflow)

    # Allocate proportionally
    n_strata = len(target_values)
    per_stratum = n // n_strata if n_strata > 0 else n
    remainder = n - (per_stratum * n_strata)

    sampled: List[GoldStandardRecord] = []

    for val in target_values:
        val_lower = val.lower()
        bucket = buckets.get(val_lower, [])
        take = min(per_stratum, len(bucket))
        sampled.extend(bucket[:take])

    # Fill remainder from overflow or largest buckets
    remaining_needed = n - len(sampled)
    if remaining_needed > 0 and overflow:
        sampled.extend(overflow[:remaining_needed])
        remaining_needed = n - len(sampled)

    # If still short, take more from the largest buckets
    if remaining_needed > 0:
        for val in target_values:
            val_lower = val.lower()
            bucket = buckets.get(val_lower, [])
            already_taken = per_stratum
            extra = bucket[already_taken:already_taken + remaining_needed]
            sampled.extend(extra)
            remaining_needed -= len(extra)
            if remaining_needed <= 0:
                break

    # Deduplicate by (pmid, drug_name)
    seen = set()
    unique: List[GoldStandardRecord] = []
    for r in sampled:
        key = (r.pmid, r.drug_name)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    logger.info(
        "Stratified sample: requested %d, got %d (from pool of %d)",
        n, len(unique), len(records)
    )
    return unique[:n]


def coverage_report(records: List[GoldStandardRecord]) -> str:
    """Generate a human-readable coverage report.

    Args:
        records: Gold-standard records

    Returns:
        Formatted report string
    """
    coverage = compute_stratum_coverage(records)
    gaps = identify_gaps(records)
    total = len(records)

    lines = [
        "=" * 60,
        "Gold Standard Coverage Report",
        "=" * 60,
        f"Total records: {total}",
        "",
    ]

    for fld, counts in coverage.items():
        lines.append(f"{fld}:")
        for val, count in counts.items():
            pct = count / total * 100 if total > 0 else 0
            lines.append(f"  {val:25s}: {count:4d} ({pct:5.1f}%)")
        lines.append("")

    if gaps:
        lines.append("GAPS (needs attention):")
        for gap in gaps:
            lines.append(f"  - {gap}")
    else:
        lines.append("No coverage gaps detected.")

    lines.append("=" * 60)
    return "\n".join(lines)
