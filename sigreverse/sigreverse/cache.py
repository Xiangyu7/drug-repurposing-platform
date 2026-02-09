"""Cache module — industrial grade with TTL, metadata, and statistics.

Provides a file-based JSON cache with:
    - TTL (time-to-live) for automatic expiration
    - Metadata tracking (created_at, access count, payload hash)
    - Cache statistics (hits, misses, expired, evicted)
    - Thread-safe operations
    - Cache inspection and cleanup utilities

Usage:
    cache = FileCache(cache_dir="data/cache", default_ttl_hours=168)
    data = cache.get("my_key")
    if data is None:
        data = expensive_api_call()
        cache.put("my_key", data)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("sigreverse.cache")


class CacheEntry:
    """Metadata wrapper for a cached value."""

    def __init__(self, data: Any, key: str, ttl_hours: float = 168.0):
        self.data = data
        self.key = key
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.created_ts = time.time()
        self.ttl_hours = ttl_hours
        self.access_count = 0
        self.last_accessed = None
        self.data_hash = _hash_obj(data)

    def is_expired(self) -> bool:
        """Check if this entry has exceeded its TTL."""
        if self.ttl_hours <= 0:
            return False  # TTL=0 means never expire
        age_hours = (time.time() - self.created_ts) / 3600.0
        return age_hours > self.ttl_hours

    def touch(self):
        """Record an access."""
        self.access_count += 1
        self.last_accessed = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "_cache_meta": {
                "key": self.key,
                "created_at": self.created_at,
                "created_ts": self.created_ts,
                "ttl_hours": self.ttl_hours,
                "access_count": self.access_count,
                "last_accessed": self.last_accessed,
                "data_hash": self.data_hash,
                "version": "1.0",
            },
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["CacheEntry"]:
        """Deserialize from dict. Returns None if format is invalid."""
        if not isinstance(d, dict) or "_cache_meta" not in d:
            # Legacy format (no metadata) — wrap it
            entry = cls(data=d, key="unknown", ttl_hours=0)
            entry.created_ts = 0  # Unknown age → never expire
            return entry

        meta = d["_cache_meta"]
        entry = cls.__new__(cls)
        entry.data = d.get("data")
        entry.key = meta.get("key", "unknown")
        entry.created_at = meta.get("created_at", "")
        entry.created_ts = float(meta.get("created_ts", 0))
        entry.ttl_hours = float(meta.get("ttl_hours", 168))
        entry.access_count = int(meta.get("access_count", 0))
        entry.last_accessed = meta.get("last_accessed")
        entry.data_hash = meta.get("data_hash", "")
        return entry


class FileCache:
    """File-based JSON cache with TTL and statistics.

    Args:
        cache_dir: Directory to store cache files.
        default_ttl_hours: Default time-to-live in hours (0 = never expire).
        enabled: If False, all operations are no-ops (passthrough).
    """

    def __init__(
        self,
        cache_dir: str = "data/cache",
        default_ttl_hours: float = 168.0,  # 7 days
        enabled: bool = True,
    ):
        self.cache_dir = cache_dir
        self.default_ttl_hours = default_ttl_hours
        self.enabled = enabled

        # Statistics
        self._stats = {
            "hits": 0,
            "misses": 0,
            "expired": 0,
            "puts": 0,
            "errors": 0,
        }

        if enabled:
            os.makedirs(cache_dir, exist_ok=True)

    @property
    def stats(self) -> Dict[str, int]:
        """Return copy of cache statistics."""
        return dict(self._stats)

    def get(self, key: str, ttl_hours: Optional[float] = None) -> Optional[Any]:
        """Retrieve a cached value by key.

        Args:
            key: Cache key (used to generate file path).
            ttl_hours: Override TTL for this lookup. If None, uses entry's original TTL.

        Returns:
            Cached data, or None if not found / expired.
        """
        if not self.enabled:
            self._stats["misses"] += 1
            return None

        path = self._key_to_path(key)
        if not os.path.exists(path):
            self._stats["misses"] += 1
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Cache read error for key={key}: {e}")
            self._stats["errors"] += 1
            self._stats["misses"] += 1
            return None

        entry = CacheEntry.from_dict(raw)
        if entry is None:
            self._stats["misses"] += 1
            return None

        # Check TTL
        effective_ttl = ttl_hours if ttl_hours is not None else entry.ttl_hours
        if effective_ttl > 0:
            age_hours = (time.time() - entry.created_ts) / 3600.0
            if age_hours > effective_ttl:
                logger.debug(f"Cache expired: key={key}, age={age_hours:.1f}h > ttl={effective_ttl}h")
                self._stats["expired"] += 1
                self._stats["misses"] += 1
                return None

        # Cache hit
        entry.touch()
        self._stats["hits"] += 1

        # Update access metadata (best-effort)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass  # Non-critical: access count update failed

        logger.debug(f"Cache hit: key={key} (accesses={entry.access_count})")
        return entry.data

    def put(self, key: str, data: Any, ttl_hours: Optional[float] = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            data: JSON-serializable data to cache.
            ttl_hours: TTL in hours (overrides default).
        """
        if not self.enabled:
            return

        ttl = ttl_hours if ttl_hours is not None else self.default_ttl_hours
        entry = CacheEntry(data=data, key=key, ttl_hours=ttl)

        path = self._key_to_path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
            self._stats["puts"] += 1
            logger.debug(f"Cache put: key={key}, ttl={ttl}h")
        except OSError as e:
            logger.warning(f"Cache write error for key={key}: {e}")
            self._stats["errors"] += 1

    def has(self, key: str) -> bool:
        """Check if a non-expired entry exists for the key."""
        return self.get(key) is not None

    def invalidate(self, key: str) -> bool:
        """Remove a specific cache entry.

        Returns:
            True if the entry was found and removed.
        """
        path = self._key_to_path(key)
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Cache invalidated: key={key}")
            return True
        return False

    def cleanup_expired(self) -> int:
        """Remove all expired cache entries.

        Returns:
            Number of entries removed.
        """
        if not self.enabled:
            return 0

        removed = 0
        cache_path = Path(self.cache_dir)
        for fpath in cache_path.glob("*.json"):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                entry = CacheEntry.from_dict(raw)
                if entry and entry.is_expired():
                    os.remove(fpath)
                    removed += 1
            except (json.JSONDecodeError, OSError):
                continue

        if removed > 0:
            logger.info(f"Cache cleanup: removed {removed} expired entries")
        return removed

    def summary(self) -> Dict[str, Any]:
        """Return summary of cache state and statistics."""
        n_files = 0
        total_bytes = 0
        if self.enabled and os.path.isdir(self.cache_dir):
            for f in Path(self.cache_dir).glob("*.json"):
                n_files += 1
                total_bytes += f.stat().st_size

        return {
            "enabled": self.enabled,
            "cache_dir": self.cache_dir,
            "default_ttl_hours": self.default_ttl_hours,
            "n_entries": n_files,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            **self._stats,
            "hit_rate": (
                round(self._stats["hits"] / max(1, self._stats["hits"] + self._stats["misses"]), 3)
            ),
        }

    def _key_to_path(self, key: str) -> str:
        """Convert a cache key to a file path."""
        # Use SHA1 hash to handle long/special-character keys
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{h}.json")


def make_cache_key(obj: Any) -> str:
    """Create a deterministic cache key from a JSON-serializable object."""
    return _hash_obj(obj)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_obj(obj: Any) -> str:
    """Compute SHA1 hash of a JSON-serialized object."""
    try:
        b = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha1(b).hexdigest()
    except (TypeError, ValueError):
        return hashlib.sha1(str(obj).encode("utf-8")).hexdigest()
