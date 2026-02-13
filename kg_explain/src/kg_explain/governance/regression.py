"""Regression test suite: fixed inputs with expected outputs.

Stores "regression fixtures" — known input CSVs with pre-computed
expected ranking outputs. On each run, re-runs the ranker and asserts
outputs match within tolerance.

Usage:
    suite = RegressionSuite(Path("tests/fixtures"))
    results = suite.run_all()
    for r in results:
        print(f"{r.fixture_name}: {'PASS' if r.passed else 'FAIL'}")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RegressionFixture:
    """A single regression test case."""
    name: str
    data_dir: Path
    expected_output: Path
    ranker_version: str
    tolerance: float = 0.001

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "data_dir": str(self.data_dir),
            "expected_output": str(self.expected_output),
            "ranker_version": self.ranker_version,
            "tolerance": self.tolerance,
        }


@dataclass
class RegressionResult:
    """Result of one regression test."""
    fixture_name: str
    passed: bool
    max_score_delta: float = 0.0
    rank_changes: int = 0
    details: List[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.fixture_name}: "
            f"max_delta={self.max_score_delta:.6f}, "
            f"rank_changes={self.rank_changes}"
        )


class RegressionSuite:
    """Manage and run regression fixtures."""

    def __init__(self, fixtures_dir: Path):
        """Load fixtures from directory.

        Each subdirectory in fixtures_dir should contain:
          - manifest.json: fixture metadata
          - input CSVs
          - expected_output.csv: expected ranking output
        """
        self.fixtures_dir = fixtures_dir
        self.fixtures: List[RegressionFixture] = []
        self._discover_fixtures()

    def _discover_fixtures(self) -> None:
        """Scan fixtures directory for regression test cases."""
        if not self.fixtures_dir.exists():
            logger.warning("Fixtures directory not found: %s", self.fixtures_dir)
            return

        for subdir in sorted(self.fixtures_dir.iterdir()):
            if not subdir.is_dir():
                continue
            manifest_path = subdir / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)

                fixture = RegressionFixture(
                    name=manifest.get("name", subdir.name),
                    data_dir=subdir / manifest.get("data_subdir", "data"),
                    expected_output=subdir / manifest.get("expected_output", "expected_output.csv"),
                    ranker_version=manifest.get("ranker_version", "v5"),
                    tolerance=float(manifest.get("tolerance", 0.001)),
                )
                self.fixtures.append(fixture)
                logger.debug("Discovered fixture: %s", fixture.name)

            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("Failed to load fixture %s: %s", subdir.name, e)

        logger.info("Discovered %d regression fixtures in %s",
                     len(self.fixtures), self.fixtures_dir)

    def run_all(self) -> List[RegressionResult]:
        """Run all regression tests."""
        results = []
        for fixture in self.fixtures:
            result = self.run_one(fixture.name)
            results.append(result)
        return results

    def run_one(self, fixture_name: str) -> RegressionResult:
        """Run a single regression fixture by comparing actual vs expected output.

        This does NOT re-run the ranker (which would need API calls).
        Instead, it compares the current output CSV against the expected snapshot.
        """
        fixture = None
        for f in self.fixtures:
            if f.name == fixture_name:
                fixture = f
                break

        if fixture is None:
            return RegressionResult(
                fixture_name=fixture_name,
                passed=False,
                details=[f"Fixture not found: {fixture_name}"],
            )

        if not fixture.expected_output.exists():
            return RegressionResult(
                fixture_name=fixture_name,
                passed=False,
                details=[f"Expected output not found: {fixture.expected_output}"],
            )

        # Look for actual output in the fixture's data_dir parent
        actual_output = fixture.data_dir.parent / "actual_output.csv"
        if not actual_output.exists():
            # Also check the expected output's sibling
            actual_output = fixture.expected_output.parent / "actual_output.csv"

        if not actual_output.exists():
            return RegressionResult(
                fixture_name=fixture_name,
                passed=False,
                details=["No actual output found. Run pipeline first to generate output."],
            )

        return self._compare_outputs(fixture, actual_output)

    def _compare_outputs(
        self, fixture: RegressionFixture, actual_path: Path
    ) -> RegressionResult:
        """Compare actual vs expected ranking output."""
        try:
            expected = pd.read_csv(fixture.expected_output, dtype=str)
            actual = pd.read_csv(actual_path, dtype=str)
        except Exception as e:
            return RegressionResult(
                fixture_name=fixture.name,
                passed=False,
                details=[f"Failed to read CSVs: {e}"],
            )

        details: List[str] = []
        max_delta = 0.0
        rank_changes = 0

        # Check score columns
        score_col = "final_score"
        if score_col not in expected.columns or score_col not in actual.columns:
            return RegressionResult(
                fixture_name=fixture.name,
                passed=False,
                details=[f"Missing '{score_col}' column"],
            )

        # Merge on drug + disease
        key_cols = ["drug_normalized", "diseaseId"]
        for col in key_cols:
            if col not in expected.columns or col not in actual.columns:
                return RegressionResult(
                    fixture_name=fixture.name,
                    passed=False,
                    details=[f"Missing key column: {col}"],
                )

        merged = expected[key_cols + [score_col]].merge(
            actual[key_cols + [score_col]],
            on=key_cols, how="outer", suffixes=("_expected", "_actual"),
        )

        for col_suffix in ["_expected", "_actual"]:
            merged[f"{score_col}{col_suffix}"] = pd.to_numeric(
                merged[f"{score_col}{col_suffix}"], errors="coerce"
            ).fillna(0)

        merged["delta"] = abs(
            merged[f"{score_col}_expected"] - merged[f"{score_col}_actual"]
        )

        max_delta = float(merged["delta"].max()) if not merged.empty else 0.0

        # Count pairs that exceed tolerance
        violations = merged[merged["delta"] > fixture.tolerance]
        for _, row in violations.iterrows():
            details.append(
                f"  {row['drug_normalized']}|{row['diseaseId']}: "
                f"expected={row[f'{score_col}_expected']:.6f}, "
                f"actual={row[f'{score_col}_actual']:.6f}, "
                f"delta={row['delta']:.6f}"
            )

        # Check rank order changes
        expected_sorted = expected.sort_values(score_col, ascending=False)
        actual_sorted = actual.sort_values(score_col, ascending=False)

        exp_order = list(expected_sorted["drug_normalized"].head(20))
        act_order = list(actual_sorted["drug_normalized"].head(20))
        rank_changes = sum(1 for a, b in zip(exp_order, act_order) if a != b)

        passed = len(violations) == 0
        if not passed:
            details.insert(0, f"{len(violations)} pairs exceed tolerance {fixture.tolerance}")

        return RegressionResult(
            fixture_name=fixture.name,
            passed=passed,
            max_score_delta=max_delta,
            rank_changes=rank_changes,
            details=details,
        )

    def create_fixture(
        self,
        name: str,
        data_dir: Path,
        output_csv: Path,
        ranker_version: str = "v5",
        tolerance: float = 0.001,
    ) -> RegressionFixture:
        """Create a new fixture from current pipeline output (snapshot).

        Copies the current output as the expected baseline.
        """
        import shutil

        fixture_dir = self.fixtures_dir / name
        fixture_dir.mkdir(parents=True, exist_ok=True)

        # Copy data files
        data_dest = fixture_dir / "data"
        if data_dir.exists():
            if data_dest.exists():
                shutil.rmtree(data_dest)
            shutil.copytree(data_dir, data_dest)

        # Copy output as expected
        expected = fixture_dir / "expected_output.csv"
        shutil.copy2(output_csv, expected)

        # Write manifest
        manifest = {
            "name": name,
            "data_subdir": "data",
            "expected_output": "expected_output.csv",
            "ranker_version": ranker_version,
            "tolerance": tolerance,
        }
        with open(fixture_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        fixture = RegressionFixture(
            name=name,
            data_dir=data_dest,
            expected_output=expected,
            ranker_version=ranker_version,
            tolerance=tolerance,
        )
        self.fixtures.append(fixture)
        logger.info("Created regression fixture: %s", name)
        return fixture
