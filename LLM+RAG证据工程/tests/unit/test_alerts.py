"""Tests for dr.monitoring.alerts.

Covers:
- AlertRule.evaluate: gt, lt, gte, lte operators
- AlertEngine with default_rules: metric exceeding threshold fires alert
- AlertEngine: metric within safe range -> no alerts
- JSONL log: alerts written to file correctly
- load_alert_config: mock YAML file loading
- Alert severity: CRITICAL vs WARNING
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dr.monitoring.alerts import (
    AlertRule,
    AlertSeverity,
    Alert,
    AlertEngine,
    load_alert_config,
)


# ---------------------------------------------------------------------------
# Tests: AlertRule.evaluate
# ---------------------------------------------------------------------------

class TestAlertRuleEvaluate:
    """Tests for individual AlertRule evaluation with different operators."""

    def _make_rule(self, operator: str, threshold: float) -> AlertRule:
        return AlertRule(
            name="test_rule",
            metric="test_metric",
            operator=operator,
            threshold=threshold,
            severity=AlertSeverity.WARNING,
            message_template="{metric}: {value} vs {threshold}",
        )

    def test_gt_fires_when_above(self):
        """gt operator should fire when value > threshold."""
        rule = self._make_rule("gt", 0.5)
        assert rule.evaluate(0.6) is True
        assert rule.evaluate(0.5) is False
        assert rule.evaluate(0.4) is False

    def test_lt_fires_when_below(self):
        """lt operator should fire when value < threshold."""
        rule = self._make_rule("lt", 0.5)
        assert rule.evaluate(0.4) is True
        assert rule.evaluate(0.5) is False
        assert rule.evaluate(0.6) is False

    def test_gte_fires_when_above_or_equal(self):
        """gte operator should fire when value >= threshold."""
        rule = self._make_rule("gte", 0.5)
        assert rule.evaluate(0.6) is True
        assert rule.evaluate(0.5) is True
        assert rule.evaluate(0.4) is False

    def test_lte_fires_when_below_or_equal(self):
        """lte operator should fire when value <= threshold."""
        rule = self._make_rule("lte", 0.5)
        assert rule.evaluate(0.4) is True
        assert rule.evaluate(0.5) is True
        assert rule.evaluate(0.6) is False

    def test_invalid_operator_returns_false(self):
        """Unknown operator should return False (no alert)."""
        rule = self._make_rule("invalid_op", 0.5)
        assert rule.evaluate(0.6) is False
        assert rule.evaluate(0.4) is False

    def test_boundary_values_gt(self):
        """gt with exact threshold should not fire."""
        rule = self._make_rule("gt", 10.0)
        assert rule.evaluate(10.0) is False
        assert rule.evaluate(10.001) is True

    def test_boundary_values_lt(self):
        """lt with exact threshold should not fire."""
        rule = self._make_rule("lt", 10.0)
        assert rule.evaluate(10.0) is False
        assert rule.evaluate(9.999) is True


# ---------------------------------------------------------------------------
# Tests: AlertEngine
# ---------------------------------------------------------------------------

class TestAlertEngine:
    """Tests for AlertEngine evaluation."""

    def test_default_rules_fire_on_high_nogo_ratio(self):
        """nogo_ratio > 0.80 should fire a CRITICAL alert."""
        engine = AlertEngine(AlertEngine.default_rules())
        alerts = engine.evaluate({"nogo_ratio": 0.85})

        nogo_alerts = [a for a in alerts if a.rule_name == "nogo_ratio_critical"]
        assert len(nogo_alerts) == 1
        assert nogo_alerts[0].severity == "critical"
        assert nogo_alerts[0].metric_value == 0.85

    def test_safe_metrics_no_alerts(self):
        """All metrics within safe ranges should produce no alerts."""
        engine = AlertEngine(AlertEngine.default_rules())
        safe_metrics = {
            "nogo_ratio": 0.30,
            "llm_fail_rate": 0.05,
            "pubmed_p95_latency": 2.0,
            "zero_benefit_count": 0,
            "pipeline_duration_minutes": 10.0,
            "gold_recall": 0.80,
        }
        alerts = engine.evaluate(safe_metrics)
        assert len(alerts) == 0

    def test_missing_metric_ignored(self):
        """Metrics not in the rules should not cause errors."""
        rules = [AlertRule(
            name="test",
            metric="some_metric",
            operator="gt",
            threshold=1.0,
            severity=AlertSeverity.WARNING,
            message_template="test",
        )]
        engine = AlertEngine(rules)
        # Pass metrics that don't include "some_metric"
        alerts = engine.evaluate({"other_metric": 999.0})
        assert len(alerts) == 0

    def test_multiple_rules_can_fire(self):
        """Multiple rules can fire simultaneously."""
        engine = AlertEngine(AlertEngine.default_rules())
        bad_metrics = {
            "nogo_ratio": 0.95,         # > 0.80 -> CRITICAL
            "llm_fail_rate": 0.50,       # > 0.20 -> CRITICAL
            "gold_recall": 0.10,         # < 0.30 -> CRITICAL
        }
        alerts = engine.evaluate(bad_metrics)
        assert len(alerts) >= 3

    def test_alert_message_formatting(self):
        """Alert message should use the template with value/threshold substitution."""
        rule = AlertRule(
            name="test_fmt",
            metric="x",
            operator="gt",
            threshold=5.0,
            severity=AlertSeverity.WARNING,
            message_template="{metric} is {value} (limit: {threshold})",
        )
        engine = AlertEngine([rule])
        alerts = engine.evaluate({"x": 10.0})

        assert len(alerts) == 1
        assert "x" in alerts[0].message
        assert "10" in alerts[0].message
        assert "5" in alerts[0].message

    def test_context_passed_to_alerts(self):
        """Context dict should be included in fired alerts."""
        rule = AlertRule(
            name="ctx_test",
            metric="v",
            operator="gt",
            threshold=0,
            severity=AlertSeverity.WARNING,
            message_template="test",
        )
        engine = AlertEngine([rule])
        alerts = engine.evaluate({"v": 1.0}, context={"run_id": "abc123"})

        assert len(alerts) == 1
        assert alerts[0].context == {"run_id": "abc123"}


# ---------------------------------------------------------------------------
# Tests: Alert severity
# ---------------------------------------------------------------------------

class TestAlertSeverity:
    """Tests for CRITICAL vs WARNING severity."""

    def test_critical_severity(self):
        """CRITICAL alert should have severity 'critical'."""
        rule = AlertRule(
            name="crit_rule", metric="m", operator="gt", threshold=0,
            severity=AlertSeverity.CRITICAL, message_template="critical!",
        )
        engine = AlertEngine([rule])
        alerts = engine.evaluate({"m": 1.0})
        assert alerts[0].severity == "critical"

    def test_warning_severity(self):
        """WARNING alert should have severity 'warning'."""
        rule = AlertRule(
            name="warn_rule", metric="m", operator="gt", threshold=0,
            severity=AlertSeverity.WARNING, message_template="warning!",
        )
        engine = AlertEngine([rule])
        alerts = engine.evaluate({"m": 1.0})
        assert alerts[0].severity == "warning"

    def test_default_rules_have_both_severities(self):
        """Default rules should include both CRITICAL and WARNING rules."""
        rules = AlertEngine.default_rules()
        severities = {r.severity for r in rules}
        assert AlertSeverity.CRITICAL in severities
        assert AlertSeverity.WARNING in severities


# ---------------------------------------------------------------------------
# Tests: JSONL log
# ---------------------------------------------------------------------------

class TestJSONLLog:
    """Tests for alert log file writing."""

    def test_alerts_written_to_jsonl(self, tmp_path):
        """Fired alerts should be appended to the JSONL log file."""
        log_path = tmp_path / "alerts.jsonl"
        rule = AlertRule(
            name="log_test", metric="val", operator="gt", threshold=0,
            severity=AlertSeverity.WARNING, message_template="val={value}",
        )
        engine = AlertEngine([rule], alert_log_path=log_path)
        engine.evaluate({"val": 5.0})

        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["rule_name"] == "log_test"
        assert entry["metric_value"] == 5.0
        assert entry["severity"] == "warning"
        assert "timestamp" in entry

    def test_multiple_evaluations_append(self, tmp_path):
        """Multiple evaluate() calls should append, not overwrite."""
        log_path = tmp_path / "alerts.jsonl"
        rule = AlertRule(
            name="append_test", metric="val", operator="gt", threshold=0,
            severity=AlertSeverity.WARNING, message_template="val={value}",
        )
        engine = AlertEngine([rule], alert_log_path=log_path)

        engine.evaluate({"val": 1.0})
        engine.evaluate({"val": 2.0})
        engine.evaluate({"val": 3.0})

        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        values = [json.loads(line)["metric_value"] for line in lines]
        assert values == [1.0, 2.0, 3.0]

    def test_no_log_when_no_alerts(self, tmp_path):
        """No alerts fired -> JSONL file should not be created/written."""
        log_path = tmp_path / "alerts.jsonl"
        rule = AlertRule(
            name="nolog_test", metric="val", operator="gt", threshold=100,
            severity=AlertSeverity.WARNING, message_template="test",
        )
        engine = AlertEngine([rule], alert_log_path=log_path)
        engine.evaluate({"val": 1.0})

        # File should not exist (or be empty if pre-created)
        assert not log_path.exists()

    def test_no_log_path_configured(self):
        """Engine without alert_log_path should still work (no file I/O)."""
        rule = AlertRule(
            name="nopath_test", metric="val", operator="gt", threshold=0,
            severity=AlertSeverity.WARNING, message_template="test",
        )
        engine = AlertEngine([rule])  # no alert_log_path
        alerts = engine.evaluate({"val": 5.0})
        assert len(alerts) == 1  # alert fires, just no file


# ---------------------------------------------------------------------------
# Tests: load_alert_config
# ---------------------------------------------------------------------------

class TestLoadAlertConfig:
    """Tests for loading alert rules from YAML config."""

    def test_load_valid_yaml(self, tmp_path):
        """Valid YAML with alerts should parse into AlertRule list."""
        yaml_content = """\
