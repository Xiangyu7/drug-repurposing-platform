"""PubMed E-utilities客户端

提供对PubMed E-utilities API的健壮访问。

API文档：https://www.ncbi.nlm.nih.gov/books/NBK25501/

特性：
- ESearch + EFetch两步检索
- 自动限速（API Key: 10 req/s, 无Key: 3 req/s）
- 四层缓存
- 结构化文献元数据提取
"""
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional, List
from pathlib import Path

from ..common.http import request_with_retries
from ..common.text import normalize_pmid
from ..config import Config
from ..logger import get_logger
from .cache import CacheManager
try:
    from ..monitoring import track_pubmed_request
except Exception:  # pragma: no cover - monitoring is optional at runtime
    from contextlib import contextmanager

    @contextmanager
    def track_pubmed_request(operation: str):
        yield

logger = get_logger(__name__)


class PubMedClient:
    """PubMed E-utilities客户端

    Example:
        >>> client = PubMedClient()
        >>> pmids = client.search("aspirin atherosclerosis", max_results=10)
        >>> articles = client.fetch_details(pmids)
        >>> for pmid, meta in articles.items():
        ...     print(meta["title"], meta["abstract"])
    """

    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
        use_cache: bool = True
    ):
        """初始化PubMed客户端

        Args:
            cache_manager: 缓存管理器（如果为None，创建默认实例）
            use_cache: 是否启用缓存
        """
        self.config = Config.pubmed
        self.cache = cache_manager or CacheManager()
        self.use_cache = use_cache

        # 限速参数（根据是否有API Key调整）
        self.has_api_key = bool(self.config.API_KEY)
        self.delay = self.config.DELAY if self.has_api_key else 0.34  # 3 req/s without key

        if not self.has_api_key:
            logger.warning(
                "NCBI_API_KEY not set - using slower rate limit (3 req/s). "
                "Get free key at https://www.ncbi.nlm.nih.gov/account/"
            )

    def _rate_limit(self):
        """限速延迟"""
        time.sleep(self.delay)

    def search(
        self,
        query: str,
        max_results: int = 100,
        sort: str = "relevance",
        reldate: Optional[int] = None
    ) -> List[str]:
        """搜索PubMed，返回PMID列表

        Args:
            query: 搜索查询（支持布尔运算符AND/OR/NOT）
            max_results: 最大返回数
            sort: 排序方式（"relevance"|"pub_date"）
            reldate: 最近N天内的文献（可选）

        Returns:
            PMID列表（字符串）

        Example:
            >>> pmids = client.search("aspirin AND atherosclerosis", max_results=20)
        """
        url = f"{self.config.EUTILS_BASE}/esearch.fcgi"

        params = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": sort,
        }

        if reldate:
            params["reldate"] = reldate

        if self.has_api_key:
            params["api_key"] = self.config.API_KEY

        logger.info("Searching PubMed: %s (max=%d)", query[:100], max_results)

        try:
            with track_pubmed_request("search"):
                resp = request_with_retries(
                    method="GET",
                    url=url,
                    params=params,
                    timeout=30,
                    max_retries=3,
                    retry_sleep=1.0
                )
                data = resp.json()
        except Exception as e:
            logger.error("PubMed search failed: %s", e)
            raise RuntimeError(f"PubMed search failed: {e}")

        self._rate_limit()

        # 提取PMID列表
        pmids = data.get("esearchresult", {}).get("idlist", [])
        logger.info("Found %d PMIDs", len(pmids))

        return pmids

    def fetch_details(
        self,
        pmids: List[str],
        force_refresh: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """批量获取文献详情（标题、摘要、作者等）

        Uses EFetch batch API (up to 200 PMIDs per request) for efficiency.
        Cached articles are served from cache; only uncached PMIDs hit the API.

        Args:
            pmids: PMID列表
            force_refresh: 强制刷新缓存

        Returns:
            {pmid: metadata}映射

        Example:
            >>> articles = client.fetch_details(["12345678", "87654321"])
            >>> print(articles["12345678"]["title"])
        """
        if not pmids:
            return {}

        # 规范化PMID, 去重, 过滤空值
        seen = set()
        unique_pmids = []
        for p in pmids:
            norm = normalize_pmid(p)
            if norm and norm not in seen:
                seen.add(norm)
                unique_pmids.append(norm)

        results = {}

        # Phase 1: serve from cache
        uncached_pmids = []
        for pmid in unique_pmids:
            if not force_refresh and self.use_cache:
                cache_key_params = {"pmid": pmid}
                cached = self.cache.get_pubmed("_PMID", pmid, params=cache_key_params)
                if cached is not None:
                    results[pmid] = cached
                    continue
            uncached_pmids.append(pmid)

        if uncached_pmids:
            logger.info(
                "Fetching %d PMIDs in batches (cached=%d, uncached=%d)",
                len(unique_pmids), len(results), len(uncached_pmids)
            )

        # Phase 2: batch fetch uncached PMIDs
        chunk_size = max(1, self.config.EFETCH_CHUNK)
        for i in range(0, len(uncached_pmids), chunk_size):
            batch = uncached_pmids[i:i + chunk_size]
            try:
                batch_results = self._fetch_batch(batch)
                for pmid, meta in batch_results.items():
                    results[pmid] = meta
                    # write to cache
                    if self.use_cache:
                        cache_key_params = {"pmid": pmid}
                        self.cache.set_pubmed("_PMID", pmid, meta, params=cache_key_params)
            except Exception as e:
                logger.error(
                    "Batch fetch failed for %d PMIDs (batch %d-%d): %s",
                    len(batch), i, i + len(batch), e
                )
                # Fallback: try individual fetches for this batch
                for pmid in batch:
                    try:
                        meta = self._fetch_single(pmid, force_refresh)
                        results[pmid] = meta
                    except Exception as e2:
                        logger.warning("Individual fetch also failed for PMID %s: %s", pmid, e2)

        logger.info("Fetched %d/%d articles", len(results), len(unique_pmids))
        return results

    def _fetch_batch(self, pmids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch multiple PMIDs in a single EFetch request.

        Args:
            pmids: List of PMIDs (max ~200 per NCBI guidelines)

        Returns:
            {pmid: metadata} for all successfully parsed articles
        """
        if not pmids:
            return {}

        url = f"{self.config.EUTILS_BASE}/efetch.fcgi"
        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        }
        if self.has_api_key:
            params["api_key"] = self.config.API_KEY

        logger.debug("Batch fetching %d PMIDs: %s...", len(pmids), ",".join(pmids[:3]))

        with track_pubmed_request("fetch_batch"):
            resp = request_with_retries(
                method="GET",
                url=url,
                params=params,
                timeout=60,
                max_retries=3,
                retry_sleep=2.0
            )
            xml_text = resp.text
        self._rate_limit()

        # Parse all <PubmedArticle> blocks from the batch response
        return self._parse_batch_xml(xml_text, pmids)

    def _fetch_single(self, pmid: str, force_refresh: bool = False) -> Dict[str, Any]:
        """获取单篇文献详情

        Args:
            pmid: 单个PMID
            force_refresh: 强制刷新缓存

        Returns:
            元数据字典
        """
        # 检查缓存（使用drug_id=""占位，因为这是通用PubMed缓存）
        cache_key_params = {"pmid": pmid}
        if self.use_cache and not force_refresh:
            cached = self.cache.get_pubmed("_PMID", pmid, params=cache_key_params)
            if cached is not None:
                logger.debug("Using cached data for PMID %s", pmid)
                return cached

        # 调用EFetch API
        url = f"{self.config.EUTILS_BASE}/efetch.fcgi"
        params = {
            "db": "pubmed",
            "id": pmid,
            "retmode": "xml",
        }

        if self.has_api_key:
            params["api_key"] = self.config.API_KEY

        logger.debug("Fetching PMID %s from PubMed", pmid)

        try:
            with track_pubmed_request("fetch_single"):
                resp = request_with_retries(
                    method="GET",
                    url=url,
                    params=params,
                    timeout=30,
                    max_retries=3,
                    retry_sleep=1.0
                )
                xml_text = resp.text
        except Exception as e:
            logger.error("PubMed fetch failed for PMID %s: %s", pmid, e)
            raise RuntimeError(f"PubMed fetch failed for {pmid}: {e}")

        self._rate_limit()

        # 解析XML
        metadata = self._parse_pubmed_xml(xml_text, pmid)

        # 写入缓存
        if self.use_cache:
            self.cache.set_pubmed("_PMID", pmid, metadata, params=cache_key_params)

        return metadata

    def _parse_pubmed_xml(self, xml_text: str, pmid: str) -> Dict[str, Any]:
        """解析PubMed XML响应（delegates to _extract_article_metadata）

        Args:
            xml_text: EFetch返回的XML
            pmid: PMID（用于日志）

        Returns:
            结构化元数据
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("Failed to parse XML for PMID %s: %s", pmid, e)
            return {"pmid": pmid, "error": f"XML parse error: {e}"}

        article = root.find(".//PubmedArticle")
        if article is None:
            logger.warning("No PubmedArticle found for PMID %s", pmid)
            return {"pmid": pmid, "error": "No article found"}

        meta = self._extract_article_metadata(article)
        if meta is None:
            return {"pmid": pmid, "error": "No MedlineCitation"}

        return meta

    def _extract_article_metadata(self, article_elem) -> Optional[Dict[str, Any]]:
        """Extract metadata from a single PubmedArticle XML element.

        Shared by both single-fetch and batch-fetch code paths.

        Args:
            article_elem: An ET Element for <PubmedArticle>

        Returns:
            Metadata dict, or None if essential fields are missing
        """
        medline = article_elem.find(".//MedlineCitation")
        if medline is None:
            return None

        pmid_elem = medline.find(".//PMID")
        pmid = pmid_elem.text.strip() if pmid_elem is not None and pmid_elem.text else ""
        if not pmid:
            return None

        title_elem = medline.find(".//ArticleTitle")
        title = title_elem.text if title_elem is not None and title_elem.text else ""

        # Handle multi-part abstracts (structured abstracts have multiple <AbstractText>)
        abstract_parts = []
        for at in medline.findall(".//AbstractText"):
            if at.text:
                abstract_parts.append(at.text.strip())
        abstract = " ".join(abstract_parts)

        # Authors
        authors = []
        author_list = medline.find(".//AuthorList")
        if author_list is not None:
            for author in author_list.findall(".//Author"):
                last = author.find("LastName")
                first = author.find("ForeName")
                if last is not None:
                    name = last.text
                    if first is not None:
                        name = f"{first.text} {name}"
                    authors.append(name)

        journal_elem = medline.find(".//Journal/Title")
        journal = journal_elem.text if journal_elem is not None and journal_elem.text else ""

        pub_date = medline.find(".//PubDate/Year")
        if pub_date is None:
            pub_date = medline.find(".//PubDate/MedlineDate")
        year = ""
        if pub_date is not None and pub_date.text:
            m = re.search(r"(\d{4})", pub_date.text)
            year = m.group(1) if m else pub_date.text[:4]

        return {
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "journal": journal,
            "year": year,
        }

    def _parse_batch_xml(self, xml_text: str, requested_pmids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Parse batch EFetch XML response containing multiple articles.

        EFetch returns a <PubmedArticleSet> with multiple <PubmedArticle> children.
        We extract each one and key by PMID.

        Args:
            xml_text: Full XML response from EFetch
            requested_pmids: PMIDs we asked for (for logging missing ones)

        Returns:
            {pmid: metadata} dict
        """
        results = {}

        if not xml_text or not xml_text.strip():
            logger.warning("Empty XML response for batch of %d PMIDs", len(requested_pmids))
            return results

        # Try parsing as proper XML first
        try:
            root = ET.fromstring(xml_text)
            articles = root.findall(".//PubmedArticle")
            for article in articles:
                meta = self._extract_article_metadata(article)
                if meta and meta.get("pmid"):
                    results[meta["pmid"]] = meta
        except ET.ParseError:
            # Fallback: regex extraction for concatenated/malformed XML
            logger.warning("Batch XML parse failed, falling back to regex extraction")
            blocks = re.findall(r"<PubmedArticle>.*?</PubmedArticle>", xml_text, flags=re.S)
            for block in blocks:
                try:
                    article = ET.fromstring(block)
                    meta = self._extract_article_metadata(article)
                    if meta and meta.get("pmid"):
                        results[meta["pmid"]] = meta
                except ET.ParseError as e:
                    logger.warning("Failed to parse PubmedArticle block: %s", e)

        # Log any missing PMIDs
        missing = set(requested_pmids) - set(results.keys())
        if missing:
            logger.warning("Batch fetch missing %d PMIDs: %s", len(missing), list(missing)[:5])

        return results

    def search_and_fetch(
        self,
        drug_id: str,
        query: str,
        max_results: int = 50,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """搜索并获取文献详情（一步到位）

        使用drug_id作为缓存键的一部分，适合药物特异性搜索。

        Args:
            drug_id: 药物ID（用于缓存）
            query: 搜索查询
            max_results: 最大返回数
            force_refresh: 强制刷新缓存

        Returns:
            {"pmids": [...], "articles": {pmid: metadata}}
        """
        # 检查缓存
        cache_params = {"max_results": max_results}
        if self.use_cache and not force_refresh:
            cached = self.cache.get_pubmed(drug_id, query, params=cache_params)
            if cached is not None:
                logger.info("Using cached PubMed results for %s", drug_id)
                return cached

        # 搜索
        pmids = self.search(query, max_results=max_results)

        # 获取详情
        articles = self.fetch_details(pmids)

        result = {
            "query": query,
            "pmids": pmids,
            "articles": articles,
        }

        # 写入缓存
        if self.use_cache:
            self.cache.set_pubmed(drug_id, query, result, params=cache_params)

        return result
