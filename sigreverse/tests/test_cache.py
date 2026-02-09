"""Unit tests for sigreverse.cache module.

Tests cover:
    - FileCache get/put operations
    - TTL expiration
    - Cache statistics tracking
    - Legacy format compatibility
    - Cleanup operations
    - Cache key generation
    - Edge cases (disabled cache, corrupt files)
"""
import json
import os
import time
import pytest

from sigreverse.cache import FileCache, CacheEntry, make_cache_key


# ===== CacheEntry =====

class TestCacheEntry:
    def test_create(self):
        entry = CacheEntry(data={"key": "val"}, key="test_key", ttl_hours=24)
        assert entry.data == {"key": "val"}
        assert entry.key == "test_key"
        assert entry.ttl_hours == 24
        assert entry.access_count == 0

    def test_to_dict(self):
        entry = CacheEntry(data=[1, 2, 3], key="k")
        d = entry.to_dict()
        assert "_cache_meta" in d
        assert "data" in d
        assert d["data"] == [1, 2, 3]
        assert d["_cache_meta"]["key"] == "k"

    def test_from_dict_roundtrip(self):
        entry = CacheEntry(data={"x": 42}, key="roundtrip", ttl_hours=48)
        d = entry.to_dict()
        restored = CacheEntry.from_dict(d)
        assert restored.data == {"x": 42}
        assert restored.key == "roundtrip"
        assert restored.ttl_hours == 48

    def test_from_dict_legacy(self):
        """Legacy format (no _cache_meta) should still work."""
        legacy = {"results": [1, 2, 3]}
        entry = CacheEntry.from_dict(legacy)
        assert entry.data == {"results": [1, 2, 3]}
        assert entry.key == "unknown"

    def test_is_expired(self):
        entry = CacheEntry(data={}, key="k", ttl_hours=0.001)  # ~3.6 seconds
        # Not expired yet
        assert not entry.is_expired()
        # Manually set old timestamp
        entry.created_ts = time.time() - 3600  # 1 hour ago
        assert entry.is_expired()

    def test_never_expire(self):
        entry = CacheEntry(data={}, key="k", ttl_hours=0)
        entry.created_ts = time.time() - 999999
        assert not entry.is_expired()

    def test_touch(self):
        entry = CacheEntry(data={}, key="k")
        assert entry.access_count == 0
        entry.touch()
        assert entry.access_count == 1
        assert entry.last_accessed is not None


# ===== FileCache =====

class TestFileCache:
    def test_put_and_get(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path), default_ttl_hours=24)
        cache.put("my_key", {"data": [1, 2, 3]})
        result = cache.get("my_key")
        assert result == {"data": [1, 2, 3]}

    def test_miss_returns_none(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        assert cache.get("nonexistent") is None

    def test_expired_returns_none(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path), default_ttl_hours=0.0001)
        cache.put("old_key", {"old": True})
        # Manually backdate the entry
        path = cache._key_to_path("old_key")
        with open(path, "r") as f:
            raw = json.load(f)
        raw["_cache_meta"]["created_ts"] = time.time() - 3600 * 24  # 24 hours ago
        raw["_cache_meta"]["ttl_hours"] = 0.001  # Very short TTL
        with open(path, "w") as f:
            json.dump(raw, f)
        assert cache.get("old_key") is None

    def test_stats_tracking(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        cache.put("k1", "v1")
        cache.get("k1")  # hit
        cache.get("k2")  # miss

        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["puts"] == 1

    def test_disabled_cache(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path), enabled=False)
        cache.put("k", "v")
        assert cache.get("k") is None
        assert cache.stats["misses"] == 1

    def test_invalidate(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        cache.put("k", "v")
        assert cache.get("k") == "v"
        assert cache.invalidate("k") is True
        assert cache.get("k") is None

    def test_invalidate_nonexistent(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        assert cache.invalidate("nonexistent") is False

    def test_has(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        cache.put("k", "v")
        assert cache.has("k") is True
        assert cache.has("nope") is False

    def test_summary(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        cache.put("k1", "v1")
        cache.put("k2", "v2")
        cache.get("k1")

        summary = cache.summary()
        assert summary["enabled"] is True
        assert summary["n_entries"] == 2
        assert summary["hits"] == 1
        assert summary["puts"] == 2
        assert 0 <= summary["hit_rate"] <= 1.0

    def test_cleanup_expired(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path), default_ttl_hours=0.001)
        cache.put("old", "data")
        # Backdate
        path = cache._key_to_path("old")
        with open(path, "r") as f:
            raw = json.load(f)
        raw["_cache_meta"]["created_ts"] = time.time() - 3600 * 24
        with open(path, "w") as f:
            json.dump(raw, f)

        removed = cache.cleanup_expired()
        assert removed == 1
        assert not os.path.exists(path)

    def test_ttl_override_on_get(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path), default_ttl_hours=24)
        cache.put("k", "v")
        # With long TTL, should be a hit
        assert cache.get("k", ttl_hours=1000) is not None
        # Backdate, then use short TTL override
        path = cache._key_to_path("k")
        with open(path, "r") as f:
            raw = json.load(f)
        raw["_cache_meta"]["created_ts"] = time.time() - 3600  # 1 hour ago
        with open(path, "w") as f:
            json.dump(raw, f)
        assert cache.get("k", ttl_hours=0.5) is None  # 30 min TTL, 1h old → expired

    def test_corrupt_cache_file(self, tmp_path):
        cache = FileCache(cache_dir=str(tmp_path))
        # Write corrupt JSON
        path = cache._key_to_path("corrupt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{invalid json")
        assert cache.get("corrupt") is None
        assert cache.stats["errors"] == 1


# ===== Cache key generation =====

class TestMakeCacheKey:
    def test_deterministic(self):
        obj = {"type": "entities", "symbols": ["BRCA1", "TP53"]}
        k1 = make_cache_key(obj)
        k2 = make_cache_key(obj)
        assert k1 == k2

    def test_different_for_different_input(self):
        k1 = make_cache_key({"a": 1})
        k2 = make_cache_key({"a": 2})
        assert k1 != k2

    def test_order_independent_for_dicts(self):
        k1 = make_cache_key({"a": 1, "b": 2})
        k2 = make_cache_key({"b": 2, "a": 1})
        assert k1 == k2  # sort_keys=True

    def test_handles_complex_objects(self):
        k = make_cache_key({"nested": {"list": [1, 2, 3]}, "key": "val"})
        assert isinstance(k, str) and len(k) == 40  # SHA1 hex
