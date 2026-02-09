"""统一缓存管理器

四层缓存策略：
1. ctgov_cache/       - ClinicalTrials.gov API响应
2. pubmed_cache/      - PubMed文献元数据
3. pubmed_cache_best/ - PubMed精选摘要（RAG输入）
4. dossiers_json/     - 最终药物档案

缓存键生成规则：
- CT.gov: {nct_id}.json
- PubMed: {drug_id}_{safe_query}__{md5(params)[:32]}.json
- Dossier: {drug_id}__{safe_drug_name}.json
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional, Dict

from ..common.file_io import read_json, write_json
from ..common.text import safe_filename
from ..logger import get_logger

logger = get_logger(__name__)


CACHE_SCHEMA_VERSION = 2  # Increment when cache format changes

# Pattern for safe path components: alphanumeric, dash, underscore, dot
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _validate_path_component(value: str, label: str = "key") -> str:
    """Validate a string is safe to use as part of a filesystem path.

    Rejects path traversal attempts (../, /, null bytes) and empty strings.

    Args:
        value: The string to validate
        label: Human-readable label for error messages

    Returns:
        The validated string (stripped)

    Raises:
        ValueError: If the value contains unsafe characters
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"Cache {label} must be a non-empty string, got: {value!r}")
    value = value.strip()
    if not value:
        raise ValueError(f"Cache {label} must not be blank")
    if "\x00" in value:
        raise ValueError(f"Cache {label} contains null byte")
    if "/" in value or "\\" in value:
        raise ValueError(f"Cache {label} contains path separator: {value!r}")
    if ".." in value:
        raise ValueError(f"Cache {label} contains path traversal: {value!r}")
    return value


