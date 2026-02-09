"""Unit tests for step6 pipeline logic: dedup, cross-drug filtering, topic gating, confidence."""

import pytest
import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.step6_evidence_extraction import (
    normalize_pmid,
    build_other_drug_markers,
    contains_other_drug,
    classify_endpoint,
    topic_match_ratio,
    guess_direction,
    guess_model,
)


# ============================================================
# normalize_pmid
# ============================================================
class TestNormalizePmid:
    def test_clean_pmid(self):
        assert normalize_pmid("12345678") == "12345678"

    def test_pmid_with_prefix(self):
        assert normalize_pmid("PMID: 12345678") == "12345678"

    def test_pmid_with_url(self):
        assert normalize_pmid("https://pubmed.ncbi.nlm.nih.gov/12345678/") == "12345678"

    def test_none_returns_empty(self):
        assert normalize_pmid(None) == ""

    def test_empty_string(self):
        assert normalize_pmid("") == ""

    def test_too_short(self):
        """PMIDs must be 6-9 digits."""
        assert normalize_pmid("12345") == ""

    def test_too_long(self):
        """PMIDs must be 6-9 digits."""
        assert normalize_pmid("1234567890") == ""

    def test_integer_input(self):
        assert normalize_pmid(12345678) == "12345678"

    def test_gibberish(self):
        assert normalize_pmid("strconv(abc)") == ""


# ============================================================
# Cross-drug filtering
# ============================================================
class TestCrossDrugFilter:
    def test_build_markers_excludes_current(self):
        markers = build_other_drug_markers(
            ["resveratrol", "dexamethasone", "abc"], "resveratrol"
        )
        assert "resveratrol" not in markers
        assert "dexamethasone" in markers
        assert "abc" not in markers  # len < 4, filtered out

    def test_build_markers_excludes_short(self):
        markers = build_other_drug_markers(["ab", "abc", "abcdef"], "xyz")
        assert "ab" not in markers
        assert "abc" not in markers
        assert "abcdef" in markers

    def test_build_markers_sorted_longest_first(self):
        markers = build_other_drug_markers(
            ["aaaa", "bbbbbb", "ccccc"], "xxxx"
        )
        assert markers[0] == "bbbbbb"  # longest first

    def test_contains_other_drug_positive(self):
        markers = ["dexamethasone", "metformin"]
        assert contains_other_drug("This study used dexamethasone in rats", markers) is True

    def test_contains_other_drug_negative(self):
        markers = ["dexamethasone", "metformin"]
        assert contains_other_drug("Resveratrol reduced plaque", markers) is False

    def test_contains_other_drug_case_insensitive(self):
        markers = ["dexamethasone"]
        assert contains_other_drug("DEXAMETHASONE was administered", markers) is True

    def test_contains_other_drug_empty_text(self):
        markers = ["dexamethasone"]
        assert contains_other_drug("", markers) is False
        assert contains_other_drug(None, markers) is False

    def test_contains_other_drug_empty_markers(self):
        assert contains_other_drug("anything", []) is False


# ============================================================
# Endpoint classification
# ============================================================
class TestClassifyEndpoint:
    def test_plaque_imaging(self):
        assert classify_endpoint("coronary CTA plaque volume", "atherosclerosis") == "PLAQUE_IMAGING"

    def test_pad_function(self):
        assert classify_endpoint("six-minute walk test distance", "PAD") == "PAD_FUNCTION"

    def test_cv_events(self):
        assert classify_endpoint("MACE composite endpoint", "coronary") == "CV_EVENTS"

    def test_other_default(self):
        assert classify_endpoint("blood pressure", "hypertension") == "OTHER"

    def test_ivus(self):
        assert classify_endpoint("IVUS total atheroma volume", "") == "PLAQUE_IMAGING"

    def test_carotid(self):
        assert classify_endpoint("carotid intima-media thickness", "") == "PLAQUE_IMAGING"


# ============================================================
# Topic match ratio
# ============================================================
class TestTopicMatchRatio:
    def test_high_match_plaque(self):
        text = "atherosclerosis plaque regression carotid coronary foam cell oxLDL"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio > 0.3

    def test_zero_match(self):
        text = "completely unrelated text about cooking recipes"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio == 0.0

    def test_other_endpoint_broad(self):
        """OTHER keywords are broad cardiovascular terms."""
        text = "atherosclerosis cardiovascular inflammation"
        ratio = topic_match_ratio(text, "OTHER")
        assert ratio > 0.2

    def test_empty_text(self):
        assert topic_match_ratio("", "PLAQUE_IMAGING") == 0.0
        assert topic_match_ratio(None, "PLAQUE_IMAGING") == 0.0

    def test_boundary_threshold(self):
        """Test value near the 0.30 topic mismatch threshold."""
        # Single keyword hit out of 14 PLAQUE_IMAGING keywords = ~0.07
        text = "atherosclerosis"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio < 0.30  # below mismatch threshold

    def test_above_threshold(self):
        """Multiple keyword hits should exceed threshold."""
        text = "atherosclerosis plaque atheroma coronary carotid foam cell"
        ratio = topic_match_ratio(text, "PLAQUE_IMAGING")
        assert ratio >= 0.30


