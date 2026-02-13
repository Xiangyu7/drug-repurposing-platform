"""Tests for dr.common.audit_log.

Covers:
- Append entries and verify hash chain
- verify_chain on valid log -> True
- Tamper with an entry, verify_chain -> False with issues
- Genesis hash for first entry
- Query by actor, action, since, until
- Invalid role/action raises ValueError
- read_all on empty file -> empty list
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dr.common.audit_log import (
    AuditLog,
    AuditEntry,
    GENESIS_HASH,
    VALID_ROLES,
    VALID_ACTIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _append_sample(log: AuditLog, actor: str = "admin", n: int = 1) -> list[AuditEntry]:
    """Append n sample entries and return them."""
    entries = []
    for i in range(n):
        entry = log.append(
            actor=actor,
            role="admin",
            action="pipeline_run",
            payload={"step": i, "note": f"entry_{i}"},
        )
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Tests: Append and hash chain
# ---------------------------------------------------------------------------

class TestAppendAndHashChain:
    """Tests for appending entries and verifying the hash chain."""

    def test_append_single_entry(self, tmp_path):
        """Appending one entry should create a valid log."""
        log = AuditLog(tmp_path / "audit.jsonl")
        entry = log.append("alice", "admin", "pipeline_run", {"v": "5"})

        assert entry.actor == "alice"
        assert entry.role == "admin"
        assert entry.action == "pipeline_run"
        assert entry.payload == {"v": "5"}
        assert entry.entry_hash != ""
        assert entry.prev_hash == GENESIS_HASH

    def test_append_multiple_entries_chain(self, tmp_path):
        """Multiple entries should form a hash chain."""
        log = AuditLog(tmp_path / "audit.jsonl")
        e1 = log.append("alice", "admin", "pipeline_run", {"step": 1})
        e2 = log.append("bob", "reviewer", "review_submit", {"drug": "aspirin"})
        e3 = log.append("system", "pipeline", "pipeline_complete", {"status": "ok"})

        # Chain: genesis -> e1 -> e2 -> e3
        assert e1.prev_hash == GENESIS_HASH
        assert e2.prev_hash == e1.entry_hash
        assert e3.prev_hash == e2.entry_hash

    def test_verify_chain_valid(self, tmp_path):
        """verify_chain on an untampered log should return (True, [])."""
        log = AuditLog(tmp_path / "audit.jsonl")
        _append_sample(log, n=5)

        is_valid, issues = log.verify_chain()
        assert is_valid is True
        assert issues == []

    def test_verify_chain_tampered_entry(self, tmp_path):
        """Modifying an entry should cause verify_chain to fail."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path)
        _append_sample(log, n=3)

        # Tamper with the second entry (index 1)
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        entry = json.loads(lines[1])
        entry["actor"] = "TAMPERED_ACTOR"
        lines[1] = json.dumps(entry, ensure_ascii=False)

        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Re-open and verify
        log2 = AuditLog(log_path)
        is_valid, issues = log2.verify_chain()
        assert is_valid is False
        assert len(issues) > 0
        # Should detect hash mismatch on entry 1
        assert any("Entry 1" in issue for issue in issues)

    def test_verify_chain_broken_link(self, tmp_path):
        """Deleting an entry should break the chain linkage."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path)
        _append_sample(log, n=3)

        # Remove the second line (break chain: entry 2 expects entry 1's hash)
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        del lines[1]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log2 = AuditLog(log_path)
        is_valid, issues = log2.verify_chain()
        assert is_valid is False
        assert len(issues) > 0


# ---------------------------------------------------------------------------
# Tests: Genesis hash
# ---------------------------------------------------------------------------

class TestGenesisHash:
    """Tests for the genesis hash on first entry."""

    def test_genesis_hash_is_64_zeros(self):
        """Genesis hash should be 64 zero characters."""
        assert GENESIS_HASH == "0" * 64
        assert len(GENESIS_HASH) == 64

    def test_first_entry_prev_hash_is_genesis(self, tmp_path):
        """First entry in a new log should have prev_hash == GENESIS_HASH."""
        log = AuditLog(tmp_path / "audit.jsonl")
        entry = log.append("user", "admin", "pipeline_run", {})
        assert entry.prev_hash == GENESIS_HASH

    def test_empty_log_last_hash_is_genesis(self, tmp_path):
        """An empty log should return genesis hash for _get_last_hash."""
        log = AuditLog(tmp_path / "audit.jsonl")
        assert log._get_last_hash() == GENESIS_HASH


# ---------------------------------------------------------------------------
# Tests: Query
# ---------------------------------------------------------------------------

class TestQuery:
    """Tests for querying audit log entries."""

    def test_query_by_actor(self, tmp_path):
        """Query by actor should filter correctly."""
        log = AuditLog(tmp_path / "audit.jsonl")
        log.append("alice", "admin", "pipeline_run", {})
        log.append("bob", "reviewer", "review_submit", {})
        log.append("alice", "admin", "pipeline_complete", {})

        results = log.query(actor="alice")
        assert len(results) == 2
        assert all(e.actor == "alice" for e in results)

    def test_query_by_action(self, tmp_path):
        """Query by action should filter correctly."""
        log = AuditLog(tmp_path / "audit.jsonl")
        log.append("alice", "admin", "pipeline_run", {})
        log.append("bob", "reviewer", "review_submit", {})
        log.append("carol", "admin", "pipeline_run", {})

        results = log.query(action="pipeline_run")
        assert len(results) == 2
        assert all(e.action == "pipeline_run" for e in results)

    def test_query_by_since(self, tmp_path):
        """Query with since filter should return entries >= since."""
        log = AuditLog(tmp_path / "audit.jsonl")
        e1 = log.append("alice", "admin", "pipeline_run", {})
        e2 = log.append("bob", "admin", "pipeline_run", {})
        e3 = log.append("carol", "admin", "pipeline_run", {})

        # Use e2's timestamp as the since filter
        results = log.query(since=e2.timestamp)
        assert len(results) >= 2  # e2 and e3 (and possibly e1 if same timestamp)

    def test_query_by_until(self, tmp_path):
        """Query with until filter should return entries <= until."""
        log = AuditLog(tmp_path / "audit.jsonl")
        e1 = log.append("alice", "admin", "pipeline_run", {})
        e2 = log.append("bob", "admin", "pipeline_run", {})
        e3 = log.append("carol", "admin", "pipeline_run", {})

        # Use e1's timestamp as the until filter
        results = log.query(until=e1.timestamp)
        assert len(results) >= 1  # at least e1

    def test_query_combined_filters(self, tmp_path):
        """Multiple query filters should be combined (AND logic)."""
        log = AuditLog(tmp_path / "audit.jsonl")
        log.append("alice", "admin", "pipeline_run", {})
        log.append("alice", "admin", "pipeline_complete", {})
        log.append("bob", "reviewer", "review_submit", {})

        results = log.query(actor="alice", action="pipeline_run")
        assert len(results) == 1
        assert results[0].actor == "alice"
        assert results[0].action == "pipeline_run"

    def test_query_no_matches(self, tmp_path):
        """Query with no matching entries should return empty list."""
        log = AuditLog(tmp_path / "audit.jsonl")
        log.append("alice", "admin", "pipeline_run", {})

        results = log.query(actor="nonexistent")
        assert results == []


# ---------------------------------------------------------------------------
# Tests: Invalid role/action
# ---------------------------------------------------------------------------

class TestInvalidRoleAction:
    """Tests for ValueError on invalid roles and actions."""

    def test_invalid_role_raises(self, tmp_path):
        """Invalid role should raise ValueError."""
        log = AuditLog(tmp_path / "audit.jsonl")
        with pytest.raises(ValueError, match="[Ii]nvalid role"):
            log.append("user", "superuser", "pipeline_run", {})

    def test_invalid_action_raises(self, tmp_path):
        """Invalid action should raise ValueError."""
        log = AuditLog(tmp_path / "audit.jsonl")
        with pytest.raises(ValueError, match="[Ii]nvalid action"):
            log.append("user", "admin", "delete_everything", {})

    def test_all_valid_roles_accepted(self, tmp_path):
        """All defined VALID_ROLES should be accepted."""
        log = AuditLog(tmp_path / "audit.jsonl")
        for role in VALID_ROLES:
            entry = log.append("tester", role, "pipeline_run", {"role": role})
            assert entry.role == role

    def test_all_valid_actions_accepted(self, tmp_path):
        """All defined VALID_ACTIONS should be accepted."""
        log = AuditLog(tmp_path / "audit.jsonl")
        for action in VALID_ACTIONS:
            entry = log.append("tester", "admin", action, {"action": action})
            assert entry.action == action


# ---------------------------------------------------------------------------
# Tests: read_all
# ---------------------------------------------------------------------------

class TestReadAll:
    """Tests for reading all entries from the log."""

    def test_read_all_empty_file(self, tmp_path):
        """read_all on an empty file should return empty list."""
        log = AuditLog(tmp_path / "audit.jsonl")
        entries = log.read_all()
        assert entries == []

    def test_read_all_returns_correct_count(self, tmp_path):
        """read_all should return all appended entries."""
        log = AuditLog(tmp_path / "audit.jsonl")
        _append_sample(log, n=5)

        entries = log.read_all()
        assert len(entries) == 5

    def test_read_all_preserves_order(self, tmp_path):
        """read_all should return entries in append order."""
        log = AuditLog(tmp_path / "audit.jsonl")
        log.append("first", "admin", "pipeline_run", {"order": 1})
        log.append("second", "admin", "pipeline_run", {"order": 2})
        log.append("third", "admin", "pipeline_run", {"order": 3})

        entries = log.read_all()
        assert entries[0].actor == "first"
        assert entries[1].actor == "second"
        assert entries[2].actor == "third"

    def test_read_all_entries_are_audit_entry_type(self, tmp_path):
        """All entries from read_all should be AuditEntry instances."""
        log = AuditLog(tmp_path / "audit.jsonl")
        _append_sample(log, n=3)

        entries = log.read_all()
        for entry in entries:
            assert isinstance(entry, AuditEntry)

    def test_read_all_skips_corrupt_lines(self, tmp_path):
        """Corrupt JSONL lines should be skipped, valid ones returned."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path)
        log.append("alice", "admin", "pipeline_run", {})

        # Append a corrupt line
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("{this is not valid json}\n")

        log.append("bob", "admin", "pipeline_run", {})

        entries = log.read_all()
        # Should get 2 valid entries (alice and bob), skip the corrupt one
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Tests: AuditEntry
# ---------------------------------------------------------------------------

