"""Quality gate: blocks release if metrics fall below thresholds.

Evaluates current pipeline metrics against configured thresholds
and optionally checks for regression from a baseline version.

Usage:
    gate = QualityGate({"hit@10": 0.50, "mrr": 0.25}, regression_tolerance=0.05)
    result = gate.check(current_metrics, baseline_metrics)
    if not result.passed:
        print("BLOCKED:", result.failures)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class QualityGateResult:
    """Result of a quality gate check."""
    passed: bool
    failures: List[str] = field(default_factory=list)
    regressions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    baseline_metrics: Optional[Dict[str, float]] = None

    def summary(self) -> str:
        status = "PASSED" if self.passed else "BLOCKED"
        lines = [f"Quality Gate: {status}"]
        if self.failures:
            lines.append(f"  Failures: {'; '.join(self.failures)}")
        if self.regressions:
            lines.append(f"  Regressions: {'; '.join(self.regressions)}")
        if self.warnings:
            lines.append(f"  Warnings: {'; '.join(self.warnings)}")
        return "\n".join(lines)


class QualityGate:
    """Evaluates whether pipeline output meets quality thresholds.

    Args:
        thresholds: {metric_name: minimum_value}
        regression_tolerance: Maximum allowed drop from baseline (e.g., 0.05 = 5%)
        warning_margin: Metrics within this margin of threshold trigger warnings
    """

    def __init__(
        self,
        thresholds: Dict[str, float],
        regression_tolerance: float = 0.05,
        warning_margin: float = 0.03,
    ):
        self.thresholds = thresholds
        self.regression_tolerance = regression_tolerance
        self.warning_margin = warning_margin

    def check(
        self,
        metrics: Dict[str, float],
        baseline_metrics: Optional[Dict[str, float]] = None,
    ) -> QualityGateResult:
        """Run quality gate check.

        Args:
            metrics: Current pipeline metrics
            baseline_metrics: Previous approved version's metrics (optional)

        Returns:
            QualityGateResult with pass/fail and details
        """
        failures: List[str] = []
        regressions: List[str] = []
        warnings: List[str] = []

        # Check absolute thresholds
        for metric_name, min_value in self.thresholds.items():
            current = metrics.get(metric_name)
            if current is None:
                warnings.append(f"{metric_name}: not found in metrics")
                continue

            if current < min_value:
                failures.append(
                    f"{metric_name}: {current:.4f} < threshold {min_value:.4f}"
                )
            elif current < min_value + self.warning_margin:
                warnings.append(
                    f"{metric_name}: {current:.4f} within {self.warning_margin:.2f} "
                    f"of threshold {min_value:.4f}"
                )

        # Check regression from baseline
        if baseline_metrics:
            for metric_name in self.thresholds:
                current = metrics.get(metric_name)
                baseline = baseline_metrics.get(metric_name)
                if current is None or baseline is None:
                    continue

                drop = baseline - current
                if drop > self.regression_tolerance:
                    regressions.append(
                        f"{metric_name}: dropped {drop:.4f} from baseline "
                        f"{baseline:.4f} (tolerance: {self.regression_tolerance:.4f})"
                    )

        passed = len(failures) == 0 and len(regressions) == 0

        result = QualityGateResult(
            passed=passed,
            failures=failures,
            regressions=regressions,
            warnings=warnings,
            metrics=metrics,
            baseline_metrics=baseline_metrics,
        )

        if passed:
            logger.info("Quality gate PASSED")
        else:
            logger.warning("Quality gate BLOCKED: %s", result.summary())

        return result

    @classmethod
    def from_config(cls, config_dict: dict) -> "QualityGate":
        """Create from YAML config section.

        Expected format:
            quality_gate:
              thresholds:
                "hit@10": 0.50
                mrr: 0.25
              regression_tolerance: 0.05
        """
        thresholds = config_dict.get("thresholds", {})
        tolerance = float(config_dict.get("regression_tolerance", 0.05))
        margin = float(config_dict.get("warning_margin", 0.03))
        return cls(thresholds=thresholds, regression_tolerance=tolerance, warning_margin=margin)