alerts:
  - name: high_nogo
    metric: nogo_ratio
    operator: gt
    threshold: 0.80
    severity: critical
    message: "NO-GO ratio {value:.1%} exceeds {threshold:.1%}"
  - name: slow_pipeline
    metric: duration_min
    operator: gt
    threshold: 60
    severity: warning
    message: "Pipeline took {value:.1f} min"
"""
        config_path = tmp_path / "alerts.yaml"
        config_path.write_text(yaml_content, encoding="utf-8")

        rules = load_alert_config(config_path)
        assert len(rules) == 2

        assert rules[0].name == "high_nogo"
        assert rules[0].metric == "nogo_ratio"
        assert rules[0].operator == "gt"
        assert rules[0].threshold == 0.80
        assert rules[0].severity == AlertSeverity.CRITICAL

        assert rules[1].name == "slow_pipeline"
        assert rules[1].severity == AlertSeverity.WARNING
        assert rules[1].threshold == 60.0

    def test_load_empty_alerts_list(self, tmp_path):
        """YAML with empty alerts list should return empty rule list."""
        config_path = tmp_path / "empty.yaml"
        config_path.write_text("alerts: []\n", encoding="utf-8")

        rules = load_alert_config(config_path)
        assert rules == []

    def test_load_default_severity(self, tmp_path):
        """If severity is missing, it should default to 'warning'."""
        yaml_content = """\
