"""Ollama客户端

提供对本地Ollama服务的访问：
- Embedding（nomic-embed-text等）
- LLM生成（qwen2.5:7b-instruct等）

Ollama是一个本地LLM服务框架，支持多种开源模型。
API文档：https://github.com/ollama/ollama/blob/main/docs/api.md
"""
import math
from typing import List, Optional, Dict, Any

from ..common.http import request_with_retries
from ..config import Config
from ..logger import get_logger

logger = get_logger(__name__)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算余弦相似度

    Args:
        a: 向量A
        b: 向量B

    Returns:
        余弦相似度（-1到1，越接近1越相似）

    Example:
        >>> vec1 = [1.0, 2.0, 3.0]
        >>> vec2 = [2.0, 4.0, 6.0]  # 方向相同，长度不同
        >>> cosine_similarity(vec1, vec2)
        1.0
    """
    if not a or not b or len(a) != len(b):
        logger.warning("Invalid vectors for cosine similarity")
        return 0.0

    # 点积
    dot = sum(x * y for x, y in zip(a, b))

    # 模长
    norm_a = math.sqrt(sum(x * x for x in a)) + 1e-12  # 避免除零
    norm_b = math.sqrt(sum(x * x for x in b)) + 1e-12

    return dot / (norm_a * norm_b)


class OllamaClient:
    """Ollama客户端

    支持Embedding和LLM生成两种模式。

    Example:
        >>> client = OllamaClient()
        >>>
        >>> # Embedding
        >>> embs = client.embed(["hello world", "test document"])
        >>> print(len(embs), len(embs[0]))  # (2, 768)
        >>>
        >>> # LLM生成
        >>> response = client.chat(
        ...     messages=[{"role": "user", "content": "What is atherosclerosis?"}],
        ...     format="json"
        ... )
        >>> print(response["message"]["content"])
    """

    def __init__(
        self,
        host: Optional[str] = None,
        embed_model: Optional[str] = None,
        llm_model: Optional[str] = None,
        timeout: Optional[float] = None
    ):
        """初始化Ollama客户端

        Args:
            host: Ollama服务地址（默认从Config读取）
            embed_model: Embedding模型名称（默认从Config读取）
            llm_model: LLM模型名称（默认从Config读取）
            timeout: 请求超时（秒，默认从Config读取）
        """
        self.config = Config.ollama
        self.host = (host or self.config.HOST).rstrip("/")
        self.embed_model = embed_model or self.config.EMBED_MODEL
        self.llm_model = llm_model or self.config.LLM_MODEL
        self.timeout = timeout or self.config.TIMEOUT

        # 检查配置
        if self.timeout < 10:
            logger.warning("Ollama timeout < 10s may cause failures for large models")

    # ============================================================
    # Embedding
    # ============================================================

    def embed(self, texts: List[str], model: Optional[str] = None) -> Optional[List[List[float]]]:
        """生成文本的embedding向量

        Args:
            texts: 文本列表
            model: 模型名称（可选，默认使用embed_model）

        Returns:
            embedding向量列表，如果失败返回None

        Example:
            >>> embs = client.embed(["hello", "world"])
            >>> print(len(embs[0]))  # 768 (nomic-embed-text维度)
        """
        if Config.features.DISABLE_EMBED:
            logger.debug("Embedding disabled by config")
            return None

        if not texts:
            return []

        model = model or self.embed_model
        url = f"{self.host}/api/embed"

        try:
            # 尝试新API（批量embedding）
            resp = request_with_retries(
                method="POST",
                url=url,
                json={"model": model, "input": texts},
                timeout=self.timeout,
                trust_env=False  # Ollama需要禁用代理
            )
            data = resp.json()
            embeddings = data.get("embeddings")

            if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
                logger.debug("Embedded %d texts using %s", len(texts), model)
                return embeddings

        except Exception as e:
            logger.warning("Batch embedding failed, trying fallback: %s", e)

        # 回退到旧API（单个embedding）
        try:
            url_old = f"{self.host}/api/embeddings"
            embeddings = []

            for text in texts:
                resp = request_with_retries(
                    method="POST",
                    url=url_old,
                    json={"model": model, "prompt": text},
                    timeout=self.timeout,
                    trust_env=False
                )
                data = resp.json()
                emb = data.get("embedding")

                if not isinstance(emb, list):
                    logger.error("Invalid embedding response for text: %s", text[:50])
                    return None

                embeddings.append(emb)

            logger.debug("Embedded %d texts using fallback API", len(texts))
            return embeddings

        except Exception as e:
            logger.error("Embedding completely failed: %s", e)
            return None

    def embed_batched(
        self,
        texts: List[str],
        model: Optional[str] = None,
        batch_size: Optional[int] = None
    ) -> Optional[List[List[float]]]:
        """分批生成embedding（避免超时）

        Args:
            texts: 文本列表
            model: 模型名称
            batch_size: 批次大小（默认从Config读取）

        Returns:
            embedding向量列表

        Example:
            >>> # 100个文本，每次处理16个
            >>> embs = client.embed_batched(texts, batch_size=16)
        """
        if not texts:
            return []

        batch_size = batch_size or self.config.EMBED_BATCH_SIZE
        embeddings: List[List[float]] = []

        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            chunk_embs = self.embed(chunk, model=model)

            if chunk_embs is None:
                logger.error("Batch embedding failed at chunk %d-%d", i, i + len(chunk))
                return None

            embeddings.extend(chunk_embs)

        logger.debug("Batched embedding completed: %d texts in %d batches", len(texts), len(texts) // batch_size + 1)
        return embeddings

    # ============================================================
    # LLM生成
    # ============================================================

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        format: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        stream: bool = False
    ) -> Optional[Dict[str, Any]]:
        """LLM对话生成

        Args:
            messages: 消息列表，格式[{"role": "user", "content": "..."}]
            model: 模型名称（可选）
            format: 输出格式（"json"或None）
            schema: JSON schema（如果format="json"且USE_CHAT_SCHEMA=1）
            temperature: 采样温度（0-1，0最确定）
            stream: 是否流式输出（当前不支持）

        Returns:
            响应字典，格式{"message": {"role": "assistant", "content": "..."}}

        Example:
            >>> response = client.chat(
            ...     messages=[{"role": "user", "content": "What is BM25?"}],
            ...     format="json"
            ... )
            >>> print(response["message"]["content"])
        """
        if Config.features.DISABLE_LLM:
            logger.debug("LLM disabled by config")
            return None

        if stream:
            raise NotImplementedError("Streaming not yet supported")

        model = model or self.llm_model
        url = f"{self.host}/api/chat"

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
            }
        }

        # 添加format约束
        if format == "json":
            if self.config.USE_SCHEMA and schema:
                # 使用JSON schema硬约束
                payload["format"] = {"type": "json", "schema": schema}
                logger.debug("Using JSON schema constraint")
            else:
                # 使用简单的json format
                payload["format"] = "json"
                logger.debug("Using simple JSON format")

        try:
            resp = request_with_retries(
                method="POST",
                url=url,
                json=payload,
                timeout=self.timeout,
                trust_env=False
            )
            data = resp.json()

            # 检查响应格式
            if "message" not in data:
                logger.error("Invalid LLM response: missing 'message' field")
                return None

            logger.debug("LLM generation completed using %s", model)
            return data

        except Exception as e:
            logger.error("LLM chat failed: %s", e)
            return None

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        format: Optional[str] = None,
        temperature: float = 0.0
    ) -> Optional[str]:
        """简单的文本生成（单轮对话）

        Args:
            prompt: 提示词
            model: 模型名称
            format: 输出格式（"json"或None）
            temperature: 采样温度

        Returns:
            生成的文本，如果失败返回None

        Example:
            >>> text = client.generate("Explain atherosclerosis in one sentence")
            >>> print(text)
        """
        response = self.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            format=format,
            temperature=temperature
        )

        if response and "message" in response:
            return response["message"].get("content")

        return None

    # ============================================================
    # 工具方法
    # ============================================================

    def rerank_by_embedding(
        self,
        query: str,
        docs: List[Dict[str, Any]],
        topk: int = 25,
        text_fields: List[str] = None,
        model: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """使用embedding对文档进行重排序

        适用于BM25预排序后的精细化排序。

        Args:
            query: 查询文本
            docs: 文档列表（已预排序）
            topk: 返回前k个文档
            text_fields: 用于embedding的文本字段（默认["title", "abstract"]）
            model: embedding模型

        Returns:
            重排序后的文档列表

        Example:
            >>> # BM25预排序
            >>> ranked = bm25.rank(query, all_docs, topk=100)
            >>> docs = [d for _, d in ranked]
            >>>
            >>> # Embedding重排序
            >>> reranked = client.rerank_by_embedding(query, docs[:60], topk=20)
        """
        if Config.features.DISABLE_EMBED:
            logger.debug("Embedding reranking disabled, returning original order")
            return docs[:topk]

        if text_fields is None:
            text_fields = ["title", "abstract"]

        if not docs:
            return []

        # 限制文档数（避免超时）
        max_docs = min(len(docs), self.config.MAX_RERANK_DOCS)
        docs = docs[:max_docs]

        # 生成query embedding
        query_emb = self.embed([query], model=model)
        if not query_emb:
            logger.warning("Query embedding failed, returning original order")
            return docs[:topk]

        query_vec = query_emb[0]

        # 生成文档embedding
        doc_texts = []
        for doc in docs:
            text = " ".join([str(doc.get(field, "")) for field in text_fields])
            # 截断过长文本（避免超时）
            doc_texts.append(text[:3000])

        doc_embs = self.embed_batched(doc_texts, model=model)
        if not doc_embs:
            logger.warning("Document embedding failed, returning original order")
            return docs[:topk]

        # 计算相似度并重排序
        scored = []
        for doc, doc_vec in zip(docs, doc_embs):
            similarity = cosine_similarity(query_vec, doc_vec)
            scored.append((similarity, doc))

        scored.sort(key=lambda x: x[0], reverse=True)

        logger.debug("Embedding reranking: %d docs -> top %d", len(docs), min(topk, len(scored)))

        return [doc for _, doc in scored[:topk]]
