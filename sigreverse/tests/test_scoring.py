"""Unit tests for sigreverse.scoring module.

Tests cover:
    - Direction classification (LDP3 sign convention: both negative = reverser)
    - WTCS-like scoring mode (same-sign coherence gate)
    - Continuous scoring mode
    - Legacy binary scoring mode
    - FDR filtering
    - LDP3 type cross-validation
    - Edge cases (zero values, extreme values)
"""
import pytest
from sigreverse.scoring import (
    compute_signature_score, ScoringMode, _classify_direction,
    _wtcs_like_score, _continuous_score, _legacy_binary_score,
    maybe_flip_z_down,
)


# ===== Direction classification (LDP3 convention) =====

class TestClassifyDirection:
    """LDP3 convention:
        z_up < 0 AND z_down < 0 → reverser (drug reverses disease)
        z_up > 0 AND z_down > 0 → mimicker (drug amplifies disease)
        opposing signs → partial
        zero → orthogonal
    """

    def test_reverser_both_negative(self):
        """Both negative = reverser in LDP3 convention."""
        assert _classify_direction(-3.0, -2.0) == "reverser"

    def test_reverser_strongly_negative(self):
        assert _classify_direction(-10.0, -8.0) == "reverser"

    def test_mimicker_both_positive(self):
        """Both positive = mimicker in LDP3 convention."""
        assert _classify_direction(3.0, 2.0) == "mimicker"

    def test_partial_opposing_signs_up_neg_down_pos(self):
        """z_up<0, z_down>0 → partial (one reversed, one not)."""
        assert _classify_direction(-3.0, 2.0) == "partial"

    def test_partial_opposing_signs_up_pos_down_neg(self):
        """z_up>0, z_down<0 → partial."""
        assert _classify_direction(3.0, -2.0) == "partial"

    def test_orthogonal_zeros(self):
        assert _classify_direction(0.0, 0.0) == "orthogonal"

    def test_orthogonal_one_zero(self):
        # z_up=0 doesn't satisfy any of the strict >0 or <0 conditions → orthogonal
        assert _classify_direction(0.0, 2.0) == "orthogonal"

    def test_orthogonal_other_zero(self):
        assert _classify_direction(-3.0, 0.0) == "orthogonal"


# ===== WTCS-like scoring (LDP3 convention: same-sign gate) =====

class TestWTCSLikeScore:
    def test_reverser_both_negative(self):
        """Both negative → same sign → wtcs = (z_up + z_down)/2 < 0"""
        score, strength = _wtcs_like_score(-4.0, -3.0)
        assert score == pytest.approx((-4.0 + -3.0) / 2.0)  # -3.5
        assert score < 0
        assert strength == pytest.approx(3.5)

    def test_mimicker_both_positive(self):
        """Both positive → same sign → wtcs = (z_up + z_down)/2 > 0"""
        score, strength = _wtcs_like_score(4.0, 3.0)
        assert score == pytest.approx((4.0 + 3.0) / 2.0)  # 3.5
        assert score > 0

    def test_opposing_signs_attenuated(self):
        """z_up<0, z_down>0 → opposing → partial → attenuated score (v2: 0.3x penalty)"""
        score, strength = _wtcs_like_score(-4.0, 3.0)
        # raw = (-4+3)/2 = -0.5, attenuated = -0.5 * 0.3 = -0.15
        assert score == pytest.approx(-0.15)

    def test_opposing_signs_reverse_attenuated(self):
        """z_up>0, z_down<0 → opposing → partial → attenuated score (v2: 0.3x penalty)"""
        score, strength = _wtcs_like_score(4.0, -3.0)
        # raw = (4-3)/2 = 0.5, attenuated = 0.5 * 0.3 = 0.15
        assert score == pytest.approx(0.15)

    def test_zero_zero(self):
        score, strength = _wtcs_like_score(0.0, 0.0)
        assert score == 0.0

    def test_strong_reverser(self):
        """Very negative z-scores should produce very negative score."""
        score, strength = _wtcs_like_score(-10.0, -8.0)
        assert score == pytest.approx(-9.0)
        assert strength == pytest.approx(9.0)

    def test_strength_for_incoherent(self):
        """Incoherent (opposing signs) should still compute diagnostic strength.
        v2: score is attenuated (not zero), strength = abs(raw_wtcs)."""
        score, strength = _wtcs_like_score(-4.0, 3.0)
        assert score == pytest.approx(-0.15)  # v2: attenuated, not zero
        assert strength == pytest.approx(0.5)  # abs((-4+3)/2) = 0.5


# ===== Continuous scoring (LDP3 convention) =====

class TestContinuousScore:
    def test_reverser(self):
        """Both negative → z_up + z_down << 0"""
        score, strength = _continuous_score(-4.0, -3.0)
        assert score == pytest.approx(-7.0)  # -4 + (-3)

    def test_mimicker(self):
        """Both positive → z_up + z_down >> 0"""
        score, strength = _continuous_score(4.0, 3.0)
        assert score == pytest.approx(7.0)  # 4 + 3

    def test_partial_opposing(self):
        """Opposing signs → partially cancel"""
        score, strength = _continuous_score(-4.0, 3.0)
        assert score == pytest.approx(-1.0)  # -4 + 3

    def test_no_signal(self):
        score, strength = _continuous_score(0.0, 0.0)
        assert score == 0.0


# ===== Legacy binary scoring =====

