"""FusionRanker module — integrate SigReverse scores with KG_Explain paths.

Implements multi-source evidence fusion for drug repurposing:
    1. Signature score (from SigReverse WTCS/Tau pipeline)
    2. Knowledge graph path score (from KG_Explain DTPD paths)
    3. FAERS safety signal (optional, from KG_Explain safety module)
    4. Dose-response quality bonus
    5. Literature co-occurrence boost

Final FusionScore:
    fusion = w_sig * norm(sig_score) + w_kg * norm(kg_score)
             + w_safety * norm(safety_score) + w_dr * dr_bonus
             + w_lit * lit_boost

Where norm() standardizes each component to [0, 1] range.

Architecture:
    - EvidenceSource: abstract base for pluggable evidence streams
    - SignatureEvidence: wraps SigReverse drug-level scores
    - KGExplainEvidence: wraps KG_Explain DTPD path scores
    - FusionRanker: weighted combination with configurable weights

References:
    - RankAggreg: weighted Borda count / Stuart method
    - CMap + KG fusion: Zeng et al. 2022, Nature Computational Science
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.fusion")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FusionScore:
    """Final fused drug ranking score."""
    drug: str
    fusion_score: float             # Combined score (lower = better candidate)
    rank: int = 0
    sig_score_norm: float = 0.0     # Normalized signature score [0,1]
    kg_score_norm: float = 0.0      # Normalized KG path score [0,1]
    safety_score_norm: float = 0.0  # Normalized safety score [0,1]
    dr_bonus: float = 0.0           # Dose-response quality bonus
    lit_boost: float = 0.0          # Literature co-occurrence boost
    evidence_sources: int = 0       # Number of evidence sources with data
    confidence: str = "low"         # low | medium | high


# ---------------------------------------------------------------------------
# Evidence sources (abstract)
# ---------------------------------------------------------------------------

class EvidenceSource(ABC):
    """Abstract base for pluggable evidence streams."""

    @abstractmethod
    def get_scores(self) -> Dict[str, float]:
        """Return drug → raw score mapping."""
        ...

    @abstractmethod
    def source_name(self) -> str:
        ...

    def lower_is_better(self) -> bool:
        """Whether lower raw scores indicate better candidates.

        Override in subclasses to declare the score direction.
        Default: True (lower = better, as in SigReverse reversal scores).
        """
        return True


class SignatureEvidence(EvidenceSource):
    """Evidence from SigReverse signature reversal scores.

    Expects a drug-level DataFrame with 'drug' and 'final_reversal_score' columns.
    More negative = stronger reversal = better candidate.
    """

    def __init__(self, df_drug: pd.DataFrame, score_col: str = "final_reversal_score"):
        self.scores = {}
        if df_drug is not None and len(df_drug) > 0:
            for _, row in df_drug.iterrows():
                drug = str(row.get("drug", ""))
                score = float(row.get(score_col, 0.0))
                if drug:
                    self.scores[drug] = score

    def get_scores(self) -> Dict[str, float]:
        return self.scores

    def source_name(self) -> str:
        return "SigReverse"


class KGExplainEvidence(EvidenceSource):
    """Evidence from KG_Explain knowledge graph path scores.

    Accepts KG_Explain V5 output directly (drug_normalized + final_score columns)
    or a pre-processed DataFrame with custom column names.

    When multiple rows exist per drug (drug-disease level data), automatically
    aggregates to drug level using max(score).

    Integration with KG_Explain V5:
        - DTPD (Drug → Target → Pathway → Disease) path scoring
        - Edge confidence weighting
        - Phenotype boost from phenotype-annotated edges

    Note: KG_Explain V5 final_score is HIGHER = BETTER (mechanism_score * penalties
    * phenotype_multiplier), unlike SigReverse where lower = better.
    """

    def __init__(
        self,
        df_kg: Optional[pd.DataFrame] = None,
        csv_path: Optional[str] = None,
        drug_col: str = "drug",
        score_col: str = "final_score",
        disease_filter: Optional[str] = None,
        disease_col: str = "diseaseName",
    ):
        self.scores = {}
        # KG_Explain v5 final_score: higher is better; legacy kg_score: lower is better.
        self._lower_is_better = False
        df = df_kg
        if df is None and csv_path:
            try:
                df = pd.read_csv(csv_path)
            except Exception as e:
                logger.warning(f"Failed to load KG scores from {csv_path}: {e}")
                return

        if df is None or len(df) == 0:
            return

        # Auto-detect drug column: prefer drug_col, fallback to drug_normalized
        if drug_col not in df.columns and "drug_normalized" in df.columns:
            drug_col = "drug_normalized"
            logger.info("KGExplainEvidence: using 'drug_normalized' column")

        if drug_col not in df.columns:
            logger.warning(f"KGExplainEvidence: column '{drug_col}' not found, "
                           f"available: {list(df.columns)}")
            return

        # Auto-detect score column for backward compatibility.
        score_candidates = [score_col, "final_score", "kg_score", "mechanism_score", "score"]
        resolved_score_col = next((c for c in score_candidates if c in df.columns), None)
        if resolved_score_col is None:
            logger.warning(
                f"KGExplainEvidence: score column '{score_col}' not found, "
                f"available: {list(df.columns)}"
            )
            return
        if resolved_score_col != score_col:
            logger.info(
                "KGExplainEvidence: score column '%s' not found, fallback to '%s'",
                score_col, resolved_score_col,
            )

        # Score direction by schema:
        # - final_score/mechanism_score/score: higher is better (default)
        # - kg_score (legacy): lower is better
        if resolved_score_col == "kg_score":
            self._lower_is_better = True

        # Optional disease filtering
        if disease_filter and disease_col in df.columns:
            import re
            pattern = re.escape(str(disease_filter).lower())
            mask = df[disease_col].astype(str).str.lower().str.contains(pattern, na=False)
            df = df[mask]
            logger.info(f"KGExplainEvidence: filtered to {len(df)} rows "
                        f"matching disease '{disease_filter}'")

        # Numeric coercion guard
        df = df.copy()
        df[resolved_score_col] = pd.to_numeric(df[resolved_score_col], errors="coerce")
        df = df[df[resolved_score_col].notna()]
        if len(df) == 0:
            logger.warning("KGExplainEvidence: no valid numeric scores in column '%s'", resolved_score_col)
            return

        # Aggregate to drug level.
        # v5 final_score (higher is better): max
        # legacy kg_score (lower is better): min
        for drug, group in df.groupby(drug_col, dropna=True):
            drug_str = str(drug).strip()
            if drug_str:
                if self._lower_is_better:
                    self.scores[drug_str] = float(group[resolved_score_col].min())
                else:
                    self.scores[drug_str] = float(group[resolved_score_col].max())

    def get_scores(self) -> Dict[str, float]:
        return self.scores

    def source_name(self) -> str:
        return "KG_Explain"

    def lower_is_better(self) -> bool:
        """Declare score direction for normalization."""
        return self._lower_is_better


class SafetyEvidence(EvidenceSource):
    """Evidence from FAERS safety signal (from KG_Explain safety module).

    Higher safety_score = more adverse events = worse candidate.
    This score acts as a penalty in the fusion.

    Accepts two formats:
        1. Pre-aggregated: drug + safety_score (one row per drug)
        2. KG_Explain FAERS: drug_normalized + report_count + prr (per-AE rows)
           Auto-aggregates to drug level using log(total_reports + 1).
    """

    def __init__(
        self,
        df_safety: Optional[pd.DataFrame] = None,
        drug_col: str = "drug",
        score_col: str = "safety_score",
    ):
        self.scores = {}
        if df_safety is None or len(df_safety) == 0:
            return

        # Auto-detect drug column
        if drug_col not in df_safety.columns and "drug_normalized" in df_safety.columns:
            drug_col = "drug_normalized"

        if drug_col not in df_safety.columns:
            logger.warning(f"SafetyEvidence: column '{drug_col}' not found")
            return

        # Detect format: pre-aggregated vs KG_Explain FAERS per-AE rows
        if score_col in df_safety.columns:
            # Pre-aggregated format
            for drug, group in df_safety.groupby(drug_col, dropna=True):
                drug_str = str(drug).strip()
                if drug_str:
                    self.scores[drug_str] = float(group[score_col].max())
        elif "report_count" in df_safety.columns:
            # KG_Explain FAERS format: aggregate total reports per drug
            for drug, group in df_safety.groupby(drug_col, dropna=True):
                drug_str = str(drug).strip()
                if drug_str:
                    total_reports = group["report_count"].sum()
                    self.scores[drug_str] = math.log(total_reports + 1)
            logger.info(f"SafetyEvidence: aggregated FAERS data for "
                        f"{len(self.scores)} drugs")
        else:
            logger.warning(f"SafetyEvidence: neither '{score_col}' nor "
                           f"'report_count' column found")

    def get_scores(self) -> Dict[str, float]:
        return self.scores

    def source_name(self) -> str:
        return "FAERS_Safety"

    def lower_is_better(self) -> bool:
        """FAERS Safety: LOWER safety score = fewer adverse events = better."""
        return True


# ---------------------------------------------------------------------------
# Normalization utilities
# ---------------------------------------------------------------------------

def min_max_normalize(
    scores: Dict[str, float],
    lower_is_better: bool = True,
) -> Dict[str, float]:
    """Normalize scores to [0, 1] range.

    Args:
        scores: Drug → raw score mapping.
        lower_is_better: If True, lowest raw score maps to 0 (best).

    Returns:
        Normalized scores in [0, 1], where 0 = best.
    """
    if not scores:
        return {}

    values = list(scores.values())
    vmin, vmax = min(values), max(values)

    if vmax - vmin < 1e-12:
        return {d: 0.5 for d in scores}

    normalized = {}
    for drug, val in scores.items():
        normed = (val - vmin) / (vmax - vmin)
        if lower_is_better:
            normalized[drug] = normed  # 0 = best (lowest raw)
        else:
            normalized[drug] = 1.0 - normed  # 0 = best (highest raw)

    return normalized


def rank_normalize(scores: Dict[str, float], lower_is_better: bool = True) -> Dict[str, float]:
    """Normalize scores by rank (percentile-based).

    More robust than min-max for outlier-heavy distributions.
    Returns percentile rank in [0, 1] where 0 = best.
    """
    if not scores:
        return {}

    items = sorted(scores.items(), key=lambda x: x[1], reverse=not lower_is_better)
    n = len(items)
    return {drug: i / max(n - 1, 1) for i, (drug, _) in enumerate(items)}


# ---------------------------------------------------------------------------
# FusionRanker
# ---------------------------------------------------------------------------

class FusionRanker:
    """Weighted multi-source evidence fusion for drug ranking.

    Usage:
        ranker = FusionRanker(
            weights={"signature": 0.5, "kg": 0.3, "safety": 0.1, "dose_response": 0.1}
        )
        ranker.add_evidence(SignatureEvidence(df_drug))
        ranker.add_evidence(KGExplainEvidence(df_kg))
        results = ranker.fuse()
        df_fusion = ranker.to_dataframe()
    """

    DEFAULT_WEIGHTS = {
        "signature": 0.50,       # CMap reversal signal
        "kg": 0.30,              # KG path confidence
        "safety": 0.10,          # FAERS safety penalty
        "dose_response": 0.05,   # Dose-response quality bonus
        "literature": 0.05,      # Literature co-occurrence
    }

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        normalization: str = "rank",  # "rank" or "minmax"
    ):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self.normalization = normalization
        self.evidence_sources: Dict[str, EvidenceSource] = {}
        self.dose_response_data: Optional[pd.DataFrame] = None
        self.literature_data: Optional[Dict[str, float]] = None
        self.results: List[FusionScore] = []

    def add_evidence(self, source: EvidenceSource):
        """Add an evidence source."""
        name = source.source_name()
        self.evidence_sources[name] = source
        logger.info(
            f"Added evidence source: {name} ({len(source.get_scores())} drugs)"
        )

    def set_dose_response(self, df_dr: pd.DataFrame):
        """Set dose-response data for quality bonus."""
        self.dose_response_data = df_dr

    def set_literature_scores(self, lit_scores: Dict[str, float]):
        """Set literature co-occurrence scores."""
        self.literature_data = lit_scores

    def fuse(self) -> List[FusionScore]:
        """Execute the fusion ranking.

        Steps:
            1. Collect all unique drugs across evidence sources
            2. Normalize each evidence source to [0,1]
            3. Compute weighted sum
            4. Add bonuses (dose-response, literature)
            5. Rank by fusion score
        """
        # Collect all drugs
        all_drugs: set = set()
        raw_scores: Dict[str, Dict[str, float]] = {}

        for name, source in self.evidence_sources.items():
            scores = source.get_scores()
            all_drugs.update(scores.keys())
            raw_scores[name] = scores

        if not all_drugs:
            logger.warning("No drugs found across evidence sources")
            return []

        # Normalize each source (using source's own lower_is_better declaration)
        norm_fn = rank_normalize if self.normalization == "rank" else min_max_normalize
        normalized: Dict[str, Dict[str, float]] = {}

        for name, scores in raw_scores.items():
            # Use the source's declared score direction
            source = self.evidence_sources.get(name)
            lib = source.lower_is_better() if source else True
            normalized[name] = norm_fn(scores, lower_is_better=lib)

        # Weight mapping: source name → weight key
        source_weight_map = {
            "SigReverse": "signature",
            "KG_Explain": "kg",
            "FAERS_Safety": "safety",
        }

        # Compute fusion scores
        results = []
        for drug in sorted(all_drugs):
            sig_norm = normalized.get("SigReverse", {}).get(drug, 0.5)
            kg_norm = normalized.get("KG_Explain", {}).get(drug, 0.5)
            safety_norm = normalized.get("FAERS_Safety", {}).get(drug, 0.5)

            # Dose-response bonus
            dr_bonus = 0.0
            if self.dose_response_data is not None and "drug" in self.dose_response_data.columns:
                dr_row = self.dose_response_data[
                    self.dose_response_data["drug"] == drug
                ]
                if len(dr_row) > 0:
                    quality = dr_row.iloc[0]["dr_quality"] if "dr_quality" in dr_row.columns else "insufficient"
                    dr_bonus = {
                        "excellent": -0.2,
                        "good": -0.1,
                        "marginal": -0.05,
                        "poor": 0.0,
                        "insufficient": 0.0,
                    }.get(quality, 0.0)

            # Literature boost
            lit_boost = 0.0
            if self.literature_data and drug in self.literature_data:
                # Higher lit score → bigger boost (negative = better)
                lit_boost = -min(self.literature_data[drug], 1.0) * 0.2

            # Weighted sum
            # Note: dr_bonus and lit_boost are already scaled offsets (e.g. -0.2),
            # so we add them directly rather than multiplying by a tiny weight.
            w = self.weights
            fusion = (
                w.get("signature", 0) * sig_norm
                + w.get("kg", 0) * kg_norm
                + w.get("safety", 0) * safety_norm
                + dr_bonus
                + lit_boost
            )

            # Count evidence sources with actual data
            n_sources = sum(
                1 for name in raw_scores if drug in raw_scores[name]
            )

            # Confidence level
            if n_sources >= 3:
                confidence = "high"
            elif n_sources >= 2:
                confidence = "medium"
            else:
                confidence = "low"

            results.append(FusionScore(
                drug=drug,
                fusion_score=fusion,
                sig_score_norm=sig_norm,
                kg_score_norm=kg_norm,
                safety_score_norm=safety_norm,
                dr_bonus=dr_bonus,
                lit_boost=lit_boost,
                evidence_sources=n_sources,
                confidence=confidence,
            ))

        # Sort by fusion score (lower = better)
        results.sort(key=lambda x: x.fusion_score)
        for i, r in enumerate(results):
            r.rank = i + 1

        self.results = results
        logger.info(
            f"Fusion ranking: {len(results)} drugs, "
            f"{sum(1 for r in results if r.confidence == 'high')} high-confidence"
        )
        return results

    def to_dataframe(self) -> pd.DataFrame:
        """Convert fusion results to DataFrame."""
        if not self.results:
            return pd.DataFrame()

        rows = []
        for r in self.results:
            rows.append({
                "rank": r.rank,
                "drug": r.drug,
                "fusion_score": r.fusion_score,
                "sig_score_norm": r.sig_score_norm,
                "kg_score_norm": r.kg_score_norm,
                "safety_score_norm": r.safety_score_norm,
                "dr_bonus": r.dr_bonus,
                "lit_boost": r.lit_boost,
                "evidence_sources": r.evidence_sources,
                "confidence": r.confidence,
            })
        return pd.DataFrame(rows)
