"""Signature-level scoring module — industrial grade.

Implements continuous directional scoring aligned with CMap WTCS principles,
replacing the original binary reverser/not classification.

Improvements (v0.4.0):
    - NaN/Inf input validation with safe fallback
    - z-score clipping to prevent numerical overflow
    - Detailed warnings on invalid inputs

LDP3 z-score sign convention (verified from actual API output):
    - z-up < 0: disease-UP genes are DOWN-regulated by drug (reversed)
    - z-down < 0: disease-DOWN genes are UP-regulated by drug (reversed)
    - Therefore: REVERSER = z_up < 0 AND z_down < 0 (BOTH negative)
    - MIMICKER = z_up > 0 AND z_down > 0 (BOTH positive)
    - LDP3 'direction-down' field = 1 for reversers confirms internal flip

Scoring modes:
    - wtcs_like: Continuous score using z_up + z_down (both negative = strong reversal).
      Includes sign-coherence gate from CMap WTCS principle.
    - continuous: Simple additive z_up + z_down for continuous ranking.
    - legacy_binary: Original binary reverser / not (kept for comparison).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("sigreverse.scoring")

# Maximum absolute z-score to prevent numerical issues
_Z_CLIP = 50.0


class ScoringMode(str, Enum):
    WTCS_LIKE = "wtcs_like"
    CONTINUOUS = "continuous"
    LEGACY_BINARY = "legacy_binary"


@dataclass
class SignatureScore:
    """Result of scoring a single LINCS signature against a disease query.

    Attributes:
        is_reverser: True if the signature reverses the disease direction.
        sig_score: Continuous score (more negative = stronger reversal).
        sig_strength: Absolute magnitude of the directional signal.
        fdr_pass: Whether the signature passes FDR significance filter.
        ldp3_type_agree: Whether our classification agrees with LDP3 'type' field.
        confidence_weight: Combined weight from FDR and Fisher p-value.
        direction_category: 'reverser' | 'mimicker' | 'partial' | 'orthogonal'.
    """
    is_reverser: bool
    sig_score: float
    sig_strength: float
    fdr_pass: bool = True
    ldp3_type_agree: Optional[bool] = None
    confidence_weight: float = 1.0
    direction_category: str = "unknown"


def compute_signature_score(
    z_up: float,
    z_down: float,
    mode: ScoringMode = ScoringMode.WTCS_LIKE,
    fdr_up: Optional[float] = None,
    fdr_down: Optional[float] = None,
    fdr_threshold: float = 0.05,
    logp_fisher: Optional[float] = None,
    ldp3_type: Optional[str] = None,
) -> SignatureScore:
    """Score a single signature against a disease query.

    WTCS-like mode (default, recommended):
        Inspired by CMap's WTCS, adapted for LDP3 sign convention.
        LDP3 z-scores: z-up < 0 = disease-up genes reversed,
                        z-down < 0 = disease-down genes reversed.
        Reverser = both negative. Mimicker = both positive.

        if sign(z_up) == sign(z_down):
            wtcs = (z_up + z_down) / 2   # negative for reverser, positive for mimicker
        else:
            wtcs = 0                       # incoherent signal → no score

    Continuous mode:
        sig_score = z_up + z_down
        Captures overall reversal tendency without the sign-check gate.

    Legacy binary mode:
        Original binary classification (z_up<0 AND z_down<0) for backward compatibility.

    Args:
        z_up: LDP3 z-score for disease up-regulated genes.
        z_down: LDP3 z-score for disease down-regulated genes.
        mode: Scoring mode to use.
        fdr_up: FDR for up direction (from LDP3 'fdr-up').
        fdr_down: FDR for down direction (from LDP3 'fdr-down').
        fdr_threshold: Max FDR for a direction to be considered significant.
        logp_fisher: Fisher combined log p-value (from LDP3 'logp-fisher').
        ldp3_type: LDP3's own classification ('reversers' or 'mimickers').

    Returns:
        SignatureScore with continuous score and metadata.
    """
    # --- Input validation: NaN/Inf guard ---
    if not math.isfinite(z_up) or not math.isfinite(z_down):
        logger.warning(f"Non-finite z-scores detected: z_up={z_up}, z_down={z_down}. Returning zero score.")
        return SignatureScore(
            is_reverser=False,
            sig_score=0.0,
            sig_strength=0.0,
            fdr_pass=False,
            ldp3_type_agree=None,
            confidence_weight=0.0,
            direction_category="invalid",
        )

    # Clip extreme z-scores to prevent overflow
    z_up = max(-_Z_CLIP, min(_Z_CLIP, z_up))
    z_down = max(-_Z_CLIP, min(_Z_CLIP, z_down))

    # --- FDR significance filter ---
    fdr_pass = True
    if fdr_up is not None and fdr_down is not None:
        # At least one direction must be significant
        fdr_pass = (fdr_up < fdr_threshold) or (fdr_down < fdr_threshold)

    # --- Confidence weight from Fisher p-value ---
    confidence_weight = 1.0
    if logp_fisher is not None and logp_fisher > 0:
        # logp_fisher is -log10(p), higher = more significant
        # Normalize: cap contribution, diminishing returns above logp=10
        confidence_weight = min(logp_fisher / 10.0, 2.0)

    # --- Direction classification ---
    direction_category = _classify_direction(z_up, z_down)
    is_reverser = direction_category == "reverser"

    # --- Core scoring ---
    if mode == ScoringMode.WTCS_LIKE:
        sig_score, sig_strength = _wtcs_like_score(z_up, z_down)
    elif mode == ScoringMode.CONTINUOUS:
        sig_score, sig_strength = _continuous_score(z_up, z_down)
    elif mode == ScoringMode.LEGACY_BINARY:
        sig_score, sig_strength = _legacy_binary_score(z_up, z_down)
    else:
        raise ValueError(f"Unknown scoring mode: {mode}")

    # --- LDP3 cross-validation ---
    ldp3_type_agree = None
    if ldp3_type is not None:
        ldp3_says_reverser = ldp3_type.strip().lower() == "reversers"
        ldp3_type_agree = (is_reverser == ldp3_says_reverser)

    return SignatureScore(
        is_reverser=is_reverser,
        sig_score=sig_score,
        sig_strength=sig_strength,
        fdr_pass=fdr_pass,
        ldp3_type_agree=ldp3_type_agree,
        confidence_weight=confidence_weight,
        direction_category=direction_category,
    )


def _classify_direction(z_up: float, z_down: float) -> str:
    """Classify the directional relationship between drug and disease signatures.

    LDP3 z-score sign convention (verified from actual API output):
        - z-up < 0: disease-UP genes are DOWN-regulated by drug (reversed)
        - z-down < 0: disease-DOWN genes are UP-regulated by drug (reversed)
        - LDP3 'direction-down' field = 1 for reversers confirms internal flip

    Categories:
        reverser:    z_up < 0 AND z_down < 0  (drug reverses both directions)
        mimicker:    z_up > 0 AND z_down > 0  (drug amplifies both directions)
        partial:     opposing signs (one direction reversed, other not)
        orthogonal:  one or both near zero, no clear pattern
    """
    if z_up < 0 and z_down < 0:
        return "reverser"
    elif z_up > 0 and z_down > 0:
        return "mimicker"
    elif (z_up < 0 and z_down > 0) or (z_up > 0 and z_down < 0):
        # One direction reversed, other not → partial/mixed signal
        return "partial"
    else:
        return "orthogonal"


def _wtcs_like_score(z_up: float, z_down: float) -> tuple[float, float]:
    """WTCS-inspired scoring adapted for LDP3 sign convention.

    LDP3 sign convention (verified from actual API output):
        - z-up < 0: disease-UP genes are DOWN-regulated by drug (reversed)
        - z-down < 0: disease-DOWN genes are UP-regulated by drug (reversed)
        - Both negative = REVERSER, both positive = MIMICKER

    CMap WTCS principle adapted for LDP3:
        Only score when z_up and z_down have the SAME sign (coherent signal).
        This prevents single-direction enrichment from producing false positives.

    Scoring formula:
        - Same sign (coherent): wtcs = (z_up + z_down) / 2
          · Reverser: both < 0 → wtcs < 0 (more negative = stronger reversal)
          · Mimicker: both > 0 → wtcs > 0
        - Opposing signs (incoherent): wtcs = 0

    Returns:
        (sig_score, sig_strength)
    """
    sign_up = 1 if z_up >= 0 else -1
    sign_down = 1 if z_down >= 0 else -1

    if sign_up == sign_down and (z_up != 0 or z_down != 0):
        # Same sign → coherent directional signal
        wtcs = (z_up + z_down) / 2.0
        strength = abs(wtcs)
        return wtcs, strength
    else:
        # Opposing signs or both zero → incoherent → zero score
        # But still compute potential strength for diagnostics
        strength = abs(z_up - z_down) / 2.0
        return 0.0, strength


def _continuous_score(z_up: float, z_down: float) -> tuple[float, float]:
    """Simple continuous score without sign-check gate.

    LDP3 sign convention:
        - z-up < 0 AND z-down < 0 = REVERSER (both negative)
        - sig_score = z_up + z_down
        - For reverser: both < 0 → sig_score << 0 → good (more negative = better)
        - For mimicker: both > 0 → sig_score >> 0

    No sign-coherence gate, so partial signals also get scored.

    Returns:
        (sig_score, sig_strength)
    """
    sig_score = z_up + z_down
    strength = abs(sig_score)
    return sig_score, strength


def _legacy_binary_score(z_up: float, z_down: float) -> tuple[float, float]:
    """Original binary scoring for backward compatibility.

    Original definition: reverser if z_up < 0 AND z_down < 0.
    This was based on LDP3's convention where both z-scores being negative
    indicates disease signature is reversed.
    """
    strength = abs(z_up * z_down)
    if z_up < 0 and z_down < 0:
        return -strength, strength
    return 0.0, strength


def maybe_flip_z_down(z_down: float, flip: bool) -> float:
    """Optionally flip z_down sign.

    Some LDP3 API versions return z-down with inverted sign convention.
    Verify with your specific API version before enabling.
    """
    return -z_down if flip else z_down
