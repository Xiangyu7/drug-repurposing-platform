"""BM25排名器

纯Python实现的BM25算法，用于文献检索和排序。

BM25 (Best Matching 25) 是一种经典的信息检索算法，广泛用于搜索引擎。
它比简单的TF-IDF更加鲁棒，考虑了文档长度归一化。

Reference:
    Robertson, S. E., & Zaragoza, H. (2009). "The Probabilistic Relevance Framework: BM25 and Beyond"
"""
import math
import re
from typing import List, Dict, Any, Tuple

from ..logger import get_logger

logger = get_logger(__name__)


def tokenize(text: str, min_len: int = 2) -> List[str]:
    """分词（简单的空格+正则分词）

    Args:
        text: 输入文本
        min_len: 最小token长度（过滤短词）

    Returns:
        token列表（小写）

    Example:
        >>> tokenize("Atherosclerosis and Plaque Regression")
        ['atherosclerosis', 'and', 'plaque', 'regression']
    """
    text = (text or "").lower()
    # 去除特殊字符，保留字母、数字、连字符
    text = re.sub(r"[^a-z0-9\s\-]+", " ", text)
    # 分词并过滤短词
    tokens = [t for t in text.split() if t and len(t) >= min_len]
    return tokens


class BM25Ranker:
    """BM25排名器

    Example:
        >>> docs = [
        ...     {"pmid": "12345", "title": "Atherosclerosis Treatment", "abstract": "..."},
        ...     {"pmid": "67890", "title": "Plaque Regression Study", "abstract": "..."}
        ... ]
        >>> ranker = BM25Ranker()
        >>> ranked = ranker.rank(
        ...     query="atherosclerosis plaque regression",
        ...     docs=docs,
        ...     topk=10
        ... )
        >>> for score, doc in ranked:
        ...     print(f"{score:.2f} - {doc['title']}")
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """初始化BM25排名器

        Args:
            k1: TF饱和参数（通常1.2-2.0，默认1.5）
                - 控制词频(TF)的影响力
                - 值越大，高频词的得分增长越快
            b: 长度归一化参数（0-1，默认0.75）
                - 控制文档长度对得分的影响
                - b=1: 完全归一化（长文档被严重惩罚）
                - b=0: 不归一化（长文档不被惩罚）
        """
        self.k1 = k1
        self.b = b

    def rank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        topk: int = 80,
        text_fields: List[str] = None
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """对文档进行BM25排名

        Args:
            query: 搜索查询
            docs: 文档列表（每个文档是字典）
            topk: 返回前k个文档
            text_fields: 用于排名的文本字段（默认["title", "abstract"]）

        Returns:
            [(score, doc), ...]列表，按score降序排列

        Example:
            >>> ranked = ranker.rank("aspirin atherosclerosis", docs, topk=20)
        """
        if text_fields is None:
            text_fields = ["title", "abstract"]

        # 分词query
        q_tokens = tokenize(query)
        if not q_tokens or not docs:
            logger.warning("Empty query or documents - returning empty ranking")
            return []

        # 分词所有文档
        doc_tokens = []
        for doc in docs:
            # 合并所有指定字段的文本
            text = " ".join([str(doc.get(field, "")) for field in text_fields])
            doc_tokens.append(tokenize(text))

        # 计算语料库统计
        N = len(doc_tokens)
        if N == 0:
            return []

        avgdl = sum(len(toks) for toks in doc_tokens) / N

        # 计算文档频率(DF)
        df = {}
        for toks in doc_tokens:
            for token in set(toks):  # 每个文档只计数一次
                df[token] = df.get(token, 0) + 1

        # IDF计算（使用BM25的IDF公式）
        def idf(token: str) -> float:
            """计算逆文档频率

            BM25的IDF公式：log((N - n + 0.5) / (n + 0.5) + 1)
            其中N是文档总数，n是包含该token的文档数
            """
            n = df.get(token, 0)
            # 避免log(0)和负数
            return math.log(1 + (N - n + 0.5) / (n + 0.5))

        # 对每个文档计算BM25得分
        ranked = []
        for doc, toks in zip(docs, doc_tokens):
            # 计算文档长度
            dl = len(toks)

            # 计算词频(TF)
            tf = {}
            for token in toks:
                tf[token] = tf.get(token, 0) + 1

            # 计算BM25得分
            score = 0.0
            for q_token in q_tokens:
                if q_token not in tf:
                    continue

                # 词频
                f = tf[q_token]

                # BM25公式：
                # IDF(q_token) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
                numerator = f * (self.k1 + 1)
                denominator = f + self.k1 * (1 - self.b + self.b * dl / (avgdl + 1e-9))
                score += idf(q_token) * numerator / denominator

            ranked.append((score, doc))

        # 按得分降序排列
        ranked.sort(key=lambda x: x[0], reverse=True)

        logger.debug(
            "Ranked %d documents, top score: %.2f, returning top %d",
            len(ranked),
            ranked[0][0] if ranked else 0.0,
            min(topk, len(ranked))
        )

        return ranked[:topk]

    def batch_rank(
        self,
        queries: List[str],
        docs: List[Dict[str, Any]],
        topk: int = 80
    ) -> Dict[str, List[Tuple[float, Dict[str, Any]]]]:
        """批量排名（多个查询共享同一文档集）

        Args:
            queries: 查询列表
            docs: 文档列表
            topk: 每个查询返回的top文档数

        Returns:
            {query: [(score, doc), ...]}映射

        Example:
            >>> queries = ["aspirin atherosclerosis", "statin plaque"]
            >>> results = ranker.batch_rank(queries, docs, topk=10)
        """
        results = {}
        for query in queries:
            results[query] = self.rank(query, docs, topk=topk)
        return results


def rerank_by_fields(
    ranked: List[Tuple[float, Dict[str, Any]]],
    boost_fields: Dict[str, float]
) -> List[Tuple[float, Dict[str, Any]]]:
    """根据字段值对排名结果进行二次调整

    用于实现诸如"提升人类试验"、"降低细胞实验"等策略。

    Args:
        ranked: BM25排名结果
        boost_fields: {field_name: boost_factor}映射
            - boost_factor > 1.0: 提升得分
            - boost_factor < 1.0: 降低得分

    Returns:
        重新排序后的结果

    Example:
        >>> # 提升人类试验，降低细胞实验
        >>> boost = {"model": 1.5}  # 如果doc["model"]=="human"
        >>> reranked = rerank_by_fields(ranked, boost)
    """
    reranked = []
    for score, doc in ranked:
        adjusted_score = score

        for field, boost in boost_fields.items():
            if field in doc:
                # 简化逻辑：如果字段存在且非空，应用boost
                if doc[field]:
                    adjusted_score *= boost

        reranked.append((adjusted_score, doc))

    reranked.sort(key=lambda x: x[0], reverse=True)
    return reranked


# ============================================================
# Reciprocal Rank Fusion (RRF)
# ============================================================

def reciprocal_rank_fusion(
    ranked_lists: List[List[Tuple[float, Dict[str, Any]]]],
    k: int = 60,
) -> List[Tuple[float, Dict[str, Any]]]:
    """Combine multiple ranked lists using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank_i)) for each list i where document appears.
    Higher k gives more weight to lower-ranked documents.

    Args:
        ranked_lists: List of ranked result lists [(score, doc), ...]
        k: RRF constant (default 60, typical range 10-100)

    Returns:
        Fused ranking sorted by RRF score descending.

    Reference:
        Cormack et al. (2009) "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods"
    """
    # Build doc_id -> rrf_score mapping
    rrf_scores: Dict[str, float] = {}
    doc_map: Dict[str, Dict[str, Any]] = {}

    for ranked in ranked_lists:
        for rank_pos, (score, doc) in enumerate(ranked):
            doc_id = str(doc.get("pmid", id(doc)))
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + 1.0 / (k + rank_pos + 1)
            doc_map[doc_id] = doc

    # Sort by RRF score
    fused = [(rrf_scores[did], doc_map[did]) for did in rrf_scores]
    fused.sort(key=lambda x: x[0], reverse=True)
    return fused


# ============================================================
# Hybrid Ranker (BM25 + Embedding via RRF)
# ============================================================

class HybridRanker:
    """Combines BM25 and embedding-based ranking via Reciprocal Rank Fusion.

    Stage 1: BM25 for lexical matching (fast, broad)
    Stage 2: Embedding for semantic matching (slower, precise)
    Fusion: RRF merges both rankings

    Example:
        >>> from dr.evidence.ollama import OllamaClient
        >>> ranker = HybridRanker(embed_client=OllamaClient())
        >>> results = ranker.rank("resveratrol atherosclerosis", docs, topk=30)
    """

    def __init__(
        self,
        embed_client=None,
        bm25_topk: int = 80,
        embed_topk: int = 60,
        rrf_k: int = 60,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        """Initialize hybrid ranker.

        Args:
            embed_client: OllamaClient for embedding (if None, falls back to BM25 only)
            bm25_topk: How many docs to keep from BM25 stage
            embed_topk: How many docs to send to embedding reranker
            rrf_k: RRF fusion constant
            k1: BM25 k1 parameter
            b: BM25 b parameter
        """
        self.bm25 = BM25Ranker(k1=k1, b=b)
        self.embed_client = embed_client
        self.bm25_topk = bm25_topk
        self.embed_topk = embed_topk
        self.rrf_k = rrf_k

    def rank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        topk: int = 30,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Rank documents using hybrid BM25 + embedding fusion.

        If embedding is unavailable, falls back to BM25-only.
        """
        # Stage 1: BM25
        bm25_ranked = self.bm25.rank(query, docs, topk=self.bm25_topk)

        if not bm25_ranked:
            return []

        # Stage 2: Embedding rerank (optional)
        if self.embed_client is None:
            logger.debug("No embed_client, returning BM25-only results")
            return bm25_ranked[:topk]

        # Get docs for embedding
        bm25_docs = [d for _, d in bm25_ranked[:self.embed_topk]]

        try:
            embed_ranked_docs = self.embed_client.rerank_by_embedding(
                query, bm25_docs, topk=self.embed_topk
            )
            # Convert to (rank_position, doc) tuples for RRF
            embed_ranked = [(1.0 / (i + 1), d) for i, d in enumerate(embed_ranked_docs)]
        except Exception as e:
            logger.warning("Embedding rerank failed, using BM25 only: %s", e)
            return bm25_ranked[:topk]

        # Fusion via RRF
        fused = reciprocal_rank_fusion(
            [bm25_ranked[:self.embed_topk], embed_ranked],
            k=self.rrf_k
        )

        logger.debug("Hybrid ranking: %d BM25 + %d embed -> %d fused",
                     len(bm25_ranked), len(embed_ranked), len(fused))

        return fused[:topk]