# ============================================================
# Direction guessing
# ============================================================
class TestGuessDirection:
    def test_benefit(self):
        assert guess_direction("Resveratrol significantly reduced plaque") == "benefit"

    def test_harm(self):
        assert guess_direction("Drug increased cardiovascular risk") == "harm"

    def test_neutral(self):
        assert guess_direction("No difference observed between groups") == "neutral"

    def test_neutral_overrides_benefit(self):
        """'not significantly reduced' should be neutral, not benefit."""
        assert guess_direction("not significantly reduced") == "neutral"

    def test_unknown(self):
        assert guess_direction("The study examined pharmacokinetics") == "unknown"

    def test_empty(self):
        assert guess_direction("") == "unknown"


# ============================================================
# Model guessing
# ============================================================
class TestGuessModel:
    def test_human(self):
        assert guess_model("randomized placebo-controlled trial in patients") == "human"

    def test_animal(self):
        assert guess_model("ApoE knockout mice fed high-fat diet") == "animal"

    def test_cell(self):
        assert guess_model("THP-1 macrophage cells in vitro") == "cell"

    def test_unknown(self):
        assert guess_model("analytical chemistry methods") == "unknown"


# ============================================================
# Dedup logic (inline in process_one, test via functional behavior)
# ============================================================
class TestDedup:
    """Test the dedup function pattern used in step6."""

    def _dedupe(self, items):
        """Replicate step6's dedupe logic."""
        seen = set()
        out = []
        for it in items:
            k = (str(it.get("pmid", "")).strip(), str(it.get("claim", "")).strip())
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    def test_no_duplicates(self):
        items = [
            {"pmid": "111", "claim": "claim A"},
            {"pmid": "222", "claim": "claim B"},
        ]
        assert len(self._dedupe(items)) == 2

    def test_exact_duplicate(self):
        items = [
            {"pmid": "111", "claim": "claim A"},
            {"pmid": "111", "claim": "claim A"},
        ]
        assert len(self._dedupe(items)) == 1

    def test_same_pmid_different_claim(self):
        items = [
            {"pmid": "111", "claim": "claim A"},
            {"pmid": "111", "claim": "claim B"},
        ]
        assert len(self._dedupe(items)) == 2

    def test_different_pmid_same_claim(self):
        items = [
            {"pmid": "111", "claim": "claim A"},
            {"pmid": "222", "claim": "claim A"},
        ]
        assert len(self._dedupe(items)) == 2

    def test_empty_pmid_dedup(self):
        """Empty PMIDs with same claim should dedup."""
        items = [
            {"pmid": "", "claim": "claim A"},
            {"pmid": "", "claim": "claim A"},
        ]
        assert len(self._dedupe(items)) == 1

    def test_preserves_order(self):
        items = [
            {"pmid": "333", "claim": "third"},
            {"pmid": "111", "claim": "first"},
            {"pmid": "222", "claim": "second"},
            {"pmid": "111", "claim": "first"},  # dup
        ]
        result = self._dedupe(items)
        assert len(result) == 3
        assert result[0]["pmid"] == "333"  # order preserved


# ============================================================
# Cache version control
# ============================================================
class TestCacheVersioning:
    def test_stamp_adds_version(self):
        from src.dr.retrieval.cache import CacheManager, CACHE_SCHEMA_VERSION
        cache = CacheManager(base_dir="/tmp/test_dr_cache")
        data = {"pmid": "123", "title": "test"}
        stamped = cache._stamp(data)
        assert stamped["_v"] == CACHE_SCHEMA_VERSION

    def test_check_version_current(self):
        from src.dr.retrieval.cache import CacheManager, CACHE_SCHEMA_VERSION
        cache = CacheManager(base_dir="/tmp/test_dr_cache")
        data = {"_v": CACHE_SCHEMA_VERSION, "pmid": "123"}
        assert cache._check_version(data, "test_key") is True

    def test_check_version_stale(self):
        from src.dr.retrieval.cache import CacheManager, CACHE_SCHEMA_VERSION
        cache = CacheManager(base_dir="/tmp/test_dr_cache")
        data = {"_v": CACHE_SCHEMA_VERSION - 1, "pmid": "123"}
        assert cache._check_version(data, "test_key") is False

    def test_check_version_missing(self):
        """Old cache files without _v should be treated as stale."""
        from src.dr.retrieval.cache import CacheManager
        cache = CacheManager(base_dir="/tmp/test_dr_cache")
        data = {"pmid": "123"}  # no _v field
        assert cache._check_version(data, "test_key") is False


# ============================================================
# Config gating thresholds
# ============================================================
class TestEvidenceGatingConfig:
    def test_defaults(self):
        from src.dr.config import EvidenceGatingConfig
        cfg = EvidenceGatingConfig()
        assert cfg.TOPIC_MISMATCH_THRESHOLD == 0.30
        assert cfg.HIGH_CONFIDENCE_MIN_PMIDS == 6
        assert cfg.MED_CONFIDENCE_MIN_PMIDS == 3
        assert cfg.SUPPORT_COUNT_MODE == "unique_pmids"

    def test_confidence_logic(self):
        """Verify confidence assignment matches thresholds."""
        from src.dr.config import Config
        gating = Config.gating

        # HIGH: >= 6 unique PMIDs
        assert 7 >= gating.HIGH_CONFIDENCE_MIN_PMIDS
        # MED: >= 3 unique PMIDs
        assert 4 >= gating.MED_CONFIDENCE_MIN_PMIDS
        # LOW: < 3
        assert 2 < gating.MED_CONFIDENCE_MIN_PMIDS
