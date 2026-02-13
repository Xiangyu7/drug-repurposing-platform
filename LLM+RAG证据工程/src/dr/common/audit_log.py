"""Append-only audit log with tamper detection via hash chain.

Each log entry is a JSON line with:
  - timestamp, actor, role, action, payload
  - prev_hash: SHA256 of previous entry (chain integrity)
  - entry_hash: SHA256 of this entry

The hash chain provides tamper detection: modifying any entry
breaks the chain from that point forward.

Usage:
    log = AuditLog(Path("output/audit.jsonl"))
    log.append("admin", "admin", "pipeline_run", {"version": "v5"})
    is_valid, issues = log.verify_chain()
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_ROLES = {"admin", "reviewer", "pipeline", "readonly"}
VALID_ACTIONS = {
    "pipeline_run", "pipeline_complete", "approval",
    "config_change", "data_access", "model_register",
    "quality_gate_pass", "quality_gate_fail",
    "review_submit", "annotation_update", "export",
}

# Genesis hash for the first entry in a new log
GENESIS_HASH = "0" * 64


@dataclass
class AuditEntry:
    """Single audit log entry."""
    timestamp: str
    actor: str
    role: str
    action: str
    payload: Dict[str, Any]
    prev_hash: str
    entry_hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA256 of this entry (excluding entry_hash field)."""
        d = {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        blob = json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuditLog:
    """Append-only JSONL audit log with hash chain."""

    def __init__(self, log_path: Path):
        """Initialize. Creates file if not exists."""
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self.path.touch()

    def _get_last_hash(self) -> str:
        """Read the hash of the last entry in the log."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS_HASH

        last_line = ""
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_line = line

        if not last_line:
            return GENESIS_HASH

        try:
            entry = json.loads(last_line)
            return entry.get("entry_hash", GENESIS_HASH)
        except json.JSONDecodeError:
            logger.warning("Failed to parse last log entry, using genesis hash")
            return GENESIS_HASH

    def append(
        self,
        actor: str,
        role: str,
        action: str,
        payload: Dict[str, Any],
    ) -> AuditEntry:
        """Append a new entry to the log.

        Args:
            actor: Who performed the action (username or "system")
            role: Actor's role (admin/reviewer/pipeline/readonly)
            action: Action type (pipeline_run/approval/etc.)
            payload: Action-specific data

        Returns:
            The created AuditEntry

        Raises:
            ValueError: If role or action is not in valid sets
        """
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role}. Valid: {VALID_ROLES}")
        if action not in VALID_ACTIONS:
            raise ValueError(f"Invalid action: {action}. Valid: {VALID_ACTIONS}")

        prev_hash = self._get_last_hash()

        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            role=role,
            action=action,
            payload=payload,
            prev_hash=prev_hash,
        )
        entry.entry_hash = entry.compute_hash()

        # Append to JSONL file
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

        logger.debug("Audit: %s %s %s", actor, action, entry.entry_hash[:12])
        return entry

    def verify_chain(self) -> tuple[bool, List[str]]:
        """Verify the hash chain integrity.

        Checks:
        1. Each entry's entry_hash matches its computed hash
        2. Each entry's prev_hash matches the previous entry's entry_hash
        3. First entry's prev_hash is the genesis hash

        Returns:
            (is_valid, list_of_issues)
        """
        entries = self.read_all()
        issues: List[str] = []

        if not entries:
            return True, []

        # Check first entry
        if entries[0].prev_hash != GENESIS_HASH:
            issues.append(
                f"Entry 0: prev_hash should be genesis but is {entries[0].prev_hash[:12]}"
            )

        for i, entry in enumerate(entries):
            # Verify self-hash
            computed = entry.compute_hash()
            if computed != entry.entry_hash:
                issues.append(
                    f"Entry {i}: hash mismatch (stored={entry.entry_hash[:12]}, "
                    f"computed={computed[:12]})"
                )

            # Verify chain linkage
            if i > 0:
                expected_prev = entries[i - 1].entry_hash
                if entry.prev_hash != expected_prev:
                    issues.append(
                        f"Entry {i}: chain break (prev_hash={entry.prev_hash[:12]}, "
                        f"expected={expected_prev[:12]})"
                    )

        is_valid = len(issues) == 0
        if is_valid:
            logger.info("Audit chain verified: %d entries, all valid", len(entries))
        else:
            logger.warning("Audit chain INVALID: %d issues found", len(issues))

        return is_valid, issues

    def read_all(self) -> List[AuditEntry]:
        """Read all entries from the log."""
        entries: List[AuditEntry] = []

        if not self.path.exists() or self.path.stat().st_size == 0:
            return entries

        with open(self.path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entries.append(AuditEntry(**data))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Failed to parse audit entry %d: %s", i, e)

        return entries

    def query(
        self,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[AuditEntry]:
        """Query entries by filter criteria.

        Args:
            actor: Filter by actor name
            action: Filter by action type
            since: Filter entries after this ISO timestamp
            until: Filter entries before this ISO timestamp

        Returns:
            Filtered list of AuditEntry objects
        """
        entries = self.read_all()

        if actor:
            entries = [e for e in entries if e.actor == actor]
        if action:
            entries = [e for e in entries if e.action == action]
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        if until:
            entries = [e for e in entries if e.timestamp <= until]

        return entries