# ============================================================
# Cross-Encoder Reranker (LLM-based pointwise scoring)
# ============================================================

class CrossEncoderReranker:
    """Reranks documents using LLM as a cross-encoder (pointwise relevance scoring).

    Uses the local Ollama LLM to score query-document relevance on a 0-10 scale.
    This is a lightweight alternative to dedicated cross-encoder models.

    Example:
        >>> reranker = CrossEncoderReranker(ollama_client=client)
        >>> reranked = reranker.rerank("aspirin atherosclerosis", docs, topk=15)
    """

    def __init__(self, ollama_client=None, model: str = None):
        self.client = ollama_client
        self.model = model

    def rerank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        topk: int = 15,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Rerank documents by LLM relevance scoring."""
        if not self.client or not docs:
            return [(0.0, d) for d in docs[:topk]]

        scored = []
        for doc in docs:
            title = doc.get("title", "")
            abstract = (doc.get("abstract", "") or "")[:500]
            score = self._score_relevance(query, title, abstract)
            scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:topk]

    def _score_relevance(self, query: str, title: str, abstract: str) -> float:
        """Score relevance of a single document to query (0-10)."""
        prompt = (
            f"Rate the relevance of this paper to the query on a scale of 0-10.\n"
            f"Query: {query}\n"
            f"Title: {title}\n"
            f"Abstract: {abstract}\n\n"
            f"Reply with ONLY a number between 0 and 10."
        )
        try:
            response = self.client.generate(
                prompt=prompt,
                model=self.model,
                temperature=0.0,
            )
            if response:
                # Extract first number from response
                match = re.search(r"(\d+(?:\.\d+)?)", response.strip())
                if match:
                    score = float(match.group(1))
                    return min(10.0, max(0.0, score))
        except Exception as e:
            logger.debug("Cross-encoder scoring failed: %s", e)

        return 5.0  # default neutral score on failure


# ============================================================
# Ranking Pipeline (composable stages)
# ============================================================

class RankingPipeline:
    """Composable multi-stage ranking pipeline.

    Chains ranking stages: BM25 -> Hybrid -> CrossEncoder (each optional).

    Example:
        >>> pipeline = RankingPipeline(
        ...     hybrid_ranker=HybridRanker(embed_client=client),
        ...     cross_encoder=None,  # disabled
        ... )
        >>> results = pipeline.rank("query", docs, topk=25)
    """

    def __init__(
        self,
        hybrid_ranker: HybridRanker = None,
        cross_encoder: CrossEncoderReranker = None,
        bm25_topk: int = 80,
        hybrid_topk: int = 30,
        final_topk: int = 15,
    ):
        self.hybrid_ranker = hybrid_ranker
        self.cross_encoder = cross_encoder
        self.bm25_topk = bm25_topk
        self.hybrid_topk = hybrid_topk
        self.final_topk = final_topk

        # Fallback BM25 if no hybrid ranker provided
        if self.hybrid_ranker is None:
            self.hybrid_ranker = HybridRanker(embed_client=None, bm25_topk=bm25_topk)

    def rank(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        topk: int = None,
    ) -> List[Dict[str, Any]]:
        """Run full ranking pipeline and return top documents.

        Args:
            query: Search query
            docs: Document list
            topk: Override final topk (default: self.final_topk)

        Returns:
            List of ranked documents (score-free, just docs)
        """
        final_k = topk or self.final_topk

        # Stage 1+2: Hybrid (BM25 + Embedding via RRF)
        hybrid_results = self.hybrid_ranker.rank(query, docs, topk=self.hybrid_topk)

        if not hybrid_results:
            return []

        # Stage 3: Cross-encoder (optional)
        if self.cross_encoder is not None:
            hybrid_docs = [d for _, d in hybrid_results]
            ce_results = self.cross_encoder.rerank(query, hybrid_docs, topk=final_k)
            return [d for _, d in ce_results]

        return [d for _, d in hybrid_results[:final_k]]
