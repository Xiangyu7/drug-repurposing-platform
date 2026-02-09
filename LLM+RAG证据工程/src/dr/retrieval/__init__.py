"""数据检索层

提供统一的外部API访问接口：
- ClinicalTrials.gov API v2
- PubMed E-utilities
- 四层缓存系统
"""
from .ctgov import CTGovClient
from .pubmed import PubMedClient
from .cache import CacheManager

__all__ = ["CTGovClient", "PubMedClient", "CacheManager"]
