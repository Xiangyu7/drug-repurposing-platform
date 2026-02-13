"""Model version registry for tracking pipeline configuration snapshots.

Each "model version" is a combination of:
  - Ranker version (v3/v5)
  - Config YAML hash
  - Data file hashes (edge CSVs)
  - Evaluation metrics at registration time

Storage: JSON file (append-safe, human-readable).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModelVersion:
    """A registered model version snapshot."""
    version_id: str
    ranker_version: str
    config_hash: str
    data_hashes: Dict[str, str]
    metrics: Dict[str, float]
    created_at: str
    git_commit: str = ""
    notes: str = ""
    status: str = "candidate"  # candidate | approved | deprecated

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModelRegistry:
    """File-based model version registry.

    Usage:
        registry = ModelRegistry(Path("output/model_registry.json"))
        version = registry.register("v5", config_path, data_dir, metrics)
        registry.approve(version.version_id)
    """

    def __init__(self, registry_path: Path):
        self.path = registry_path
        self._versions: List[ModelVersion] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        """Load existing registry from JSON."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._versions = [ModelVersion(**v) for v in data.get("versions", [])]
            logger.info("Loaded %d model versions from %s", len(self._versions), self.path)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load registry %s: %s", self.path, e)
            self._versions = []

    def _save(self) -> None:
        """Persist registry to JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"versions": [v.to_dict() for v in self._versions]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _hash_file(path: Path) -> str:
        """SHA256 hash of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _hash_config(config_path: Path) -> str:
        """SHA256 hash of config file content."""
        if not config_path.exists():
            return "missing"
        return ModelRegistry._hash_file(config_path)

    def register(
        self,
        ranker_version: str,
        config_path: Path,
        data_dir: Path,
        metrics: Dict[str, float],
        git_commit: str = "",
        notes: str = "",
    ) -> ModelVersion:
        """Register a new model version.

        Args:
            ranker_version: e.g., "v5"
            config_path: Path to the YAML config used
            data_dir: Directory with input CSV files
            metrics: Evaluation metrics {name: value}
            git_commit: Git commit hash
            notes: Human-readable notes

        Returns:
            The created ModelVersion
        """
        config_hash = self._hash_config(config_path)

        # Hash key data files
        data_hashes: Dict[str, str] = {}
        if data_dir.exists():
            for csv_file in sorted(data_dir.glob("*.csv")):
                data_hashes[csv_file.name] = self._hash_file(csv_file)

        now = datetime.now(timezone.utc).isoformat()
        short_hash = config_hash[:8]
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        version_id = f"{ranker_version}-{date_str}-{short_hash}"

        # Ensure unique ID
        existing_ids = {v.version_id for v in self._versions}
        counter = 1
        base_id = version_id
        while version_id in existing_ids:
            version_id = f"{base_id}-{counter}"
            counter += 1

        version = ModelVersion(
            version_id=version_id,
            ranker_version=ranker_version,
            config_hash=config_hash,
            data_hashes=data_hashes,
            metrics={k: round(float(v), 6) for k, v in metrics.items()},
            created_at=now,
            git_commit=git_commit,
            notes=notes,
            status="candidate",
        )

        self._versions.append(version)
        self._save()
        logger.info("Registered model version: %s", version_id)
        return version

    def get_latest(self, ranker_version: str = "v5") -> Optional[ModelVersion]:
        """Get the latest approved version for a ranker."""
        approved = [
            v for v in self._versions
            if v.ranker_version == ranker_version and v.status == "approved"
        ]
        return approved[-1] if approved else None

    def get_baseline(self, ranker_version: str = "v5") -> Optional[ModelVersion]:
        """Get the baseline (first approved) version."""
        approved = [
            v for v in self._versions
            if v.ranker_version == ranker_version and v.status == "approved"
        ]
        return approved[0] if approved else None

    def approve(self, version_id: str) -> None:
        """Mark a version as approved."""
        for v in self._versions:
            if v.version_id == version_id:
                v.status = "approved"
                self._save()
                logger.info("Approved model version: %s", version_id)
                return
        raise KeyError(f"Version not found: {version_id}")

    def deprecate(self, version_id: str, reason: str = "") -> None:
        """Deprecate a version."""
        for v in self._versions:
            if v.version_id == version_id:
                v.status = "deprecated"
                v.notes = f"{v.notes} [deprecated: {reason}]".strip()
                self._save()
                logger.info("Deprecated model version: %s (%s)", version_id, reason)
                return
        raise KeyError(f"Version not found: {version_id}")

    def diff(self, version_a: str, version_b: str) -> Dict[str, Any]:
        """Compare two versions: config diff, metric diff, data diff."""
        va = self._find(version_a)
        vb = self._find(version_b)

        metric_diff = {}
        all_metrics = set(va.metrics.keys()) | set(vb.metrics.keys())
        for m in sorted(all_metrics):
            val_a = va.metrics.get(m, 0.0)
            val_b = vb.metrics.get(m, 0.0)
            metric_diff[m] = {"a": val_a, "b": val_b, "delta": round(val_a - val_b, 6)}

        data_diff = {
            "added": [f for f in vb.data_hashes if f not in va.data_hashes],
            "removed": [f for f in va.data_hashes if f not in vb.data_hashes],
            "changed": [
                f for f in va.data_hashes
                if f in vb.data_hashes and va.data_hashes[f] != vb.data_hashes[f]
            ],
        }

        return {
            "version_a": version_a,
            "version_b": version_b,
            "config_changed": va.config_hash != vb.config_hash,
            "metric_diff": metric_diff,
            "data_diff": data_diff,
        }

    def list_versions(
        self,
        ranker_version: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[ModelVersion]:
        """List all versions, optionally filtered."""
        result = self._versions
        if ranker_version:
            result = [v for v in result if v.ranker_version == ranker_version]
        if status:
            result = [v for v in result if v.status == status]
        return result

    def _find(self, version_id: str) -> ModelVersion:
        for v in self._versions:
            if v.version_id == version_id:
                return v
        raise KeyError(f"Version not found: {version_id}")
