"""Run provenance helpers for reproducibility manifests."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA256 for a file path."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _run_git(args: List[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def detect_git_state(repo_root: Path) -> Dict[str, Any]:
    """Best-effort git metadata for provenance."""
    commit = _run_git(["rev-parse", "HEAD"], repo_root)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    dirty_text = _run_git(["status", "--porcelain"], repo_root)
    return {
        "commit": commit or "unknown",
        "branch": branch or "unknown",
        "dirty": bool(dirty_text),
    }


def collect_file_hashes(paths: Iterable[Path]) -> Dict[str, str]:
    """Hash existing files from a path list."""
    hashed: Dict[str, str] = {}
    seen = set()
    for p in paths:
        path = Path(p)
        if path in seen:
            continue
        seen.add(path)
        if path.exists() and path.is_file():
            hashed[str(path)] = sha256_file(path)
    return hashed


def build_manifest(
    pipeline: str,
    repo_root: Path,
    input_files: Iterable[Path],
    output_files: Iterable[Path],
    config: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
    contracts: Optional[Dict[str, str]] = None,
    audit_log: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a standard manifest payload.

    Args:
        audit_log: Optional AuditLog instance. If provided, appends a
                   'pipeline_run' entry to the audit log.
    """
    manifest = {
        "pipeline": pipeline,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "git": detect_git_state(repo_root),
        "contracts": contracts or {},
        "config": config or {},
        "summary": summary or {},
        "inputs": {
            "files": collect_file_hashes(input_files),
        },
        "outputs": {
            "files": collect_file_hashes(output_files),
        },
    }

    # Optionally record to audit log
    if audit_log is not None:
        try:
            audit_log.append(
                actor="pipeline",
                role="pipeline",
                action="pipeline_run",
                payload={
                    "pipeline": pipeline,
                    "git_commit": manifest["git"].get("commit", "unknown"),
                },
            )
        except Exception:
            pass  # Don't fail manifest creation if audit log fails

    return manifest


def write_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    """Write a manifest as pretty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
