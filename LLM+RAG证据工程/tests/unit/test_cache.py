"""Unit tests for CacheManager"""

import pytest
import tempfile
import json
from pathlib import Path
from src.dr.retrieval.cache import CacheManager, CACHE_SCHEMA_VERSION, _validate_path_component


class TestCacheManager:
    """Tests for cache management"""

    def test_cache_initialization(self):
        """Test cache manager initializes with default dir"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            assert cache is not None
            assert cache.ctgov_dir.exists()
            assert cache.pubmed_dir.exists()
            assert cache.pubmed_best_dir.exists()
            assert cache.dossier_dir.exists()

    def test_cache_custom_directory(self):
        """Test cache manager accepts custom directory"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            assert cache.base_dir == Path(td)

    def test_ctgov_save_and_load(self):
        """Test saving and loading CTGov cached data"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            test_data = {"nct_id": "NCT12345678", "status": "Completed"}

            cache.set_ctgov("NCT12345678", test_data)
            loaded = cache.get_ctgov("NCT12345678")

            assert loaded is not None
            assert loaded["nct_id"] == "NCT12345678"
            assert loaded["status"] == "Completed"
            assert loaded["_v"] == CACHE_SCHEMA_VERSION

    def test_ctgov_load_missing(self):
        """Test loading non-existent CTGov cache returns None"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            assert cache.get_ctgov("NCT99999999") is None

    def test_pubmed_save_and_load(self):
        """Test saving and loading PubMed cached data"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            test_data = {"pmid": "123", "title": "Test Article"}
            params = {"max_results": 10}

            cache.set_pubmed("D001", "aspirin", test_data, params=params)
            loaded = cache.get_pubmed("D001", "aspirin", params=params)

            assert loaded is not None
            assert loaded["pmid"] == "123"

    def test_pubmed_best_cache(self):
        """Test pubmed_cache_best separate from pubmed_cache"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data_raw = {"type": "raw"}
            data_best = {"type": "best"}

            cache.set_pubmed("D001", "query", data_raw, is_best=False)
            cache.set_pubmed("D001", "query", data_best, is_best=True)

            loaded_raw = cache.get_pubmed("D001", "query", is_best=False)
            loaded_best = cache.get_pubmed("D001", "query", is_best=True)

            assert loaded_raw["type"] == "raw"
            assert loaded_best["type"] == "best"

    def test_dossier_save_and_load(self):
        """Test saving and loading dossier data"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            test_data = {"drug_id": "D001", "canonical_name": "resveratrol", "score": 85}

            cache.set_dossier("D001", "resveratrol", test_data)
            loaded = cache.get_dossier("D001", "resveratrol")

            assert loaded is not None
            assert loaded["score"] == 85

    def test_dossier_load_missing(self):
        """Test loading non-existent dossier returns None"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            assert cache.get_dossier("D999", "nonexistent") is None

    def test_cache_overwrite(self):
        """Test cache can be overwritten"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            cache.set_ctgov("NCT001", {"value": "old"})
            cache.set_ctgov("NCT001", {"value": "new"})

            loaded = cache.get_ctgov("NCT001")
            assert loaded["value"] == "new"

    def test_cache_with_complex_data(self):
        """Test caching complex nested data structures"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            complex_data = {
                "papers": [
                    {"pmid": "123", "title": "Test", "authors": ["A", "B"]},
                    {"pmid": "456", "title": "Test2", "authors": ["C", "D"]}
                ],
                "metadata": {"count": 2, "query": "test query"}
            }

            cache.set_ctgov("NCT_COMPLEX", complex_data)
            loaded = cache.get_ctgov("NCT_COMPLEX")

            assert loaded is not None
            assert len(loaded["papers"]) == 2
            assert loaded["metadata"]["count"] == 2

    def test_cache_clearing(self):
        """Test cache can be cleared"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            cache.set_ctgov("NCT001", {"data": 1})
            cache.set_ctgov("NCT002", {"data": 2})

            deleted = cache.clear_cache("ctgov")
            assert deleted == 2
            assert cache.get_ctgov("NCT001") is None
            assert cache.get_ctgov("NCT002") is None

    def test_cache_stats(self):
        """Test cache stats returns correct counts"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            cache.set_ctgov("NCT001", {"data": 1})
            cache.set_ctgov("NCT002", {"data": 2})

            stats = cache.cache_stats()
            assert stats["ctgov"] == 2
            assert stats["pubmed"] == 0

    def test_cache_key_consistency(self):
        """Test same inputs produce same cache key"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            key1 = cache._pubmed_cache_key("D001", "test", {"param": "value"})
            key2 = cache._pubmed_cache_key("D001", "test", {"param": "value"})
            assert key1 == key2

    def test_cache_key_different_inputs(self):
        """Test different inputs produce different keys"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            key1 = cache._pubmed_cache_key("D001", "test1", {"param": "a"})
            key2 = cache._pubmed_cache_key("D001", "test2", {"param": "b"})
            assert key1 != key2

    def test_cache_key_collision_resistance(self):
        """Test cache keys are resistant to collisions"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)

            key1 = cache._pubmed_cache_key("D001", "query", {"param": "a"})
            key2 = cache._pubmed_cache_key("D001", "query", {"param": "b"})
            key3 = cache._pubmed_cache_key("D001", "query", {"param": "ab"})

            assert key1 != key2
            assert key1 != key3
            assert key2 != key3


class TestCacheVersioning:
    """Tests for cache schema versioning"""

    def test_stamp_adds_version(self):
        """Test _stamp adds _v field"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data = {"pmid": "123", "title": "test"}
            stamped = cache._stamp(data)
            assert stamped["_v"] == CACHE_SCHEMA_VERSION

    def test_stamp_does_not_mutate_input(self):
        """Test _stamp returns a copy, not mutating input"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data = {"pmid": "123"}
            cache._stamp(data)
            assert "_v" not in data

    def test_check_version_current(self):
        """Test current version is valid"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data = {"_v": CACHE_SCHEMA_VERSION, "pmid": "123"}
            assert cache._check_version(data, "test_key") is True

    def test_check_version_stale(self):
        """Test old version is stale"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data = {"_v": CACHE_SCHEMA_VERSION - 1, "pmid": "123"}
            assert cache._check_version(data, "test_key") is False

    def test_check_version_missing(self):
        """Old cache files without _v should be treated as stale"""
        with tempfile.TemporaryDirectory() as td:
            cache = CacheManager(base_dir=td)
            data = {"pmid": "123"}
            assert cache._check_version(data, "test_key") is False


class TestPathValidation:
    """Tests for path traversal prevention"""

    def test_valid_nct_id(self):
        """Normal NCT ID passes validation"""
        result = _validate_path_component("NCT12345678", "nct_id")
        assert result == "NCT12345678"

    def test_valid_drug_id(self):
        """Normal drug ID passes validation"""
        result = _validate_path_component("D81B744A593", "drug_id")
        assert result == "D81B744A593"

    def test_rejects_path_traversal(self):
        """Path traversal should raise ValueError"""
        with pytest.raises(ValueError):
            _validate_path_component("../../../etc/passwd", "drug_id")

    def test_rejects_dotdot_without_slash(self):
        """Double-dot should raise even without slashes"""
        with pytest.raises(ValueError, match="path traversal"):
            _validate_path_component("..secret", "drug_id")

    def test_rejects_slash(self):
        """Forward slash should raise ValueError"""
        with pytest.raises(ValueError, match="path separator"):
            _validate_path_component("foo/bar", "drug_id")

    def test_rejects_backslash(self):
        """Backslash should raise ValueError"""
        with pytest.raises(ValueError, match="path separator"):
            _validate_path_component("foo\\bar", "drug_id")

    def test_rejects_null_byte(self):
        """Null byte should raise ValueError"""
        with pytest.raises(ValueError, match="null byte"):
            _validate_path_component("foo\x00bar", "drug_id")

    def test_rejects_empty(self):
        """Empty string should raise ValueError"""
        with pytest.raises(ValueError, match="non-empty"):
            _validate_path_component("", "drug_id")

    def test_rejects_none(self):
        """None should raise ValueError"""
        with pytest.raises(ValueError, match="non-empty"):
            _validate_path_component(None, "drug_id")