class TestAuditEntry:
    """Tests for the AuditEntry dataclass."""

    def test_compute_hash_deterministic(self):
        """Same entry data should produce same hash."""
        e1 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            actor="alice",
            role="admin",
            action="pipeline_run",
            payload={"v": 1},
            prev_hash=GENESIS_HASH,
        )
        h1 = e1.compute_hash()
        h2 = e1.compute_hash()
        assert h1 == h2

    def test_compute_hash_changes_with_data(self):
        """Different data should produce different hashes."""
        e1 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            actor="alice",
            role="admin",
            action="pipeline_run",
            payload={},
            prev_hash=GENESIS_HASH,
        )
        e2 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            actor="bob",  # different actor
            role="admin",
            action="pipeline_run",
            payload={},
            prev_hash=GENESIS_HASH,
        )
        assert e1.compute_hash() != e2.compute_hash()

    def test_to_dict(self):
        """to_dict should return all fields."""
        e = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            actor="alice",
            role="admin",
            action="pipeline_run",
            payload={"key": "value"},
            prev_hash=GENESIS_HASH,
            entry_hash="abc123",
        )
        d = e.to_dict()
        assert d["actor"] == "alice"
        assert d["role"] == "admin"
        assert d["entry_hash"] == "abc123"
        assert d["payload"] == {"key": "value"}
        assert d["prev_hash"] == GENESIS_HASH

    def test_hash_excludes_entry_hash_field(self):
        """compute_hash should not include entry_hash itself (avoids circular ref)."""
        e = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            actor="alice",
            role="admin",
            action="pipeline_run",
            payload={},
            prev_hash=GENESIS_HASH,
            entry_hash="should_not_affect",
        )
        h1 = e.compute_hash()

        e.entry_hash = "different_value"
        h2 = e.compute_hash()

        assert h1 == h2  # entry_hash is excluded from hash computation
