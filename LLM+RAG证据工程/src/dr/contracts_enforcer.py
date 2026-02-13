"""Contract enforcement for pipeline data schemas.

Wraps existing validators in contracts.py with enforcement logic:
- strict=True (default): raises ContractViolationError on any violation
- strict=False: logs warnings but continues

Usage:
    enforcer = ContractEnforcer(strict=True)
    enforcer.check_step7_scores(scores_df)  # Raises if columns missing

    enforcer = ContractEnforcer(strict=False)
    enforcer.check_step7_scores(scores_df)  # Logs warning if columns missing
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import pandas as pd

from .logger import get_logger
from .contracts import (
    validate_step6_dossier,
    validate_step7_scores_columns,
    validate_step7_gating_columns,
    validate_step7_cards,
    validate_step8_shortlist_columns,
    validate_step9_plan_columns,
    stamp_step6_dossier_contract,
)

logger = get_logger(__name__)


class ContractViolationError(Exception):
    """Raised when pipeline data violates its schema contract."""

    def __init__(self, step: str, violations: List[str]):
        self.step = step
        self.violations = violations
        msg = f"Contract violation in {step} ({len(violations)} issues):\n" + \
              "\n".join(f"  - {v}" for v in violations)
        super().__init__(msg)


class ContractEnforcer:
    """Enforces pipeline data contracts with configurable strictness.

    Args:
        strict: If True, raise ContractViolationError on violations.
                If False, log warnings and continue.
    """

    def __init__(self, strict: bool = True):
        self.strict = strict
        self._violation_log: List[Dict[str, Any]] = []

    @property
    def violations(self) -> List[Dict[str, Any]]:
        """All violations recorded during this enforcer's lifetime."""
        return list(self._violation_log)

    def _handle_violations(self, step: str, issues: List[str]) -> None:
        """Handle detected violations based on strictness mode."""
        if not issues:
            return

        self._violation_log.append({"step": step, "issues": issues})

        if self.strict:
            raise ContractViolationError(step, issues)
        else:
            for issue in issues:
                logger.warning("Contract warning [%s]: %s", step, issue)

    def check_step6_dossier(
        self, dossier: Dict[str, Any], producer: str = ""
    ) -> Dict[str, Any]:
        """Validate and stamp a Step6 dossier.

        Args:
            dossier: The dossier dict to validate
            producer: Producer identifier for contract stamp

        Returns:
            The dossier (with contract stamp if valid)
        """
        issues = validate_step6_dossier(dossier)
        self._handle_violations("step6_dossier", issues)
        if not issues and producer:
            stamp_step6_dossier_contract(dossier, producer)
        return dossier

    def check_step7_scores(self, df: pd.DataFrame) -> None:
        """Validate Step7 scores CSV columns."""
        issues = validate_step7_scores_columns(df.columns)
        self._handle_violations("step7_scores", issues)

    def check_step7_gating(self, df: pd.DataFrame) -> None:
        """Validate Step7 gating CSV columns."""
        issues = validate_step7_gating_columns(df.columns)
        self._handle_violations("step7_gating", issues)

    def check_step7_cards(self, cards: Any) -> None:
        """Validate Step7 cards JSON structure."""
        issues = validate_step7_cards(cards)
        self._handle_violations("step7_cards", issues)

    def check_step8_shortlist(self, df: pd.DataFrame) -> None:
        """Validate Step8 shortlist CSV columns."""
        issues = validate_step8_shortlist_columns(df.columns)
        self._handle_violations("step8_shortlist", issues)

    def check_step9_plan(self, df: pd.DataFrame) -> None:
        """Validate Step9 validation plan CSV columns."""
        issues = validate_step9_plan_columns(df.columns)
        self._handle_violations("step9_plan", issues)

    def clear_log(self) -> None:
        """Clear the violation log."""
        self._violation_log.clear()


_default_enforcer: Optional[ContractEnforcer] = None


def default_enforcer(strict: bool = True) -> ContractEnforcer:
    """Get or create the default singleton enforcer."""
    global _default_enforcer
    if _default_enforcer is None or _default_enforcer.strict != strict:
        _default_enforcer = ContractEnforcer(strict=strict)
    return _default_enforcer
