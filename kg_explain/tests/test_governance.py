"""Tests for kg_explain.governance: registry, quality_gate, regression."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from kg_explain.governance.registry import ModelVersion, ModelRegistry
from kg_explain.governance.quality_gate import QualityGate, QualityGateResult
from kg_explain.governance.regression import (
    RegressionSuite,
    RegressionFixture,
    RegressionResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, content: str = "ranker: v5\nweights:\n  moa: 0.3\n") -> Path:
    """Write a tiny YAML-like config file and return its path."""
    path.write_text(content, encoding="utf-8")
    return path


def _make_data_dir(base: Path, csvs: dict[str, str] | None = None) -> Path:
    """Create a data directory with optional CSV stubs."""
    base.mkdir(parents=True, exist_ok=True)
    if csvs is None:
        csvs = {
            "edge_drug_target.csv": "drug,target\naspirin,COX2\n",
            "edge_gene_pathway.csv": "gene,pathway\nCOX2,inflammation\n",
        }
    for name, body in csvs.items():
        (base / name).write_text(body, encoding="utf-8")
    return base


def _make_ranking_csv(path: Path, rows: list[dict]) -> Path:
    """Write a ranking CSV with drug_normalized, diseaseId, final_score."""
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return path


# ===================================================================
# ModelRegistry tests
# ===================================================================


class TestModelRegistry:
    """Tests for ModelVersion and ModelRegistry."""

    def test_register_version_id_pattern(self, tmp_path: Path):
        """Register a version; its ID should follow the v{X}-YYYYMMDD-{hash} pattern."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        ver = registry.register("v5", config, data, {"hit@10": 0.55, "mrr": 0.30})

        # Pattern: v5-YYYYMMDD-<8hex>
        parts = ver.version_id.split("-")
        assert parts[0] == "v5"
        assert len(parts[1]) == 8 and parts[1].isdigit()
        assert len(parts[2]) == 8
        assert ver.status == "candidate"
        assert ver.metrics["hit@10"] == 0.55

    def test_get_latest_returns_none_when_no_approved(self, tmp_path: Path):
        """get_latest returns None when no versions have been approved."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        registry.register("v5", config, data, {"hit@10": 0.5})
        assert registry.get_latest("v5") is None

    def test_approve_then_get_latest(self, tmp_path: Path):
        """After approving a version, get_latest returns it."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        ver = registry.register("v5", config, data, {"hit@10": 0.55})
        registry.approve(ver.version_id)

        latest = registry.get_latest("v5")
        assert latest is not None
        assert latest.version_id == ver.version_id
        assert latest.status == "approved"

    def test_deprecate_version(self, tmp_path: Path):
        """Deprecating a version sets status to 'deprecated'."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        ver = registry.register("v5", config, data, {"hit@10": 0.5})
        registry.deprecate(ver.version_id, reason="outdated")

        found = registry._find(ver.version_id)
        assert found.status == "deprecated"
        assert "outdated" in found.notes

    def test_list_versions_filter_ranker(self, tmp_path: Path):
        """list_versions with ranker_version filter only returns matching versions."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        registry.register("v3", config, data, {"hit@10": 0.4})
        registry.register("v5", config, data, {"hit@10": 0.55})
        registry.register("v5", config, data, {"hit@10": 0.60})

        v5_list = registry.list_versions(ranker_version="v5")
        assert len(v5_list) == 2
        assert all(v.ranker_version == "v5" for v in v5_list)

    def test_list_versions_filter_status(self, tmp_path: Path):
        """list_versions with status filter only returns matching statuses."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        v1 = registry.register("v5", config, data, {"hit@10": 0.5})
        v2 = registry.register("v5", config, data, {"hit@10": 0.55})
        registry.approve(v2.version_id)

        approved = registry.list_versions(status="approved")
        assert len(approved) == 1
        assert approved[0].version_id == v2.version_id

        candidates = registry.list_versions(status="candidate")
        assert len(candidates) == 1
        assert candidates[0].version_id == v1.version_id

    def test_diff_two_versions(self, tmp_path: Path):
        """diff returns config_changed, metric_diff, and data_diff."""
        registry = ModelRegistry(tmp_path / "reg.json")

        config_a = _write_config(tmp_path / "config_a.yaml", "version: a\n")
        data_a = _make_data_dir(tmp_path / "data_a", {
            "edges.csv": "a,b\n1,2\n",
        })
        va = registry.register("v5", config_a, data_a, {"hit@10": 0.50, "mrr": 0.25})

        config_b = _write_config(tmp_path / "config_b.yaml", "version: b\n")
        data_b = _make_data_dir(tmp_path / "data_b", {
            "edges.csv": "a,b\n1,3\n",        # changed content
            "new_edges.csv": "x,y\n10,20\n",  # added file
        })
        vb = registry.register("v5", config_b, data_b, {"hit@10": 0.55, "mrr": 0.20})

        d = registry.diff(va.version_id, vb.version_id)

        assert d["config_changed"] is True
        assert d["metric_diff"]["hit@10"]["delta"] == pytest.approx(-0.05, abs=1e-4)
        assert d["metric_diff"]["mrr"]["delta"] == pytest.approx(0.05, abs=1e-4)
        assert "edges.csv" in d["data_diff"]["changed"]
        assert "new_edges.csv" in d["data_diff"]["added"]

    def test_persistence_save_and_reload(self, tmp_path: Path):
        """Registry state persists across save / reload cycles."""
        reg_path = tmp_path / "reg.json"

        registry = ModelRegistry(reg_path)
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        ver = registry.register("v5", config, data, {"hit@10": 0.5})
        registry.approve(ver.version_id)

        # Reload from disk
        registry2 = ModelRegistry(reg_path)
        assert len(registry2.list_versions()) == 1
        latest = registry2.get_latest("v5")
        assert latest is not None
        assert latest.version_id == ver.version_id
        assert latest.status == "approved"

    def test_register_duplicate_config_gets_unique_ids(self, tmp_path: Path):
        """Registering the same config+data twice yields distinct version IDs."""
        registry = ModelRegistry(tmp_path / "reg.json")
        config = _write_config(tmp_path / "config.yaml")
        data = _make_data_dir(tmp_path / "data")

        v1 = registry.register("v5", config, data, {"hit@10": 0.5})
        v2 = registry.register("v5", config, data, {"hit@10": 0.5})

        assert v1.version_id != v2.version_id
        assert len(registry.list_versions()) == 2


# ===================================================================
# QualityGate tests
# ===================================================================


class TestQualityGate:
    """Tests for QualityGate and QualityGateResult."""

    def test_all_metrics_above_thresholds_passes(self):
        """If all metrics exceed thresholds the gate passes."""
        gate = QualityGate({"hit@10": 0.50, "mrr": 0.25})
        result = gate.check({"hit@10": 0.60, "mrr": 0.35})

        assert result.passed is True
        assert len(result.failures) == 0
        assert len(result.regressions) == 0

    def test_one_metric_below_threshold_fails(self):
        """If one metric is below its threshold the gate fails."""
        gate = QualityGate({"hit@10": 0.50, "mrr": 0.25})
        result = gate.check({"hit@10": 0.45, "mrr": 0.30})

        assert result.passed is False
        assert len(result.failures) == 1
        assert "hit@10" in result.failures[0]
        assert "0.4500" in result.failures[0]

    def test_regression_large_drop_fails(self):
        """A drop exceeding regression_tolerance blocks the gate."""
        gate = QualityGate({"hit@10": 0.50}, regression_tolerance=0.05)
        baseline = {"hit@10": 0.60}
        current = {"hit@10": 0.52}  # drop = 0.08 > 0.05

        result = gate.check(current, baseline)

        assert result.passed is False
        assert len(result.regressions) == 1
        assert "dropped" in result.regressions[0]

    def test_regression_within_tolerance_passes(self):
        """A drop within regression_tolerance still passes."""
        gate = QualityGate({"hit@10": 0.50}, regression_tolerance=0.05)
        baseline = {"hit@10": 0.60}
        current = {"hit@10": 0.57}  # drop = 0.03 <= 0.05

        result = gate.check(current, baseline)

        assert result.passed is True
        assert len(result.regressions) == 0

    def test_warning_margin_near_threshold(self):
        """Metric within warning_margin of threshold triggers a warning but passes."""
        gate = QualityGate({"hit@10": 0.50}, warning_margin=0.03)
        # 0.52 is above 0.50 but within 0.50 + 0.03 = 0.53
        result = gate.check({"hit@10": 0.52})

        assert result.passed is True
        assert len(result.warnings) == 1
        assert "hit@10" in result.warnings[0]

    def test_from_config_creates_correct_gate(self):
        """from_config class method constructs a gate with the right thresholds."""
        config = {
            "thresholds": {"hit@10": 0.50, "mrr": 0.25},
            "regression_tolerance": 0.10,
            "warning_margin": 0.02,
        }
        gate = QualityGate.from_config(config)

        assert gate.thresholds == {"hit@10": 0.50, "mrr": 0.25}
        assert gate.regression_tolerance == 0.10
        assert gate.warning_margin == 0.02

        # Verify it actually works
        result = gate.check({"hit@10": 0.55, "mrr": 0.30})
        assert result.passed is True

    def test_missing_metric_is_warning_not_failure(self):
        """A metric in thresholds but absent from current metrics is a warning, not failure."""
        gate = QualityGate({"hit@10": 0.50, "mrr": 0.25})
        # Only provide hit@10; mrr is missing
        result = gate.check({"hit@10": 0.60})

        assert result.passed is True
        assert len(result.warnings) == 1
        assert "mrr" in result.warnings[0]
        assert "not found" in result.warnings[0]


# ===================================================================
# RegressionSuite tests
# ===================================================================


class TestRegressionSuite:
    """Tests for RegressionSuite, RegressionFixture, RegressionResult."""

    @staticmethod
    def _build_fixture_dir(
        base: Path,
        name: str,
        *,
        ranker_version: str = "v5",
        tolerance: float = 0.001,
        expected_rows: list[dict] | None = None,
        actual_rows: list[dict] | None = None,
    ) -> Path:
        """Create a fixture subdirectory with manifest, expected output, and optional actual output."""
        fixture_dir = base / name
        fixture_dir.mkdir(parents=True, exist_ok=True)

        # Data sub-directory (can be empty)
        data_dir = fixture_dir / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "stub.csv").write_text("col\nval\n", encoding="utf-8")

        # Manifest
        manifest = {
            "name": name,
            "data_subdir": "data",
            "expected_output": "expected_output.csv",
            "ranker_version": ranker_version,
            "tolerance": tolerance,
        }
        (fixture_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        # Expected output
        if expected_rows is None:
            expected_rows = [
                {"drug_normalized": "aspirin", "diseaseId": "D001", "final_score": "0.85"},
                {"drug_normalized": "statin", "diseaseId": "D001", "final_score": "0.70"},
            ]
        _make_ranking_csv(fixture_dir / "expected_output.csv", expected_rows)

        # Actual output (placed so run_one can find it)
        if actual_rows is not None:
            _make_ranking_csv(fixture_dir / "actual_output.csv", actual_rows)

        return fixture_dir

    def test_discover_fixtures_from_directory(self, tmp_path: Path):
        """Discovers fixtures from subdirectories containing manifest.json."""
        fixtures_root = tmp_path / "fixtures"
        self._build_fixture_dir(fixtures_root, "case_a")
        self._build_fixture_dir(fixtures_root, "case_b")
        # Add a non-fixture dir (no manifest)
        (fixtures_root / "not_a_fixture").mkdir(parents=True)

        suite = RegressionSuite(fixtures_root)

        assert len(suite.fixtures) == 2
        names = {f.name for f in suite.fixtures}
        assert names == {"case_a", "case_b"}

    def test_run_one_matching_outputs_passes(self, tmp_path: Path):
        """run_one passes when actual output matches expected within tolerance."""
        fixtures_root = tmp_path / "fixtures"
        rows = [
            {"drug_normalized": "aspirin", "diseaseId": "D001", "final_score": "0.85"},
            {"drug_normalized": "statin", "diseaseId": "D001", "final_score": "0.70"},
        ]
        self._build_fixture_dir(
            fixtures_root, "exact_match",
            expected_rows=rows,
            actual_rows=rows,  # identical
        )

        suite = RegressionSuite(fixtures_root)
        result = suite.run_one("exact_match")

        assert result.passed is True
        assert result.max_score_delta == pytest.approx(0.0, abs=1e-6)

    def test_run_one_delta_exceeds_tolerance_fails(self, tmp_path: Path):
        """run_one fails when score delta exceeds the fixture tolerance."""
        fixtures_root = tmp_path / "fixtures"
        expected = [
            {"drug_normalized": "aspirin", "diseaseId": "D001", "final_score": "0.85"},
            {"drug_normalized": "statin", "diseaseId": "D001", "final_score": "0.70"},
        ]
        actual = [
            {"drug_normalized": "aspirin", "diseaseId": "D001", "final_score": "0.80"},  # delta 0.05
            {"drug_normalized": "statin", "diseaseId": "D001", "final_score": "0.70"},
        ]
        self._build_fixture_dir(
            fixtures_root, "drifted",
            tolerance=0.001,
            expected_rows=expected,
            actual_rows=actual,
        )

        suite = RegressionSuite(fixtures_root)
        result = suite.run_one("drifted")

        assert result.passed is False
        assert result.max_score_delta >= 0.04
        assert any("exceed tolerance" in d for d in result.details)

    def test_run_one_missing_fixture_fails(self, tmp_path: Path):
        """run_one with a non-existent fixture name returns passed=False."""
        fixtures_root = tmp_path / "fixtures"
        fixtures_root.mkdir()

        suite = RegressionSuite(fixtures_root)
        result = suite.run_one("nonexistent_fixture")

        assert result.passed is False
        assert any("not found" in d.lower() for d in result.details)

    def test_create_fixture_directory_structure(self, tmp_path: Path):
        """create_fixture creates the expected directory layout."""
        fixtures_root = tmp_path / "fixtures"
        fixtures_root.mkdir()

        # Source data and output to snapshot
        source_data = _make_data_dir(tmp_path / "pipeline_data")
        output_csv = _make_ranking_csv(
            tmp_path / "ranking.csv",
            [
                {"drug_normalized": "aspirin", "diseaseId": "D001", "final_score": "0.85"},
            ],
        )

        suite = RegressionSuite(fixtures_root)
        fixture = suite.create_fixture(
            name="snapshot_v5",
            data_dir=source_data,
            output_csv=output_csv,
            ranker_version="v5",
            tolerance=0.002,
        )

        assert fixture.name == "snapshot_v5"
        assert fixture.ranker_version == "v5"
        assert fixture.tolerance == 0.002

        fixture_dir = fixtures_root / "snapshot_v5"
        assert fixture_dir.is_dir()
        assert (fixture_dir / "manifest.json").exists()
        assert (fixture_dir / "expected_output.csv").exists()
        assert (fixture_dir / "data").is_dir()

        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "snapshot_v5"
        assert manifest["ranker_version"] == "v5"
        assert manifest["tolerance"] == 0.002

        # The fixture should also be discoverable now
        assert any(f.name == "snapshot_v5" for f in suite.fixtures)