class CacheManager:
    """缓存管理器

    Cache versioning: each cached entry includes a "_v" field.
    On read, if "_v" doesn't match CACHE_SCHEMA_VERSION, the entry
    is treated as stale (cache miss) so it gets re-fetched.

    Example:
        >>> cache = CacheManager(base_dir="data")
        >>> cache.get_ctgov("NCT12345678")
        >>> cache.set_ctgov("NCT12345678", {...})
        >>> cache.get_pubmed("D123ABC", "aspirin atherosclerosis", {"max_results": 10})
    """

    def __init__(self, base_dir: str | Path = "data"):
        """初始化缓存管理器

        Args:
            base_dir: 缓存根目录（默认data/）
        """
        self.base_dir = Path(base_dir)
        self.schema_version = CACHE_SCHEMA_VERSION

        # 四层缓存目录
        self.ctgov_dir = self.base_dir / "ctgov_cache"
        self.pubmed_dir = self.base_dir / "pubmed_cache"
        self.pubmed_best_dir = self.base_dir / "pubmed_cache_best"
        self.dossier_dir = self.base_dir / "dossiers_json"

        # 创建所有缓存目录
        for d in [self.ctgov_dir, self.pubmed_dir, self.pubmed_best_dir, self.dossier_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _stamp(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Add schema version to cached data (returns a shallow copy)."""
        stamped = dict(data)
        stamped["_v"] = self.schema_version
        return stamped

    def _check_version(self, data: Dict[str, Any], key: str) -> bool:
        """Check if cached data matches current schema version.

        Returns True if valid, False if stale.
        """
        v = data.get("_v")
        if v != self.schema_version:
            logger.info("Cache stale for %s (v=%s, need v=%d)", key, v, self.schema_version)
            return False
        return True

    # ============================================================
    # CTGov缓存
    # ============================================================

    def get_ctgov(self, nct_id: str) -> Optional[Dict[str, Any]]:
        """从缓存读取CT.gov试验数据

        Args:
            nct_id: NCT编号（如NCT12345678）

        Returns:
            缓存的JSON对象，如果不存在返回None
        """
        nct_id = _validate_path_component(nct_id, "nct_id")
        cache_path = self.ctgov_dir / f"{nct_id}.json"
        if not cache_path.exists():
            return None

        try:
            data = read_json(cache_path)
            if not self._check_version(data, nct_id):
                return None
            logger.debug("CTGov cache hit: %s", nct_id)
            return data
        except Exception as e:
            logger.warning("Failed to read CTGov cache %s: %s", nct_id, e)
            return None

    def set_ctgov(self, nct_id: str, data: Dict[str, Any]) -> None:
        """写入CT.gov试验数据到缓存

        Args:
            nct_id: NCT编号
            data: API响应JSON
        """
        nct_id = _validate_path_component(nct_id, "nct_id")
        cache_path = self.ctgov_dir / f"{nct_id}.json"
        try:
            write_json(cache_path, self._stamp(data))
            logger.debug("CTGov cached: %s", nct_id)
        except Exception as e:
            logger.warning("Failed to write CTGov cache %s: %s", nct_id, e)

    # ============================================================
    # PubMed缓存
    # ============================================================

    def _pubmed_cache_key(self, drug_id: str, query: str, params: Optional[Dict] = None) -> str:
        """生成PubMed缓存键

        格式：{drug_id}_{safe_query}__{md5(params)[:32]}.json

        Args:
            drug_id: 药物ID（如D123ABC）
            query: 搜索查询
            params: API参数（dict）

        Returns:
            缓存文件名
        """
        safe_q = safe_filename(query, max_len=60)

        if params:
            # 确定性排序（保证相同参数生成相同hash）
            param_str = json.dumps(params, sort_keys=True, ensure_ascii=False)
            param_hash = hashlib.md5(param_str.encode("utf-8")).hexdigest()[:32]
        else:
            param_hash = "default"

        return f"{drug_id.lower()}_{safe_q}__{param_hash}.json"

    def get_pubmed(
        self,
        drug_id: str,
        query: str,
        params: Optional[Dict] = None,
        is_best: bool = False
    ) -> Optional[Dict[str, Any]]:
        """从缓存读取PubMed数据

        Args:
            drug_id: 药物ID
            query: 搜索查询
            params: API参数
            is_best: 是否使用pubmed_cache_best/（精选摘要）

        Returns:
            缓存的JSON对象，如果不存在返回None
        """
        cache_dir = self.pubmed_best_dir if is_best else self.pubmed_dir
        cache_key = self._pubmed_cache_key(drug_id, query, params)
        cache_path = cache_dir / cache_key

        if not cache_path.exists():
            return None

        try:
            data = read_json(cache_path)
            if not self._check_version(data, cache_key):
                return None
            logger.debug("PubMed cache hit (%s): %s", "best" if is_best else "raw", cache_key)
            return data
        except Exception as e:
            logger.warning("Failed to read PubMed cache %s: %s", cache_key, e)
            return None

    def set_pubmed(
        self,
        drug_id: str,
        query: str,
        data: Dict[str, Any],
        params: Optional[Dict] = None,
        is_best: bool = False
    ) -> None:
        """写入PubMed数据到缓存

        Args:
            drug_id: 药物ID
            query: 搜索查询
            data: API响应JSON
            params: API参数
            is_best: 是否写入pubmed_cache_best/
        """
        cache_dir = self.pubmed_best_dir if is_best else self.pubmed_dir
        cache_key = self._pubmed_cache_key(drug_id, query, params)
        cache_path = cache_dir / cache_key

        try:
            write_json(cache_path, self._stamp(data))
            logger.debug("PubMed cached (%s): %s", "best" if is_best else "raw", cache_key)
        except Exception as e:
            logger.warning("Failed to write PubMed cache %s: %s", cache_key, e)

    # ============================================================
    # Dossier缓存
    # ============================================================

    def get_dossier(self, drug_id: str, drug_name: str) -> Optional[Dict[str, Any]]:
        """从缓存读取药物档案

        Args:
            drug_id: 药物ID
            drug_name: 药物规范名称

        Returns:
            缓存的档案JSON，如果不存在返回None
        """
        drug_id = _validate_path_component(drug_id, "drug_id")
        safe_name = safe_filename(drug_name, max_len=60)
        cache_path = self.dossier_dir / f"{drug_id}__{safe_name}.json"

        if not cache_path.exists():
            return None

        try:
            data = read_json(cache_path)
            if not self._check_version(data, drug_id):
                return None
            logger.debug("Dossier cache hit: %s", drug_id)
            return data
        except Exception as e:
            logger.warning("Failed to read dossier cache %s: %s", drug_id, e)
            return None

    def set_dossier(self, drug_id: str, drug_name: str, data: Dict[str, Any]) -> None:
        """写入药物档案到缓存

        Args:
            drug_id: 药物ID
            drug_name: 药物规范名称
            data: 档案JSON
        """
        drug_id = _validate_path_component(drug_id, "drug_id")
        safe_name = safe_filename(drug_name, max_len=60)
        cache_path = self.dossier_dir / f"{drug_id}__{safe_name}.json"

        try:
            write_json(cache_path, self._stamp(data))
            logger.debug("Dossier cached: %s", drug_id)
        except Exception as e:
            logger.warning("Failed to write dossier cache %s: %s", drug_id, e)

    # ============================================================
    # 工具方法
    # ============================================================

    def clear_cache(self, cache_type: str = "all") -> int:
        """清空指定缓存

        Args:
            cache_type: "ctgov"|"pubmed"|"pubmed_best"|"dossier"|"all"

        Returns:
            删除的文件数
        """
        dirs_to_clear = []

        if cache_type in ("ctgov", "all"):
            dirs_to_clear.append(self.ctgov_dir)
        if cache_type in ("pubmed", "all"):
            dirs_to_clear.append(self.pubmed_dir)
        if cache_type in ("pubmed_best", "all"):
            dirs_to_clear.append(self.pubmed_best_dir)
        if cache_type in ("dossier", "all"):
            dirs_to_clear.append(self.dossier_dir)

        total_deleted = 0
        for cache_dir in dirs_to_clear:
            if not cache_dir.exists():
                continue

            for f in cache_dir.glob("*.json"):
                try:
                    f.unlink()
                    total_deleted += 1
                except Exception as e:
                    logger.warning("Failed to delete %s: %s", f, e)

        logger.info("Cleared %d cache files (%s)", total_deleted, cache_type)
        return total_deleted

    def cache_stats(self) -> Dict[str, int]:
        """统计各层缓存的文件数

        Returns:
            {"ctgov": N, "pubmed": N, "pubmed_best": N, "dossier": N}
        """
        return {
            "ctgov": len(list(self.ctgov_dir.glob("*.json"))) if self.ctgov_dir.exists() else 0,
            "pubmed": len(list(self.pubmed_dir.glob("*.json"))) if self.pubmed_dir.exists() else 0,
            "pubmed_best": len(list(self.pubmed_best_dir.glob("*.json"))) if self.pubmed_best_dir.exists() else 0,
            "dossier": len(list(self.dossier_dir.glob("*.json"))) if self.dossier_dir.exists() else 0,
        }