alerts:
  - name: no_severity
    metric: m
    operator: gt
    threshold: 1.0
"""
        config_path = tmp_path / "nosev.yaml"
        config_path.write_text(yaml_content, encoding="utf-8")

        rules = load_alert_config(config_path)
        assert len(rules) == 1
        assert rules[0].severity == AlertSeverity.WARNING


# ---------------------------------------------------------------------------
# Tests: Alert dataclass
# ---------------------------------------------------------------------------

class TestAlertDataclass:
    """Tests for the Alert data structure."""

    def test_to_dict(self):
        """Alert.to_dict() should return all fields."""
        alert = Alert(
            rule_name="test",
            severity="critical",
            message="test message",
            metric_value=0.95,
            threshold=0.80,
            timestamp="2026-01-01T00:00:00Z",
            context={"run": "123"},
        )
        d = alert.to_dict()
        assert d["rule_name"] == "test"
        assert d["severity"] == "critical"
        assert d["context"] == {"run": "123"}
        assert d["metric_value"] == 0.95

    def test_alert_has_timestamp(self):
        """Alert created by engine should have a non-empty ISO timestamp."""
        rule = AlertRule(
            name="ts_test", metric="v", operator="gt", threshold=0,
            severity=AlertSeverity.WARNING, message_template="t",
        )
        engine = AlertEngine([rule])
        alerts = engine.evaluate({"v": 1.0})
        assert alerts[0].timestamp != ""
        assert "T" in alerts[0].timestamp
