"""Alert rules and dispatch for pipeline monitoring.

Lightweight alerting: check metrics against thresholds after each
pipeline run, log warnings/errors, and append alert events to a JSONL file.

Usage:
    engine = AlertEngine(AlertEngine.default_rules())
    alerts = engine.evaluate({"nogo_ratio": 0.85, "llm_fail_rate": 0.05})
    for alert in alerts:
        print(f"[{alert.severity}] {alert.message}")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class AlertRule:
    """A single alerting rule.

    Attributes:
        name: Human-readable rule name
        metric: Metric key to check
        operator: Comparison operator ("gt", "lt", "gte", "lte")
        threshold: Threshold value
        severity: Alert severity level
        message_template: Message format string (can use {value}, {threshold}, {metric})
    """
    name: str
    metric: str
    operator: str  # "gt", "lt", "gte", "lte"
    threshold: float
    severity: AlertSeverity
    message_template: str

    def evaluate(self, value: float) -> bool:
        """Check if the rule fires for the given value. Returns True if alert should fire."""
        ops = {
            "gt": value > self.threshold,
            "lt": value < self.threshold,
            "gte": value >= self.threshold,
            "lte": value <= self.threshold,
        }
        return ops.get(self.operator, False)


@dataclass
class Alert:
    """A fired alert."""
    rule_name: str
    severity: str
    message: str
    metric_value: float
    threshold: float
    timestamp: str
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AlertEngine:
    """Evaluate alert rules against current metrics."""

    def __init__(
        self,
        rules: List[AlertRule],
        alert_log_path: Optional[Path] = None,
    ):
        """
        Args:
            rules: Alert rules to evaluate
            alert_log_path: Path to append-only JSONL alert log (optional)
        """
        self.rules = rules
        self.alert_log_path = alert_log_path

    def evaluate(
        self,
        metrics: Dict[str, float],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Alert]:
        """Check all rules against provided metrics.

        Args:
            metrics: Current metric values {name: value}
            context: Optional context info to include in alerts

        Returns:
            List of fired alerts
        """
        fired: List[Alert] = []
        ctx = context or {}

        for rule in self.rules:
            value = metrics.get(rule.metric)
            if value is None:
                continue

            if rule.evaluate(value):
                message = rule.message_template.format(
                    value=value,
                    threshold=rule.threshold,
                    metric=rule.metric,
                )
                alert = Alert(
                    rule_name=rule.name,
                    severity=rule.severity.value,
                    message=message,
                    metric_value=value,
                    threshold=rule.threshold,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    context=ctx,
                )
                fired.append(alert)

                if rule.severity == AlertSeverity.CRITICAL:
                    logger.error("CRITICAL ALERT: %s", message)
                else:
                    logger.warning("WARNING ALERT: %s", message)

        # Append to alert log
        if fired and self.alert_log_path:
            self._append_to_log(fired)

        if not fired:
            logger.info("All %d alert rules passed", len(self.rules))

        return fired

    def _append_to_log(self, alerts: List[Alert]) -> None:
        """Append fired alerts to JSONL log."""
        try:
            self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.alert_log_path, "a", encoding="utf-8") as f:
                for alert in alerts:
                    f.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to write alert log: %s", e)

    @classmethod
    def default_rules(cls) -> List[AlertRule]:
        """Return sensible default alert rules for the DR pipeline."""
        return [
            AlertRule(
                name="nogo_ratio_critical",
                metric="nogo_ratio",
                operator="gt",
                threshold=0.80,
                severity=AlertSeverity.CRITICAL,
                message_template="NO-GO ratio {value:.1%} exceeds {threshold:.1%}",
            ),
            AlertRule(
                name="llm_extraction_failure",
                metric="llm_fail_rate",
                operator="gt",
                threshold=0.20,
                severity=AlertSeverity.CRITICAL,
                message_template="LLM extraction failure rate {value:.1%} exceeds {threshold:.1%}",
            ),
            AlertRule(
                name="pubmed_latency_warning",
                metric="pubmed_p95_latency",
                operator="gt",
                threshold=10.0,
                severity=AlertSeverity.WARNING,
                message_template="PubMed p95 latency {value:.1f}s exceeds {threshold:.1f}s",
            ),
            AlertRule(
                name="zero_benefit_drugs",
                metric="zero_benefit_count",
                operator="gt",
                threshold=0,
                severity=AlertSeverity.WARNING,
                message_template="{value:.0f} drugs have zero benefit papers",
            ),
            AlertRule(
                name="pipeline_duration_warning",
                metric="pipeline_duration_minutes",
                operator="gt",
                threshold=60.0,
                severity=AlertSeverity.WARNING,
                message_template="Pipeline took {value:.1f} min (threshold: {threshold:.1f} min)",
            ),
            AlertRule(
                name="low_recall_critical",
                metric="gold_recall",
                operator="lt",
                threshold=0.30,
                severity=AlertSeverity.CRITICAL,
                message_template="Gold recall {value:.1%} below minimum {threshold:.1%}",
            ),
        ]


def load_alert_config(path: Path) -> List[AlertRule]:
    """Load alert rules from YAML config file.

    Expected format:
        alerts:
          - name: nogo_ratio_high
            metric: nogo_ratio
            operator: gt
            threshold: 0.80
            severity: critical
            message: "NO-GO ratio {value:.1%} exceeds {threshold:.1%}"
    """
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    rules = []
    for rule_cfg in config.get("alerts", []):
        severity = AlertSeverity(rule_cfg.get("severity", "warning").lower())
        rules.append(AlertRule(
            name=rule_cfg["name"],
            metric=rule_cfg["metric"],
            operator=rule_cfg["operator"],
            threshold=float(rule_cfg["threshold"]),
            severity=severity,
            message_template=rule_cfg.get("message", "{metric}: {value} vs {threshold}"),
        ))

    return rules
