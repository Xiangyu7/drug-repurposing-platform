"""配置管理模块

集中管理所有环境变量和常量，消除跨脚本的配置重复。
"""
import os
from typing import Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class PubMedConfig:
    """PubMed E-utilities配置"""

    EUTILS_BASE: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    API_KEY: str = os.getenv("NCBI_API_KEY", "").strip()
    DELAY: float = float(os.getenv("NCBI_DELAY", "0.6"))
    TIMEOUT: float = float(os.getenv("PUBMED_TIMEOUT", "30"))
    EFETCH_CHUNK: int = int(os.getenv("PUBMED_EFETCH_CHUNK", "20"))

    def __post_init__(self):
        if self.API_KEY:
            logger.info("NCBI API key configured (length: %d)", len(self.API_KEY))
        else:
            logger.warning("NCBI_API_KEY not set - rate limiting will apply (3 req/s)")


@dataclass
class OllamaConfig:
    """Ollama LLM/Embedding配置"""

    HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    LLM_MODEL: str = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")
    TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "600"))
    EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE", "16"))
    MAX_RERANK_DOCS: int = int(os.getenv("MAX_RERANK_DOCS", "60"))
    CHAT_FORMAT: str = os.getenv("OLLAMA_CHAT_FORMAT", "json")
    USE_SCHEMA: bool = os.getenv("USE_CHAT_SCHEMA", "1") == "1"

    def __post_init__(self):
        if self.TIMEOUT < 10:
            logger.warning("OLLAMA_TIMEOUT < 10s may cause failures")


@dataclass
class CTGovConfig:
    """ClinicalTrials.gov API配置"""

    API_BASE_URL: str = "https://clinicaltrials.gov/api/v2/studies/{}"  # {} will be replaced with NCT ID
    TIMEOUT: float = float(os.getenv("CTGOV_TIMEOUT", "40"))
    MAX_RETRIES: int = int(os.getenv("CTGOV_MAX_RETRIES", "3"))
    RETRY_DELAY: float = float(os.getenv("CTGOV_RETRY_DELAY", "1.0"))


@dataclass
class RetryConfig:
    """HTTP重试策略配置"""

    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "4"))
    RETRY_SLEEP: float = float(os.getenv("RETRY_SLEEP", "2"))
    REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "30"))

    def __post_init__(self):
        if self.MAX_RETRIES < 1:
            raise ValueError("MAX_RETRIES must be >= 1")


@dataclass
class FeatureFlags:
    """功能开关"""

    DISABLE_EMBED: bool = os.getenv("DISABLE_EMBED", "0") == "1"
    DISABLE_LLM: bool = os.getenv("DISABLE_LLM", "0") == "1"
    CROSS_DRUG_FILTER: bool = os.getenv("CROSS_DRUG_FILTER", "1") == "1"
    PMID_STRICT: bool = os.getenv("PMID_STRICT", "1") == "1"
    FORCE_REBUILD: bool = os.getenv("FORCE_REBUILD", "0") == "1"
    REFRESH_EMPTY_CACHE: bool = os.getenv("REFRESH_EMPTY_CACHE", "1") == "1"

    def __post_init__(self):
        if self.DISABLE_EMBED:
            logger.warning("Embedding disabled (DISABLE_EMBED=1)")
        if self.DISABLE_LLM:
            logger.warning("LLM disabled (DISABLE_LLM=1)")


@dataclass
class RankConfig:
    """Ranking pipeline配置"""

    ENABLE_EMBED_RERANK: bool = os.getenv("ENABLE_EMBED_RERANK", "1") == "1"
    ENABLE_CROSS_ENCODER: bool = os.getenv("ENABLE_CROSS_ENCODER", "0") == "1"
    BM25_TOPK: int = int(os.getenv("BM25_TOPK", "80"))
    EMBED_TOPK: int = int(os.getenv("EMBED_TOPK", "30"))
    CROSS_ENCODER_TOPK: int = int(os.getenv("CROSS_ENCODER_TOPK", "15"))
    RRF_K: int = int(os.getenv("RRF_K", "60"))

    def __post_init__(self):
        if self.ENABLE_EMBED_RERANK:
            logger.info("Embedding rerank enabled (default)")
        if self.ENABLE_CROSS_ENCODER:
            logger.info("Cross-encoder rerank enabled")


@dataclass
class ExtractorConfig:
    """LLM Evidence Extractor配置"""

    MAX_RETRIES: int = int(os.getenv("EXTRACTOR_MAX_RETRIES", "3"))
    RETRY_BASE_DELAY: float = float(os.getenv("EXTRACTOR_RETRY_DELAY", "1.0"))
    HALLUCINATION_CHECK: bool = os.getenv("HALLUCINATION_CHECK", "1") == "1"


@dataclass
class EvidenceGatingConfig:
    """Step6 evidence gating thresholds (confidence and topic match)"""

    TOPIC_MISMATCH_THRESHOLD: float = float(os.getenv("TOPIC_MISMATCH_THRESHOLD", "0.30"))
    HIGH_CONFIDENCE_MIN_PMIDS: int = int(os.getenv("HIGH_CONFIDENCE_MIN_PMIDS", "6"))
    MED_CONFIDENCE_MIN_PMIDS: int = int(os.getenv("MED_CONFIDENCE_MIN_PMIDS", "3"))
    SUPPORT_COUNT_MODE: str = os.getenv("SUPPORT_COUNT_MODE", "unique_pmids")


@dataclass
class ScoringConfig:
    """Step7评分策略配置"""

    TOPIC_MIN: float = float(os.getenv("TOPIC_MIN", "0.30"))
    MIN_UNIQUE_PMIDS: int = int(os.getenv("MIN_UNIQUE_PMIDS", "2"))
    SAFETY_HARD_NOGO: bool = os.getenv("SAFETY_HARD_NOGO", "0") == "1"


class Config:
    """全局配置单例"""

    pubmed: PubMedConfig = PubMedConfig()
    ollama: OllamaConfig = OllamaConfig()
    ctgov: CTGovConfig = CTGovConfig()
    retry: RetryConfig = RetryConfig()
    features: FeatureFlags = FeatureFlags()
    ranking: RankConfig = RankConfig()
    extractor: ExtractorConfig = ExtractorConfig()
    gating: EvidenceGatingConfig = EvidenceGatingConfig()
    scoring: ScoringConfig = ScoringConfig()

    @classmethod
    def validate(cls) -> None:
        """验证所有配置（在启动时调用）"""
        # dataclass的__post_init__已经做了验证
        logger.info("Configuration validated successfully")

    @classmethod
    def summary(cls) -> dict:
        """返回配置摘要（用于日志）"""
        return {
            "pubmed": {
                "has_api_key": bool(cls.pubmed.API_KEY),
                "delay": cls.pubmed.DELAY,
            },
            "ollama": {
                "host": cls.ollama.HOST,
                "llm_model": cls.ollama.LLM_MODEL,
                "embed_model": cls.ollama.EMBED_MODEL,
            },
            "features": {
                "disable_embed": cls.features.DISABLE_EMBED,
                "disable_llm": cls.features.DISABLE_LLM,
                "cross_drug_filter": cls.features.CROSS_DRUG_FILTER,
            },
        }


# 启动时验证配置
Config.validate()