class TestLegacyBinaryScore:
    def test_both_negative_is_reverser(self):
        score, strength = _legacy_binary_score(-4.0, -3.0)
        assert score < 0
        assert strength == pytest.approx(12.0)

    def test_opposing_signs_is_zero(self):
        score, strength = _legacy_binary_score(-4.0, 3.0)
        assert score == 0.0

    def test_both_positive_is_zero(self):
        score, strength = _legacy_binary_score(4.0, 3.0)
        assert score == 0.0


# ===== Full compute_signature_score =====

class TestComputeSignatureScore:
    def test_default_mode_is_wtcs_like(self):
        """Both negative → reverser with negative score."""
        ss = compute_signature_score(-4.0, -3.0)
        assert ss.is_reverser is True
        assert ss.sig_score < 0
        assert ss.direction_category == "reverser"

    def test_mimicker_detected(self):
        """Both positive → mimicker."""
        ss = compute_signature_score(4.0, 3.0)
        assert ss.is_reverser is False
        assert ss.direction_category == "mimicker"
        assert ss.sig_score > 0

    def test_partial_signal_attenuated(self):
        """Opposing signs → partial → attenuated WTCS score (v2: 0.3x penalty)."""
        ss = compute_signature_score(-4.0, 3.0)
        assert ss.is_reverser is False
        assert ss.direction_category == "partial"
        assert ss.sig_score == pytest.approx(-0.15)  # v2: attenuated, not zero

    def test_fdr_pass_both_significant(self):
        ss = compute_signature_score(-4.0, -3.0, fdr_up=0.01, fdr_down=0.02)
        assert ss.fdr_pass is True

    def test_fdr_pass_one_significant(self):
        ss = compute_signature_score(-4.0, -3.0, fdr_up=0.01, fdr_down=0.9)
        assert ss.fdr_pass is True  # at least one < threshold

    def test_fdr_fail_neither_significant(self):
        ss = compute_signature_score(-4.0, -3.0, fdr_up=0.3, fdr_down=0.5)
        assert ss.fdr_pass is False

    def test_fdr_none_passes_by_default(self):
        ss = compute_signature_score(-4.0, -3.0, fdr_up=None, fdr_down=None)
        assert ss.fdr_pass is True  # no FDR data → pass

    def test_ldp3_type_agree_reverser(self):
        """LDP3 says 'reversers', we classify as reverser (both negative) → agree."""
        ss = compute_signature_score(-4.0, -3.0, ldp3_type="reversers")
        assert ss.ldp3_type_agree is True

    def test_ldp3_type_disagree(self):
        """Both positive → mimicker, but LDP3 says reversers → disagree."""
        ss = compute_signature_score(4.0, 3.0, ldp3_type="reversers")
        assert ss.ldp3_type_agree is False

    def test_ldp3_type_agree_mimicker(self):
        """Both positive → mimicker, LDP3 says mimickers → agree."""
        ss = compute_signature_score(4.0, 3.0, ldp3_type="mimickers")
        assert ss.ldp3_type_agree is True

    def test_confidence_weight_from_logp(self):
        ss = compute_signature_score(-4.0, -3.0, logp_fisher=15.0)
        assert ss.confidence_weight == pytest.approx(1.5)  # 15/10 = 1.5

    def test_confidence_weight_capped(self):
        ss = compute_signature_score(-4.0, -3.0, logp_fisher=30.0)
        assert ss.confidence_weight == pytest.approx(2.0)  # capped at 2.0

    def test_continuous_mode(self):
        """Continuous: z_up + z_down for both-negative reverser."""
        ss = compute_signature_score(-4.0, -3.0, mode=ScoringMode.CONTINUOUS)
        assert ss.sig_score == pytest.approx(-7.0)

    def test_continuous_mode_partial(self):
        """Continuous: z_up + z_down, opposing signs → partially cancels."""
        ss = compute_signature_score(-4.0, 3.0, mode=ScoringMode.CONTINUOUS)
        assert ss.sig_score == pytest.approx(-1.0)

    def test_legacy_mode(self):
        ss = compute_signature_score(-4.0, -3.0, mode=ScoringMode.LEGACY_BINARY)
        assert ss.sig_score == pytest.approx(-12.0)


# ===== maybe_flip_z_down =====

class TestMaybeFlipZDown:
    def test_no_flip(self):
        assert maybe_flip_z_down(3.0, False) == 3.0

    def test_flip(self):
        assert maybe_flip_z_down(3.0, True) == -3.0

    def test_flip_negative(self):
        assert maybe_flip_z_down(-3.0, True) == 3.0


# ===== NaN/Inf input validation (v0.4.0) =====

class TestInputValidation:
    def test_nan_z_up_returns_zero(self):
        """NaN z_up should return zero score with 'invalid' category."""
        ss = compute_signature_score(float("nan"), -3.0)
        assert ss.sig_score == 0.0
        assert ss.direction_category == "invalid"
        assert ss.is_reverser is False
        assert ss.confidence_weight == 0.0

    def test_nan_z_down_returns_zero(self):
        ss = compute_signature_score(-3.0, float("nan"))
        assert ss.sig_score == 0.0
        assert ss.direction_category == "invalid"

    def test_inf_z_up_returns_zero(self):
        ss = compute_signature_score(float("inf"), -3.0)
        assert ss.sig_score == 0.0
        assert ss.direction_category == "invalid"

    def test_neg_inf_z_down_returns_zero(self):
        ss = compute_signature_score(-3.0, float("-inf"))
        assert ss.sig_score == 0.0
        assert ss.direction_category == "invalid"

    def test_both_nan_returns_zero(self):
        ss = compute_signature_score(float("nan"), float("nan"))
        assert ss.sig_score == 0.0
        assert ss.direction_category == "invalid"
        assert ss.fdr_pass is False
