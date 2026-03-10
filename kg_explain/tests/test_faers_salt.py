"""Tests for FAERS salt-form query fallback logic.

Verifies:
    - _strip_salt_form correctly strips common salt suffixes (citrate, hydrochloride, etc.)
    - _strip_salt_form handles hydrates (monohydrate, dihydrate, etc.)
    - _strip_salt_form is a no-op for non-salt drug names
    - _faers_drug_ae falls back to stripped parent name when salt form returns no results
    - _faers_drug_ae does NOT double-query when the drug name has no salt suffix
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from kg_explain.datasources.faers import _strip_salt_form, _faers_drug_ae


# ===================================================================
# _strip_salt_form: unit tests for the stripping function
# ===================================================================


class TestStripSaltForm:
    """Verify salt-form suffix removal for common pharmaceutical salt names."""

    # -- basic salt suffixes --

    def test_citrate(self):
        assert _strip_salt_form("tofacitinib citrate") == "tofacitinib"

    def test_hydrochloride(self):
        assert _strip_salt_form("sertraline hydrochloride") == "sertraline"

    def test_maleate(self):
        assert _strip_salt_form("enalapril maleate") == "enalapril"

    def test_sodium(self):
        assert _strip_salt_form("diclofenac sodium") == "diclofenac"

    def test_sulfate(self):
        assert _strip_salt_form("hydroxychloroquine sulfate") == "hydroxychloroquine"

    def test_mesylate(self):
        assert _strip_salt_form("imatinib mesylate") == "imatinib"

    def test_fumarate(self):
        assert _strip_salt_form("tenofovir fumarate") == "tenofovir"

    def test_tartrate(self):
        assert _strip_salt_form("metoprolol tartrate") == "metoprolol"

    def test_phosphate(self):
        assert _strip_salt_form("oseltamivir phosphate") == "oseltamivir"

    def test_besylate(self):
        assert _strip_salt_form("amlodipine besylate") == "amlodipine"

    def test_potassium(self):
        assert _strip_salt_form("losartan potassium") == "losartan"

    def test_calcium(self):
        assert _strip_salt_form("atorvastatin calcium") == "atorvastatin"

    def test_acetate(self):
        assert _strip_salt_form("megestrol acetate") == "megestrol"

    def test_hcl_abbreviation(self):
        assert _strip_salt_form("venlafaxine hcl") == "venlafaxine"

    # -- hydrate suffixes --

    def test_monohydrate(self):
        assert _strip_salt_form("doxycycline monohydrate") == "doxycycline"

    def test_dihydrate(self):
        assert _strip_salt_form("azithromycin dihydrate") == "azithromycin"

    def test_hemihydrate(self):
        assert _strip_salt_form("cefuroxime hemihydrate") == "cefuroxime"

    def test_hydrate(self):
        assert _strip_salt_form("chloral hydrate") == "chloral"

    def test_sesquihydrate(self):
        assert _strip_salt_form("naproxen sesquihydrate") == "naproxen"

    # -- compound suffixes (salt + hydrate) --

    def test_hydrochloride_monohydrate(self):
        assert _strip_salt_form("cetirizine hydrochloride monohydrate") == "cetirizine"

    def test_disodium(self):
        assert _strip_salt_form("etidronate disodium") == "etidronate"

    # -- no-op cases: names without salt suffixes --

    def test_no_salt_aspirin(self):
        assert _strip_salt_form("aspirin") == "aspirin"

    def test_no_salt_methotrexate(self):
        assert _strip_salt_form("methotrexate") == "methotrexate"

    def test_no_salt_tofacitinib(self):
        assert _strip_salt_form("tofacitinib") == "tofacitinib"

    # -- edge cases --

    def test_empty_string(self):
        assert _strip_salt_form("") == ""

    def test_whitespace_only(self):
        assert _strip_salt_form("   ") == ""

    def test_extra_whitespace(self):
        assert _strip_salt_form("  tofacitinib   citrate  ") == "tofacitinib"

    def test_case_insensitive(self):
        assert _strip_salt_form("Tofacitinib CITRATE") == "Tofacitinib"

    def test_case_insensitive_hydrochloride(self):
        assert _strip_salt_form("Sertraline HydroCHLORIDE") == "Sertraline"


# ===================================================================
# _faers_drug_ae: salt-form fallback integration tests
# ===================================================================


class TestFaersDrugAeSaltFallback:
    """Verify the two-step query logic: try original name, then stripped parent."""

    TOFACITINIB_AES = [
        {"term": "NAUSEA", "count": 500},
        {"term": "HEADACHE", "count": 300},
    ]

    @patch("kg_explain.datasources.faers._faers_query")
    def test_salt_form_falls_back_to_parent(self, mock_query):
        """Salt-form name returns nothing; parent name returns results."""
        def side_effect(cache, name, limit):
            if name == "TOFACITINIB CITRATE":
                return []  # salt form: no hits
            if name == "TOFACITINIB":
                return self.TOFACITINIB_AES
            return []

        mock_query.side_effect = side_effect
        cache = MagicMock()

        results = _faers_drug_ae(cache, "TOFACITINIB CITRATE")

        assert results == self.TOFACITINIB_AES
        assert mock_query.call_count == 2
        # First call: original salt form
        mock_query.assert_any_call(cache, "TOFACITINIB CITRATE", 100)
        # Second call: stripped parent
        mock_query.assert_any_call(cache, "TOFACITINIB", 100)

    @patch("kg_explain.datasources.faers._faers_query")
    def test_non_salt_name_no_double_query(self, mock_query):
        """Non-salt drug names should only query once (no fallback needed)."""
        mock_query.return_value = self.TOFACITINIB_AES
        cache = MagicMock()

        results = _faers_drug_ae(cache, "TOFACITINIB")

        assert results == self.TOFACITINIB_AES
        # Only one call since stripped name == original name
        assert mock_query.call_count == 1

    @patch("kg_explain.datasources.faers._faers_query")
    def test_salt_form_original_succeeds_no_fallback(self, mock_query):
        """If the salt-form name itself returns results, skip the fallback."""
        mock_query.return_value = self.TOFACITINIB_AES
        cache = MagicMock()

        results = _faers_drug_ae(cache, "TOFACITINIB CITRATE")

        assert results == self.TOFACITINIB_AES
        # Only one call since first query succeeded
        assert mock_query.call_count == 1

    @patch("kg_explain.datasources.faers._faers_query")
    def test_both_forms_empty_returns_empty(self, mock_query):
        """If neither salt nor parent returns results, return empty list."""
        mock_query.return_value = []
        cache = MagicMock()

        results = _faers_drug_ae(cache, "UNKNOWNDRUG CITRATE")

        assert results == []
        # Two calls: original + stripped
        assert mock_query.call_count == 2

    @patch("kg_explain.datasources.faers._faers_query")
    def test_original_raises_exception_falls_back(self, mock_query):
        """If the first query raises an exception, still try the stripped name."""
        def side_effect(cache, name, limit):
            if name == "SERTRALINE HYDROCHLORIDE":
                raise Exception("HTTP 404")
            if name == "SERTRALINE":
                return [{"term": "DIZZINESS", "count": 100}]
            return []

        mock_query.side_effect = side_effect
        cache = MagicMock()

        results = _faers_drug_ae(cache, "SERTRALINE HYDROCHLORIDE")

        assert len(results) == 1
        assert results[0]["term"] == "DIZZINESS"
        assert mock_query.call_count == 2

    @patch("kg_explain.datasources.faers._faers_query")
    def test_both_raise_exception_returns_empty(self, mock_query):
        """If both queries raise, return empty list gracefully."""
        mock_query.side_effect = Exception("API unavailable")
        cache = MagicMock()

        results = _faers_drug_ae(cache, "LOSARTAN POTASSIUM")

        assert results == []

    @patch("kg_explain.datasources.faers._faers_query")
    def test_compound_salt_hydrate_fallback(self, mock_query):
        """Compound suffix (e.g. hydrochloride monohydrate) strips correctly."""
        def side_effect(cache, name, limit):
            if name == "CETIRIZINE HYDROCHLORIDE MONOHYDRATE":
                return []
            if name == "CETIRIZINE":
                return [{"term": "DROWSINESS", "count": 800}]
            return []

        mock_query.side_effect = side_effect
        cache = MagicMock()

        results = _faers_drug_ae(cache, "CETIRIZINE HYDROCHLORIDE MONOHYDRATE")

        assert len(results) == 1
        assert results[0]["term"] == "DROWSINESS"
