"""Release gate: blocks shortlist publication if quality criteria aren't met.

Three check categories:
1. Shortlist composition: NO-GO drugs blocked, minimum GO ratio
2. Human review: kill rate, miss rate, IRR thresholds
3. (Optional) Quality gate: model metric thresholds from kg_explain

Usage:
    gate = ReleaseGate(ReleaseGateConfig())
    result = gate.check_all(shortlist_df, review_path="reviews.csv")
    if not result.passed:
        print("BLOCKED:", result.summary())
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from ..evaluation.human_review import (
    ReviewRecord,
    compute_error_rates,
    compute_review_irr,
    load_reviews,
)
from ..logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ReleaseGateConfig:
    """Configuration for release gate checks.

    Attributes:
        block_nogo: If True, any NO-GO entry in the shortlist blocks release.
        min_go_ratio: Minimum fraction of entries that must be GO (0-1).
        max_kill_rate: Maximum acceptable kill rate from human review.
        max_miss_rate: Maximum acceptable miss rate from human review.
        min_irr_kappa: Minimum inter-rater reliability (Cohen's Kappa).
        require_dual_review: If True, require dual reviews for IRR computation.
        min_review_count: Minimum number of human reviews required.
        strict: If True, blockers cause result.passed=False.
                If False, blockers are demoted to warnings and result.passed=True.
    """
    block_nogo: bool = True
    min_go_ratio: float = 0.5
    max_kill_rate: float = 0.15
    max_miss_rate: float = 0.10
    min_irr_kappa: float = 0.6
    require_dual_review: bool = True
    min_review_count: int = 5
    strict: bool = True


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------

@dataclass
class ReleaseCheckResult:
    """Result of a release gate check.

    Attributes:
        blockers: Issues that block release (empty if passed in strict mode).
        warnings: Non-blocking issues worth noting.
        metrics: Computed metrics from the checks.
        strict: Whether strict mode was used.
    """
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    strict: bool = True

    @property
    def passed(self) -> bool:
        """True if there are no blockers, or if strict=False."""
        if not self.strict:
            return True
        return len(self.blockers) == 0

    def summary(self) -> str:
        """Human-readable summary of check results."""
        status = "PASSED" if self.passed else "BLOCKED"
        lines = [f"Release Gate: {status}"]

        if self.blockers:
            lines.append(f"Blockers ({len(self.blockers)}):")
            for b in self.blockers:
                lines.append(f"  - {b}")

        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  - {w}")

        if self.metrics:
            lines.append("Metrics:")
            for k, v in sorted(self.metrics.items()):
                if isinstance(v, float):
                    lines.append(f"  {k}: {v:.4f}")
                else:
                    lines.append(f"  {k}: {v}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Release Gate
# ---------------------------------------------------------------------------

class ReleaseGate:
    """Enforces shortlist composition and human review quality gates.

    Args:
        config: Configuration for gate thresholds and behavior.
    """

    def __init__(self, config: Optional[ReleaseGateConfig] = None):
        self.config = config or ReleaseGateConfig()

    # ------------------------------------------------------------------
    # Shortlist composition
    # ------------------------------------------------------------------

    def check_shortlist_composition(
        self, shortlist_df: pd.DataFrame
    ) -> ReleaseCheckResult:
        """Check shortlist composition: NO-GO presence and GO ratio.

        Looks for a 'gate' or 'gate_decision' column to determine decision
        values. NO-GO entries are those where the column contains 'NO-GO'.

        Args:
            shortlist_df: The shortlist DataFrame.

        Returns:
            ReleaseCheckResult with any blockers/warnings.
        """
        result = ReleaseCheckResult(strict=self.config.strict)

        if shortlist_df.empty:
            result.blockers.append("shortlist is empty")
            result.metrics["n_entries"] = 0
            if not self.config.strict:
                result.warnings.extend(result.blockers)
                result.blockers = []
            return result

        # Find gate column
        gate_col = self._find_gate_column(shortlist_df)
        if gate_col is None:
            result.warnings.append(
                "no 'gate' or 'gate_decision' column found; "
                "cannot check composition"
            )
            result.metrics["n_entries"] = len(shortlist_df)
            return result

        gate_values = shortlist_df[gate_col].astype(str).str.strip().str.upper()
        n_total = len(gate_values)
        n_go = int((gate_values == "GO").sum())
        n_nogo = int((gate_values == "NO-GO").sum())
        n_maybe = int((gate_values == "MAYBE").sum())
        go_ratio = n_go / n_total if n_total > 0 else 0.0

        result.metrics.update({
            "n_entries": n_total,
            "n_go": n_go,
            "n_nogo": n_nogo,
            "n_maybe": n_maybe,
            "go_ratio": round(go_ratio, 4),
        })

        blockers: List[str] = []

        # Check NO-GO presence
        if self.config.block_nogo and n_nogo > 0:
            blockers.append(
                f"{n_nogo} NO-GO entries in shortlist (block_nogo=True)"
            )

        # Check GO ratio
        if go_ratio < self.config.min_go_ratio:
            blockers.append(
                f"GO ratio {go_ratio:.2%} < minimum {self.config.min_go_ratio:.2%}"
            )

        if self.config.strict:
            result.blockers.extend(blockers)
        else:
            result.warnings.extend(blockers)

        return result

    # ------------------------------------------------------------------
    # Human review
    # ------------------------------------------------------------------

    def check_human_review(
        self,
        reviews: Union[str, Path, List[ReviewRecord]],
    ) -> ReleaseCheckResult:
        """Check human review quality metrics.

        Args:
            reviews: Either a file path (str/Path) to a reviews CSV,
                     or a list of ReviewRecord objects.

        Returns:
            ReleaseCheckResult with any blockers/warnings.
        """
        result = ReleaseCheckResult(strict=self.config.strict)

        # Load reviews if path
        if isinstance(reviews, (str, Path)):
            review_list = load_reviews(str(reviews))
        else:
            review_list = list(reviews)

        if len(review_list) < self.config.min_review_count:
            blocker = (
                f"only {len(review_list)} reviews "
                f"(minimum {self.config.min_review_count})"
            )
            if self.config.strict:
                result.blockers.append(blocker)
            else:
                result.warnings.append(blocker)
            result.metrics["n_reviews"] = len(review_list)
            return result

        # Error rates
        rates = compute_error_rates(review_list)
        kill_rate = rates["kill_rate"]
        miss_rate = rates["miss_rate"]
        error_rate = rates["error_rate"]

        # IRR
        irr = compute_review_irr(review_list)

        result.metrics.update({
            "n_reviews": len(review_list),
            "kill_rate": kill_rate,
            "miss_rate": miss_rate,
            "error_rate": error_rate,
            "irr_kappa": irr,
        })

        blockers: List[str] = []

        if kill_rate > self.config.max_kill_rate:
            blockers.append(
                f"kill rate {kill_rate:.2%} > max {self.config.max_kill_rate:.2%}"
            )

        if miss_rate > self.config.max_miss_rate:
            blockers.append(
                f"miss rate {miss_rate:.2%} > max {self.config.max_miss_rate:.2%}"
            )

        if self.config.require_dual_review and irr < 0:
            blockers.append(
                "dual reviews required but IRR not computable "
                "(need at least 2 reviewers per drug)"
            )
        elif irr >= 0 and irr < self.config.min_irr_kappa:
            blockers.append(
                f"IRR kappa {irr:.3f} < minimum {self.config.min_irr_kappa:.3f}"
            )

        if self.config.strict:
            result.blockers.extend(blockers)
        else:
            result.warnings.extend(blockers)

        return result

    # ------------------------------------------------------------------
    # Combined check
    # ------------------------------------------------------------------

    def check_all(
        self,
        shortlist_df: pd.DataFrame,
        reviews: Optional[Union[str, Path, List[ReviewRecord]]] = None,
    ) -> ReleaseCheckResult:
        """Run all release gate checks and combine results.

        Args:
            shortlist_df: The shortlist DataFrame.
            reviews: Optional reviews (path or list). If None, human review
                     check is skipped.

        Returns:
            Combined ReleaseCheckResult.
        """
        combined = ReleaseCheckResult(strict=self.config.strict)

        # Shortlist composition
        comp_result = self.check_shortlist_composition(shortlist_df)
        combined.blockers.extend(comp_result.blockers)
        combined.warnings.extend(comp_result.warnings)
        combined.metrics.update(
            {f"composition.{k}": v for k, v in comp_result.metrics.items()}
        )

        # Human review (optional)
        if reviews is not None:
            review_result = self.check_human_review(reviews)
            combined.blockers.extend(review_result.blockers)
            combined.warnings.extend(review_result.warnings)
            combined.metrics.update(
                {f"review.{k}": v for k, v in review_result.metrics.items()}
            )

        logger.info("Release gate check: %s", combined.summary())
        return combined

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any]) -> "ReleaseGate":
        """Create a ReleaseGate from a config dict (e.g. YAML section).

        Args:
            config_dict: Dict with config keys matching ReleaseGateConfig fields.

        Returns:
            Configured ReleaseGate instance.
        """
        cfg = ReleaseGateConfig(
            block_nogo=config_dict.get("block_nogo", True),
            min_go_ratio=float(config_dict.get("min_go_ratio", 0.5)),
            max_kill_rate=float(config_dict.get("max_kill_rate", 0.15)),
            max_miss_rate=float(config_dict.get("max_miss_rate", 0.10)),
            min_irr_kappa=float(config_dict.get("min_irr_kappa", 0.6)),
            require_dual_review=config_dict.get("require_dual_review", True),
            min_review_count=int(config_dict.get("min_review_count", 5)),
            strict=config_dict.get("strict", True),
        )
        return cls(config=cfg)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_gate_column(df: pd.DataFrame) -> Optional[str]:
        """Find the gate decision column in a DataFrame."""
        for col in ("gate", "gate_decision"):
            if col in df.columns:
                return col
        return None
