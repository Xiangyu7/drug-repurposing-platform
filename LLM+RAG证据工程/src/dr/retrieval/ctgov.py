"""ClinicalTrials.gov API v2客户端

提供对ClinicalTrials.gov API v2的健壮访问。

API文档：https://clinicaltrials.gov/data-api/api

特性：
- 自动重试（指数退避）
- 四层缓存
- 结构化元数据提取
- 安全的嵌套字典访问
"""
from typing import Dict, Any, Optional, List
from pathlib import Path

from ..common.http import request_with_retries
from ..config import Config
from ..logger import get_logger
from .cache import CacheManager

logger = get_logger(__name__)


def safe_get(d: Dict, *keys: str, default: Any = "") -> Any:
    """安全的嵌套字典访问

    Args:
        d: 字典对象
        *keys: 嵌套键路径
        default: 默认值（键不存在时返回）

    Returns:
        值或默认值

    Example:
        >>> safe_get({"a": {"b": {"c": 123}}}, "a", "b", "c")
        123
        >>> safe_get({"a": {}}, "a", "b", "c", default="N/A")
        'N/A'
    """
    val = d
    for k in keys:
        if isinstance(val, dict) and k in val:
            val = val[k]
        else:
            return default
    return val


class CTGovClient:
    """ClinicalTrials.gov API v2客户端

    Example:
        >>> client = CTGovClient()
        >>> study = client.fetch_study("NCT12345678")
        >>> metadata = client.extract_metadata(study)
        >>> print(metadata["title"])
    """

    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
        use_cache: bool = True
    ):
        """初始化CTGov客户端

        Args:
            cache_manager: 缓存管理器（如果为None，创建默认实例）
            use_cache: 是否启用缓存
        """
        self.config = Config.ctgov
        self.cache = cache_manager or CacheManager()
        self.use_cache = use_cache

    def fetch_study(self, nct_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """获取单个试验的完整数据

        Args:
            nct_id: NCT编号（如NCT12345678）
            force_refresh: 强制刷新缓存

        Returns:
            CT.gov API v2响应JSON

        Raises:
            RuntimeError: API请求失败
            ValueError: nct_id格式无效

        Example:
            >>> study = client.fetch_study("NCT04373928")
            >>> protocol = study["protocolSection"]
        """
        # 验证NCT ID格式
        nct_id = nct_id.strip().upper()
        if not nct_id.startswith("NCT") or len(nct_id) != 11:
            raise ValueError(f"Invalid NCT ID format: {nct_id}")

        # 检查缓存
        if self.use_cache and not force_refresh:
            cached = self.cache.get_ctgov(nct_id)
            if cached is not None:
                logger.debug("Using cached data for %s", nct_id)
                return cached

        # 调用API
        url = self.config.API_BASE_URL.format(nct_id)
        logger.info("Fetching %s from CT.gov API v2", nct_id)

        try:
            resp = request_with_retries(
                method="GET",
                url=url,
                timeout=self.config.TIMEOUT,
                max_retries=self.config.MAX_RETRIES,
                retry_sleep=self.config.RETRY_DELAY
            )
            data = resp.json()
        except Exception as e:
            logger.error("Failed to fetch %s: %s", nct_id, e)
            raise RuntimeError(f"CT.gov API failed for {nct_id}: {e}")

        # 写入缓存
        if self.use_cache:
            self.cache.set_ctgov(nct_id, data)

        return data

    def fetch_batch(
        self,
        nct_ids: List[str],
        skip_errors: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        """批量获取试验数据

        Args:
            nct_ids: NCT编号列表
            skip_errors: 是否跳过失败的请求（否则抛出异常）

        Returns:
            {nct_id: study_data}映射
        """
        results = {}
        failed = []

        for nct_id in nct_ids:
            try:
                results[nct_id] = self.fetch_study(nct_id)
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", nct_id, e)
                failed.append(nct_id)
                if not skip_errors:
                    raise

        if failed:
            logger.warning("Failed to fetch %d/%d studies: %s", len(failed), len(nct_ids), failed[:5])

        logger.info("Fetched %d/%d studies successfully", len(results), len(nct_ids))
        return results

    def extract_metadata(self, study: Dict[str, Any]) -> Dict[str, Any]:
        """从API响应提取结构化元数据

        Args:
            study: fetch_study()返回的JSON

        Returns:
            结构化元数据字典

        Example:
            >>> study = client.fetch_study("NCT04373928")
            >>> meta = client.extract_metadata(study)
            >>> print(meta["nctId"], meta["title"], meta["phase"])
        """
        ps = safe_get(study, "protocolSection", default={})
        id_module = safe_get(ps, "identificationModule", default={})
        status_module = safe_get(ps, "statusModule", default={})
        sponsor_module = safe_get(ps, "sponsorCollaboratorsModule", default={})
        design_module = safe_get(ps, "designModule", default={})
        arms_module = safe_get(ps, "armsInterventionsModule", default={})
        outcomes_module = safe_get(ps, "outcomesModule", default={})
        eligibility_module = safe_get(ps, "eligibilityModule", default={})
        conditions_module = safe_get(ps, "conditionsModule", default={})

        # 提取phases（多个可能）
        phases_raw = safe_get(design_module, "phases", default=[])
        phases = "; ".join(phases_raw) if isinstance(phases_raw, list) else str(phases_raw)

        # 提取conditions
        conditions_raw = safe_get(conditions_module, "conditions", default=[])
        conditions = " | ".join(conditions_raw) if isinstance(conditions_raw, list) else str(conditions_raw)

        # 提取interventions
        interventions_raw = safe_get(arms_module, "interventions", default=[])
        intervention_names = []
        intervention_types = []
        for interv in interventions_raw:
            if isinstance(interv, dict):
                intervention_names.append(safe_get(interv, "name", default=""))
                intervention_types.append(safe_get(interv, "type", default=""))

        # 提取primary outcomes
        primary_outcomes_raw = safe_get(outcomes_module, "primaryOutcomes", default=[])
        primary_outcome_titles = []
        for out in primary_outcomes_raw:
            if isinstance(out, dict):
                primary_outcome_titles.append(safe_get(out, "measure", default=""))

        metadata = {
            # 核心标识
            "nctId": safe_get(id_module, "nctId"),
            "title": safe_get(id_module, "officialTitle") or safe_get(id_module, "briefTitle"),
            "acronym": safe_get(id_module, "acronym"),

            # 状态
            "overallStatus": safe_get(status_module, "overallStatus"),
            "startDate": safe_get(status_module, "startDateStruct", "date"),
            "completionDate": safe_get(status_module, "completionDateStruct", "date"),

            # 设计
            "phase": phases,
            "studyType": safe_get(design_module, "studyType"),
            "enrollmentCount": safe_get(design_module, "enrollmentInfo", "count"),

            # 赞助商
            "leadSponsor": safe_get(sponsor_module, "leadSponsor", "name"),
            "collaborators": "; ".join([
                c.get("name", "") for c in safe_get(sponsor_module, "collaborators", default=[])
                if isinstance(c, dict)
            ]),

            # 条件
            "conditions": conditions,

            # 干预措施
            "intervention_names": " | ".join(intervention_names),
            "intervention_types": " | ".join(intervention_types),

            # 结局指标
            "primary_outcomes": " | ".join(primary_outcome_titles),

            # 纳入标准
            "eligibilityCriteria": safe_get(eligibility_module, "eligibilityCriteria"),
            "sex": safe_get(eligibility_module, "sex"),
            "minimumAge": safe_get(eligibility_module, "minimumAge"),
            "maximumAge": safe_get(eligibility_module, "maximumAge"),
        }

        return metadata

    def search_by_condition(
        self,
        condition: str,
        max_results: int = 100
    ) -> List[str]:
        """按疾病条件搜索试验（返回NCT ID列表）

        注意：此功能需要调用CT.gov搜索API，当前版本仅实现单试验获取。
        如需批量搜索，建议使用官方搜索页面导出CSV。

        Args:
            condition: 疾病条件（如"Atherosclerosis"）
            max_results: 最大返回数

        Returns:
            NCT ID列表

        Raises:
            NotImplementedError: 当前版本未实现搜索功能
        """
        raise NotImplementedError(
            "Batch search not implemented. "
            "Please use https://clinicaltrials.gov/search to export NCT IDs."
        )
