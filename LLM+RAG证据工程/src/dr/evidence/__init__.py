"""证据工程层

提供LLM+RAG相关的核心功能：
- BM25排名
- Ollama Embedding/LLM
- 证据提取
"""
from .ranker import BM25Ranker, tokenize, rerank_by_fields
from .ollama import OllamaClient, cosine_similarity
from .extractor import LLMEvidenceExtractor, EvidenceExtraction, EVIDENCE_SCHEMA

__all__ = [
    "BM25Ranker",
    "tokenize",
    "rerank_by_fields",
    "OllamaClient",
    "cosine_similarity",
    "LLMEvidenceExtractor",
    "EvidenceExtraction",
    "EVIDENCE_SCHEMA",
]
