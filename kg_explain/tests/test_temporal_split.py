"""Tests for kg_explain.evaluation.temporal_split.

Covers:
- split_by_year: normal split, missing column fallback
- cross_disease_holdout: holdout diseases in test, rest in train
- run_temporal_validation: mocked run_benchmark, gap_analysis computation
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from kg_explain.evaluation.temporal_split import (
    split_by_year,
    cross_disease_holdout,
    run_temporal_validation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gold_df_with_year():
    """Gold standard DataFrame with approval_year column."""
    return pd.DataFrame({
        "drug_normalized": ["aspirin", "ibuprofen", "metformin", "atorvastatin", "losartan"],
        "diseaseId": ["EFO_1", "EFO_1", "EFO_2", "EFO_1", "EFO_2"],
        "approval_year": [2015, 2018, 2020, 2022, 2019],
    })


@pytest.fixture
def gold_df_no_year():
    """Gold standard DataFrame WITHOUT approval_year column."""
    return pd.DataFrame({
        "drug_normalized": ["aspirin", "ibuprofen"],
        "diseaseId": ["EFO_1", "EFO_2"],
    })


# ---------------------------------------------------------------------------
# Tests: split_by_year
# ---------------------------------------------------------------------------

class TestSplitByYear:
    """Tests for temporal split by year."""

    def test_split_correct_partitioning(self, gold_df_with_year):
        """Train should have years < cutoff, test should have years >= cutoff."""
        train, test = split_by_year(gold_df_with_year, cutoff_year=2020)

        # Before 2020: 2015, 2018, 2019 -> 3 rows
        assert len(train) == 3
        assert all(train["approval_year"] < 2020)

        # 2020 and after: 2020, 2022 -> 2 rows
        assert len(test) == 2
        assert all(test["approval_year"] >= 2020)

    def test_split_preserves_all_rows(self, gold_df_with_year):
        """Train + test should contain all original rows."""
        train, test = split_by_year(gold_df_with_year, cutoff_year=2020)
        assert len(train) + len(test) == len(gold_df_with_year)

    def test_split_extreme_cutoff_all_train(self, gold_df_with_year):
        """Cutoff far in future: all data in train, empty test."""
        train, test = split_by_year(gold_df_with_year, cutoff_year=2099)
        assert len(train) == 5
        assert len(test) == 0

    def test_split_extreme_cutoff_all_test(self, gold_df_with_year):
        """Cutoff far in past: all data in test, empty train."""
        train, test = split_by_year(gold_df_with_year, cutoff_year=2000)
        assert len(train) == 0
        assert len(test) == 5

    def test_missing_column_returns_all_as_train(self, gold_df_no_year):
        """When date_col is missing, return all as train, empty test."""
        train, test = split_by_year(gold_df_no_year, cutoff_year=2020)

        assert len(train) == len(gold_df_no_year)
        assert len(test) == 0
        assert list(test.columns) == list(gold_df_no_year.columns)

    def test_custom_date_column(self):
        """Split on a custom date column name."""
        df = pd.DataFrame({
            "drug_normalized": ["d1", "d2", "d3"],
            "diseaseId": ["E1", "E2", "E3"],
            "year_approved": [2018, 2020, 2022],
        })
        train, test = split_by_year(df, cutoff_year=2020, date_col="year_approved")
        assert len(train) == 1
        assert len(test) == 2

    def test_string_year_column_coerced(self):
        """Year column with string values should be coerced to numeric."""
        df = pd.DataFrame({
            "drug_normalized": ["d1", "d2"],
            "diseaseId": ["E1", "E2"],
            "approval_year": ["2018", "2022"],
        })
        train, test = split_by_year(df, cutoff_year=2020)
        assert len(train) == 1
        assert len(test) == 1


# ---------------------------------------------------------------------------
# Tests: cross_disease_holdout
# ---------------------------------------------------------------------------

class TestCrossDiseaseHoldout:
    """Tests for cross-disease holdout split."""

    def test_holdout_diseases_in_test(self, gold_df_with_year):
        """Held-out diseases should be in test, rest in train."""
        train, test = cross_disease_holdout(gold_df_with_year, holdout_diseases=["EFO_2"])

        # EFO_2 drugs: metformin (2020) + losartan (2019)
        assert len(test) == 2
        assert all(test["diseaseId"] == "EFO_2")

        # EFO_1 drugs: aspirin (2015) + ibuprofen (2018) + atorvastatin (2022)
        assert len(train) == 3
        assert all(train["diseaseId"] == "EFO_1")

    def test_holdout_multiple_diseases(self):
        """Hold out multiple diseases."""
        df = pd.DataFrame({
            "drug_normalized": ["d1", "d2", "d3", "d4"],
            "diseaseId": ["A", "B", "C", "A"],
        })
        train, test = cross_disease_holdout(df, holdout_diseases=["A", "C"])
        assert len(test) == 3  # d1(A), d3(C), d4(A)
        assert len(train) == 1  # d2(B)
        assert set(test["diseaseId"]) == {"A", "C"}

    def test_holdout_empty_list(self, gold_df_with_year):
        """Empty holdout list: all data in train, empty test."""
        train, test = cross_disease_holdout(gold_df_with_year, holdout_diseases=[])
        assert len(train) == len(gold_df_with_year)
        assert len(test) == 0

    def test_holdout_all_diseases(self, gold_df_with_year):
        """Hold out all diseases: empty train, all in test."""
        train, test = cross_disease_holdout(
            gold_df_with_year, holdout_diseases=["EFO_1", "EFO_2"]
        )
        assert len(train) == 0
        assert len(test) == len(gold_df_with_year)

    def test_preserves_all_rows(self, gold_df_with_year):
        """Train + test = original data."""
        train, test = cross_disease_holdout(gold_df_with_year, holdout_diseases=["EFO_1"])
        assert len(train) + len(test) == len(gold_df_with_year)


# ---------------------------------------------------------------------------
# Tests: run_temporal_validation
# ---------------------------------------------------------------------------

class TestRunTemporalValidation:
    """Tests for run_temporal_validation with mocked run_benchmark."""

    @patch("kg_explain.evaluation.temporal_split.run_benchmark")
    def test_gap_analysis_is_test_minus_train(self, mock_benchmark, tmp_path):
        """gap_analysis should equal test metric - train metric."""
        # Mock run_benchmark to return known metrics
        train_metrics = {
            "aggregate": {"mrr": 0.6, "map": 0.5},
            "per_disease": {},
            "n_diseases_evaluated": 1,
            "n_gold_pairs": 3,
            "n_gold_found": 2,
        }
        test_metrics = {
            "aggregate": {"mrr": 0.4, "map": 0.3},
            "per_disease": {},
            "n_diseases_evaluated": 1,
            "n_gold_pairs": 2,
            "n_gold_found": 1,
        }
        mock_benchmark.side_effect = [train_metrics, test_metrics]

        # Create a dummy rank CSV
        rank_csv = tmp_path / "rank.csv"
        pd.DataFrame({
            "drug_normalized": ["aspirin", "ibuprofen"],
            "diseaseId": ["EFO_1", "EFO_1"],
            "final_score": [1.0, 0.5],
        }).to_csv(rank_csv, index=False)

        gold_df = pd.DataFrame({
            "drug_normalized": ["aspirin", "ibuprofen", "metformin", "atorvastatin", "losartan"],
            "diseaseId": ["EFO_1", "EFO_1", "EFO_2", "EFO_1", "EFO_2"],
            "approval_year": [2015, 2018, 2020, 2022, 2019],
        })

        result = run_temporal_validation(rank_csv, gold_df, cutoff_year=2020)

        # Verify structure
        assert result["cutoff_year"] == 2020
        assert "train_metrics" in result
        assert "test_metrics" in result
        assert "gap_analysis" in result
        assert "train_n_pairs" in result
        assert "test_n_pairs" in result

        # Verify gap_analysis = test - train
        gap = result["gap_analysis"]
        assert abs(gap["mrr"] - (0.4 - 0.6)) < 1e-6
        assert abs(gap["map"] - (0.3 - 0.5)) < 1e-6

    @patch("kg_explain.evaluation.temporal_split.run_benchmark")
    def test_empty_test_set(self, mock_benchmark, tmp_path):
        """When all data is before cutoff, test_metrics should be empty dict."""
        train_metrics = {
            "aggregate": {"mrr": 0.5},
            "per_disease": {},
            "n_diseases_evaluated": 1,
            "n_gold_pairs": 2,
            "n_gold_found": 2,
        }
        mock_benchmark.return_value = train_metrics

        rank_csv = tmp_path / "rank.csv"
        pd.DataFrame({
            "drug_normalized": ["a"], "diseaseId": ["E1"], "final_score": [1.0],
        }).to_csv(rank_csv, index=False)

        gold_df = pd.DataFrame({
            "drug_normalized": ["a", "b"],
            "diseaseId": ["E1", "E1"],
            "approval_year": [2015, 2018],
        })

        result = run_temporal_validation(rank_csv, gold_df, cutoff_year=2099)

        assert result["test_n_pairs"] == 0
        # test_metrics is {} when test set is empty
        assert result["test_metrics"] == {}
        # benchmark called only once (for train)
        assert mock_benchmark.call_count == 1

    @patch("kg_explain.evaluation.temporal_split.run_benchmark")
    def test_ks_parameter_passed_through(self, mock_benchmark, tmp_path):
        """Custom ks should be passed to run_benchmark."""
        mock_benchmark.return_value = {
            "aggregate": {}, "per_disease": {},
            "n_diseases_evaluated": 0, "n_gold_pairs": 0, "n_gold_found": 0,
        }

        rank_csv = tmp_path / "rank.csv"
        pd.DataFrame({
            "drug_normalized": ["a"], "diseaseId": ["E1"], "final_score": [1.0],
        }).to_csv(rank_csv, index=False)

        gold_df = pd.DataFrame({
            "drug_normalized": ["a", "b"],
            "diseaseId": ["E1", "E1"],
            "approval_year": [2015, 2022],
        })

        run_temporal_validation(rank_csv, gold_df, cutoff_year=2020, ks=[3, 7])

        # Verify ks was passed through
        for call_args in mock_benchmark.call_args_list:
            assert call_args.kwargs.get("ks") == [3, 7] or call_args[1].get("ks") == [3, 7]
